#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UN5 Netplay - BACKEND (sem nenhuma dependencia de GUI).

Tudo aqui pode ser testado sem abrir janela: configuracao, deteccao de arquivos, montagem dos argumentos do
netplay.py, interpretacao da saida, processo da sessao, lista de salas do servidor online e conta (login com Discord).

Os dados do usuario ficam em %APPDATA%\\UN5 Netplay (ver appdata.py): preferencias, sessao do Discord, avatar em cache
e o estado da partida (saves\\UN5-partida.p2s).

SAVE STATE PADRAO: ele mora no servidor (pasta un5/save state/). So o dono (conta do Discord marcada como `admin`
pelo servidor) faz PREPARE e publica. Quem da host ou conecta como guest baixa o save (e o symbols.json) do servidor
antes de iniciar (`sync_default`).

SALAS AO VIVO: enquanto uma sessao de host/guest roda, o backend le a saida do netplay.py (`_observe`: fase da partida
e ping real, as linhas "frame=... rtt=...ms") e a cada 2 s avisa o servidor (`POST /presence`). E isso que a lista de
salas mostra: avatar de quem e host/guest, status real, ping real e tempo de partida.

Os caminhos (pasta do projeto, symbols.json, estado da partida, python, pasta de save do PCSX2) NAO sao mais
configuraveis na tela: `full_config()` completa a configuracao do usuario com tudo que e detectado sozinho.
O nick do jogo vem sempre do perfil do Discord (`account_game_nick`); nao ha campo de nick.
O input delay tambem nao e mais escolhido na tela: o netplay.py recebe `--delay 0` (automatico): o host mede o
ping ate o servidor e escolhe; o guest usa o delay da sala.
A mascara do hash e o resto do diagnostico ficam como estao no NetPad.c / netpad_core.h (nenhuma flag de
diagnostico e enviada ao netplay.py).

A interface (launcher.py) so le/escreve `Backend.q` e chama os metodos do `Backend`.
Eventos que o backend coloca em `Backend.q`:
    ("line", texto)                 uma linha de saida da sessao
    ("exit", codigo)                a sessao terminou
    ("pine", ok, texto)             resultado do teste do PINE
    ("oneshot", codigo, texto, tok) resultado de um comando curto (ex.: release)
    ("acct", op, ok, erro)          resultado de uma operacao de conta (login, logout, restore); erro e um
                                    AccountError ou None
    ("sync", ok, texto)             resultado do download do save state padrao do servidor
    ("publish", ok, texto)          resultado da publicacao do save state padrao (so o dono)
    ("avatar", url)                 um avatar de jogador terminou de baixar (a lista de salas se redesenha)
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from i18n import tr
from appdata import CONFIG_FILE, DATA_DIR, STATE_NAME, STATE_PATH, SYMBOLS_PATH      # noqa: F401
from discord_auth import (UA, AccountError, DiscordMixin, account_game_nick, api_call,   # noqa: F401
                          fetch_avatar)

HERE = Path(__file__).resolve().parent
SERVER_URL = "https://un5netplay.duckdns.org"          # API (salas, conta, save state): HTTPS pelo Caddy
SERVER_ADDR = "137.131.158.54:47500"                   # UDP (descoberta e relay): NAO passa pelo Caddy
HOST_PORT = "47400"                                    # porta UDP local do host (fixa, sem campo na tela)                               # porta UDP local do host (fixa, sem campo na tela)
GUEST_BIND_PORT = "0"                                  # porta UDP local do guest (0 = automatica)
AUTO_DELAY = "0"                                       # --delay 0 = o netplay.py escolhe sozinho
SOLO_DELAY = "3"                                       # solo nao tem rede: nada para medir

STAT_RE = re.compile(r"frame=\s*(\d+)\s+([\d.]+)/s(?:\s+rtt=(\d+|None)ms)?\s+stalls=(\d+)")

# Fase da partida deduzida da saida do netplay.py. Linha de stat (frame=... rtt=...) = "fight". As demais fases entram
# aqui: (regex, fase). Fases aceitas pelo servidor: "fight" (na luta) e "select" (escolhendo personagem).
# A regra de "select" abaixo so funciona se o netplay.py imprimir uma linha assim; ver o aviso ao usuario.
PHASE_RULES = (
    (re.compile(r"^phase: select", re.I), "select"),
    (re.compile(r"^phase: fight", re.I), "fight"),
)
PRESENCE_EVERY = 2.0                                   # s entre avisos ao servidor
AVATAR_RETRY = 30.0                                    # s ate tentar baixar de novo um avatar que falhou

# O que o usuario realmente escolhe (e e salvo em %APPDATA%). state_dir so aparece se a pasta do PCSX2 nao for achada
# sozinha. A sessao do Discord NAO fica aqui: tem arquivo proprio (session.json).
# (chaves antigas salvas no json, como "nick", "delay" ou "session", sao ignoradas por load_config)
DEFAULTS = dict(mode="host", control="p1", room_id="", ignore_desync=False, state_dir="", lang="")

# Valores fixos (antes eram campos na tela).
FIXED = dict(pine_slot="28011", save_slot="1", load_slot="9", auto_copy=True, auto_load=True, fetch_state=True)


# ======================================================================================================================
# Configuracao
# ======================================================================================================================
def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:   # chaves antigas (project_dir, symbols_path, hash_mask, delay, session, nick, ...) sao ignoradas
        cfg.update({k: v for k, v in json.loads(CONFIG_FILE.read_text(encoding="utf-8")).items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg: dict):
    try:
        CONFIG_FILE.write_text(json.dumps({k: cfg[k] for k in DEFAULTS if k in cfg}, indent=2), encoding="utf-8")
    except OSError:
        pass


def full_config(user: dict, admin: bool = False) -> dict:
    """Configuracao do usuario + tudo que e fixo ou detectado sozinho (pasta do projeto, symbols, estado, python,
    pasta de save do PCSX2). E o dicionario que o resto do backend espera.
    `admin` (o dono): usa o symbols.json do proprio build. Os demais usam sempre o que baixaram do servidor."""
    cfg = {**FIXED, **user}
    cfg["project_dir"] = str(HERE)
    cfg["python_exe"] = default_python()
    if not admin and SYMBOLS_PATH.is_file():
        cfg["symbols_path"] = str(SYMBOLS_PATH)           # o symbols.json do servidor
    else:
        cfg["symbols_path"] = str(find_symbols(HERE) or SYMBOLS_PATH)
    cfg["state_file"] = str(find_state(HERE) or STATE_PATH)
    sdir = str(user.get("state_dir", "")).strip()
    if not (sdir and Path(sdir).expanduser().is_dir()):
        found = find_sstates_dir()
        sdir = str(found) if found else ""
    cfg["state_dir"] = sdir
    return cfg


# ======================================================================================================================
# Deteccao de arquivos
# ======================================================================================================================
def default_python() -> str:
    """python.exe para rodar o netplay.py (nunca pythonw.exe: ele nao tem stdout para o console)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        sibling = exe.with_name("python.exe")
        if sibling.is_file():
            return str(sibling)
    return str(exe)


def _unique(paths):
    seen, out = set(), []
    for p in paths:
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            out.append(Path(p))
    return out


def resolve_project(saved: str) -> Path:
    """Pasta salva (se tiver netplay.py) > pasta do launcher > pasta salva mesmo quebrada (para a UI mostrar)."""
    saved = (saved or "").strip()
    if saved:
        path = Path(saved).expanduser()
        if (path / "netplay.py").is_file():
            return path
    if (HERE / "netplay.py").is_file():
        return HERE
    return Path(saved).expanduser() if saved else HERE


def find_state(project: Path):
    """O estado da partida: primeiro o do AppData, depois os lugares antigos (compatibilidade)."""
    home = Path.home()
    for p in _unique([STATE_PATH, HERE / STATE_NAME, project / STATE_NAME, home / "Desktop" / STATE_NAME,
                      home / "Documents" / STATE_NAME, home / "Downloads" / STATE_NAME]):
        if p.is_file():
            return p
    return None


def find_symbols(project: Path):
    """symbols.json do build (host). Sem build, o do AppData (o que o guest baixa do servidor)."""
    desktop = Path.home() / "Desktop"
    cands = [project / "symbols.json", project / "temp_build" / "build" / "symbols.json",
             HERE / "symbols.json", HERE / "temp_build" / "build" / "symbols.json",
             desktop / "UN5 Online" / "temp_build" / "build" / "symbols.json"]
    if desktop.is_dir():
        cands += sorted(desktop.glob("*/temp_build/build/symbols.json"))
    cands.append(SYMBOLS_PATH)
    for p in _unique(cands):
        if p.is_file():
            return p
    return None


def find_sstates_dir():
    home = Path.home()
    for p in (Path(r"C:\Program Files (x86)\PCSX2 QT\sstates"), Path(r"C:\Program Files\PCSX2\sstates"),
              home / "Documents" / "PCSX2" / "sstates"):
        if p.is_dir():
            return p
    return None


def state_tag(path) -> str:
    """Mesmo id que o netplay.py deriva do arquivo (sha256[:16]): os dois PCs e o servidor devem mostrar o mesmo."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


# ======================================================================================================================
# Interpretacao da saida do netplay.py
# ======================================================================================================================
def parse_stat(line: str):
    m = STAT_RE.search(line)
    if not m:
        return None
    rtt = m.group(3)
    return dict(frame=int(m.group(1)), fps=float(m.group(2)),
                rtt=int(rtt) if rtt and rtt.isdigit() else None, stalls=int(m.group(4)))


def classify(line: str):
    """(tag do console, (texto de status, cor) ou None). Cores sao nomes: red green blue warn muted."""
    low = line.lower()
    if "desync at frame" in low:
        return "error", ("DESYNC", "red")
    if "hash differs at frame" in low:             # "Ignore desync" ligado: a partida continua
        return "warn", None
    if STAT_RE.search(line):                       # toda linha de stat traz "abort=0": so e erro com abort=1
        return ("error" if "abort=1" in low else None), ("IN MATCH", "green")
    if any(k in low for k in ("cannot reach", "cannot load", "traceback", "error", "failed", "aborted", "gave up",
                              "not frozen", "did not freeze", "no new .p2s", "problem")):
        return "error", None
    if "frozen at frame 0" in low or "already frozen" in low:
        return "success", ("FROZEN", "blue")
    if "match released" in low:
        return "success", ("IN MATCH", "green")
    if low.startswith("connection:"):
        return "info", None
    if low.startswith(("config:", "sync:", "sel ", "rand call sites")) or low.startswith("    0x"):
        return "dim", None
    if low.startswith(("loading ", "downloading ", "checking the host")) or "asking pcsx2 to save" in low:
        return "info", ("LOADING", "blue")
    if "waiting for the" in low:
        return None, ("WAITING", "warn")
    if "state saved:" in low:
        return "success", None
    if "stopped:" in low or "game released" in low:
        return "success", ("READY", "muted")
    if low.startswith(("host:", "guest:")):
        return "info", ("CONNECTING", "blue")
    return None, None


# ======================================================================================================================
# Validacao e argumentos do netplay.py
# ======================================================================================================================
def int_in(value, label, lo, hi) -> str:
    try:
        n = int(str(value).strip())
    except ValueError:
        raise ValueError(tr("err.int", label=label)) from None
    if not lo <= n <= hi:
        raise ValueError(tr("err.range", label=label, lo=lo, hi=hi))
    return str(n)


def clean_nick(value: str) -> str:
    """Nick como o jogo desenha: 1-15 ASCII imprimiveis, sem '<' ou '>' (tags de fonte)."""
    nick = str(value or "").strip()
    if not nick:
        return ""
    if len(nick) > 15:
        raise ValueError(tr("err.nick_len"))
    if any(not 32 <= ord(ch) <= 126 or ch in "<>" for ch in nick):
        raise ValueError(tr("err.nick_chars"))
    return nick


def _need_state_dir(cfg):
    sdir = cfg.get("state_dir", "").strip()
    if not sdir or not Path(sdir).is_dir():
        raise ValueError(tr("err.state_dir"))
    return sdir


def make_args(cfg: dict, action: str, state_path=None):
    """Argumentos depois das opcoes globais do netplay.py. `cfg` e o resultado de full_config() (+ `nick`, que o
    launcher tira do perfil do Discord). Levanta ValueError com o motivo. Nenhuma flag de diagnostico (mascara, rng,
    watch) e enviada: valem os padroes do NetPad.c / netpad_core.h e do netplay.py. O delay e sempre automatico
    (--delay 0), exceto no solo."""
    missing_state = tr("err.no_state", name=STATE_NAME)
    if action == "prepare":
        args = ["prepare", "--save-slot", int_in(cfg["save_slot"], "Save slot", 0, 99)]
        if cfg.get("state_dir", "").strip():
            args += ["--state-dir", cfg["state_dir"].strip()]
        return args
    if action == "load":
        if not state_path or not Path(state_path).is_file():
            raise ValueError(missing_state)
        return ["load", "--state", str(state_path), "--state-dir", _need_state_dir(cfg),
                "--load-slot", int_in(cfg["load_slot"], "Load slot", 0, 99)]

    if action == "solo":                                 # sem rede nao ha ping para medir
        return ["solo", "--delay", SOLO_DELAY, "--control", cfg["control"]]
    if action not in ("host", "guest"):
        raise ValueError(f"unknown action {action}")

    delay = AUTO_DELAY                                   # 0 = automatico: o host escolhe pelo ping, o guest usa o da sala
    fetch = action == "guest" and bool(cfg.get("fetch_state", True))   # o arquivo do servidor e baixado
    if not state_path or (not Path(state_path).is_file() and not fetch):
        raise ValueError(missing_state)
    args = [action, "--state", str(state_path), "--delay", delay]
    if fetch:
        args.append("--fetch-state")
    if cfg.get("auto_load", True):
        args += ["--state-dir", _need_state_dir(cfg), "--load-slot", int_in(cfg["load_slot"], "Load slot", 0, 99)]
    nick = clean_nick(cfg.get("nick", ""))
    if nick:
        args += ["--nick", nick]

    args += ["--server", SERVER_ADDR, "--api-url", SERVER_URL]
    if action == "host":
        if cfg.get("ignore_desync", False):              # so o host detecta desync
            args.append("--ignore-desync")
        return args + ["--port", HOST_PORT]
    room_id = cfg.get("room_id", "").strip()
    if not room_id:
        raise ValueError(tr("err.pick_room"))
    return args + ["--room", room_id, "--bind-port", GUEST_BIND_PORT]


# ======================================================================================================================
# Backend: processo da sessao, tarefas em thread, salas online, conta (DiscordMixin, ver discord_auth.py)
# ======================================================================================================================
class Backend(DiscordMixin):
    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.proc: subprocess.Popen | None = None
        self.started_at: float | None = None
        self.last_cmd = ""
        self.stop_file = Path(os.environ.get("TEMP", str(HERE))) / f"un5_stop_{os.getpid()}.flag"
        self.online_rooms: list = []
        self.online_error = ""
        self._online_at = 0.0
        self._online_busy = False
        self.phase = ""                                  # fase real da partida (ver PHASE_RULES) ou "" = ainda nao sei
        self.ping = None                                 # ping real (rtt) da partida, em ms, ou None
        self.avatar_cache: dict = {}                     # url -> (png ou b"" se falhou, instante)
        self._avatar_busy: set = set()
        # conta do Discord: self.session / self.account / self.avatar_png (carregados do AppData)
        self._init_account(SERVER_URL)

    # ---------------------------------------------------------------- estado
    @property
    def running(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    @staticmethod
    def _flags():
        return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    @staticmethod
    def _env():
        return {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}

    def _unlink_stop_file(self):
        try:
            self.stop_file.unlink()
        except OSError:
            pass

    # ---------------------------------------------------------------- comando base
    def base_command(self, cfg: dict, allow_missing_symbols=False):
        project = resolve_project(cfg["project_dir"])
        script = project / "netplay.py"
        if not script.is_file():
            raise ValueError(tr("err.netplay_missing", project=project))
        symbols = Path(cfg["symbols_path"].strip() or "symbols.json").expanduser()
        if not symbols.is_absolute():
            symbols = project / symbols
        if not symbols.is_file() and not allow_missing_symbols:
            raise ValueError(tr("err.symbols_missing"))
        slot = int_in(cfg["pine_slot"], "PINE slot", 1, 65535)
        py = cfg["python_exe"].strip() or default_python()
        return project, [py, "-u", str(script), "--symbols", str(symbols.resolve()), "--pine-slot", slot,
                         "--stop-file", str(self.stop_file)]

    # ---------------------------------------------------------------- sessao
    def spawn(self, cfg: dict, args, allow_missing_symbols=False) -> str:
        """Inicia o netplay.py. Levanta ValueError (configuracao) ou OSError (nao abriu)."""
        if self.running:
            raise RuntimeError(tr("err.busy"))
        project, base = self.base_command(cfg, allow_missing_symbols)
        save_config(cfg)
        self._unlink_stop_file()
        args = [str(a) for a in args]
        cmd = base + args
        self.last_cmd = subprocess.list2cmdline(cmd)
        self.phase, self.ping = "", None
        self.proc = subprocess.Popen(cmd, cwd=str(project), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                     bufsize=1, env=self._env(), creationflags=self._flags())
        self.started_at = time.monotonic()
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        role = args[0] if args else ""
        if role in ("host", "guest"):                    # so as sessoes de rede aparecem na lista de salas
            room = args[args.index("--room") + 1] if "--room" in args[:-1] else ""
            threading.Thread(target=self._presence_loop, args=(self.proc, role, room), daemon=True).start()
        return self.last_cmd

    def _observe(self, line: str):
        """Fase e ping reais da partida, lidos da saida do netplay.py."""
        stat = parse_stat(line)
        if stat:
            if not self.phase:
                self.phase = "fight"
            if stat["rtt"] is not None:
                self.ping = stat["rtt"]
            return
        for rule, phase in PHASE_RULES:
            if rule.search(line):
                self.phase = phase
                return

    def _reader(self, proc):
        try:
            for line in proc.stdout:
                line = line.rstrip("\r\n")
                self._observe(line)
                self.q.put(("line", line))
        except (OSError, ValueError):
            pass
        self.q.put(("exit", proc.wait()))

    def _presence_loop(self, proc, role, room):
        """Enquanto a sessao roda, conta ao servidor quem somos e o que a partida esta fazendo. Falhas (sala ainda nao
        criada, rede) sao ignoradas: tenta de novo em 2 s."""
        while proc.poll() is None:
            body = dict(role=role)
            if room:
                body["room"] = room
            if self.phase:
                body["phase"] = self.phase
            if self.ping is not None:
                body["ping"] = self.ping
            try:
                api_call(SERVER_URL, "POST", "/presence", body, token=self.session, timeout=4.0)
            except AccountError:
                pass
            try:
                proc.wait(timeout=PRESENCE_EVERY)
            except subprocess.TimeoutExpired:
                pass

    def request_stop(self):
        """Pede parada limpa (arquivo de stop). Levanta OSError se nao conseguir escrever."""
        self.stop_file.write_text("stop")

    def force_stop(self) -> bool:
        """Termina o processo se ainda estiver vivo. True se precisou forcar."""
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            return True
        return False

    def on_exit(self):
        self.proc, self.started_at = None, None
        self.phase, self.ping = "", None
        self._unlink_stop_file()

    # ---------------------------------------------------------------- comandos curtos
    def oneshot(self, cfg: dict, args, token=None):
        project, base = self.base_command(cfg)
        cmd = base + list(args)

        def work():
            try:
                r = subprocess.run(cmd, cwd=str(project), capture_output=True, text=True, timeout=25,
                                   env=self._env(), creationflags=self._flags())
                self.q.put(("oneshot", r.returncode, (r.stdout + r.stderr).strip(), token))
            except (OSError, subprocess.TimeoutExpired) as exc:
                self.q.put(("oneshot", -1, str(exc), token))
        threading.Thread(target=work, daemon=True).start()

    def test_pine(self, cfg: dict):
        project = resolve_project(cfg["project_dir"])
        script = project / "pine.py"
        if not script.is_file():
            raise ValueError(tr("err.pine_missing", project=project))
        cmd = [cfg["python_exe"].strip() or default_python(), "-u", str(script),
               cfg["pine_slot"].strip() or "28011"]

        def work():
            try:
                r = subprocess.run(cmd, cwd=str(project), capture_output=True, text=True, timeout=20,
                                   env=self._env(), creationflags=self._flags())
                self.q.put(("pine", r.returncode == 0, (r.stdout + r.stderr).strip()))
            except (OSError, subprocess.TimeoutExpired) as exc:
                self.q.put(("pine", False, str(exc)))
        threading.Thread(target=work, daemon=True).start()

    # ---------------------------------------------------------------- salas do servidor online
    def poll_rooms(self):
        """Chame com frequencia: a cada 3 s consulta o servidor (em thread) e atualiza online_rooms/online_error.
        online_rooms e SUBSTITUIDA (nunca editada) a cada resposta: quem compara por identidade sabe que mudou.
        Cada sala traz {id, nick, delay, tag, status, players:{host, guest}, phase, ping, elapsed, fetched}."""
        if self._online_busy or time.monotonic() - self._online_at < 3.0:
            return
        self._online_at, self._online_busy = time.monotonic(), True

        def work():
            try:
                data = api_call(SERVER_URL, "GET", "/rooms", timeout=4.0)
                now = time.monotonic()
                self.online_rooms = [dict(r, fetched=now) for r in data.get("rooms", []) if isinstance(r, dict)]
                self.online_error = ""
            except Exception as exc:                      # noqa: BLE001 - mostrado no card Rooms
                self.online_rooms, self.online_error = [], str(exc)
            finally:
                self._online_busy = False
        threading.Thread(target=work, daemon=True).start()

    # ---------------------------------------------------------------- avatares dos jogadores das salas
    def avatar_for(self, url):
        """PNG do avatar (bytes) se ja estiver baixado; senao comeca o download em segundo plano e devolve None.
        Quando terminar chega o evento ("avatar", url). Uma falha so e tentada de novo depois de AVATAR_RETRY s."""
        if not url:
            return None
        hit = self.avatar_cache.get(url)
        if hit is not None:
            png, at = hit
            if png or time.monotonic() - at < AVATAR_RETRY:
                return png or None
        if url in self._avatar_busy:
            return None
        self._avatar_busy.add(url)
        if len(self.avatar_cache) > 200:
            self.avatar_cache.clear()

        def work():
            try:
                png = fetch_avatar(url.replace("size=128", "size=64"))   # 64 px bastam para a lista
            except Exception:                              # noqa: BLE001 - rede ou arquivo invalido: so a inicial
                png = b""
            self.avatar_cache[url] = (png, time.monotonic())
            self._avatar_busy.discard(url)
            self.q.put(("avatar", url))
        threading.Thread(target=work, daemon=True).start()
        return None

    # ---------------------------------------------------------------- save state padrao (servidor)
    def check_join(self, room_id: str):
        """Guest: asks the server whether this player may join the room (not the same IP as the host, not the same
        account; the owner is exempt) and gets the ticket the join needs. Event: ("allow", ok, texto)."""
        token = self.session

        def work():
            try:
                api_call(SERVER_URL, "POST", f"/rooms/{room_id}/allow", {}, token=token, timeout=6.0)
                self.q.put(("allow", True, ""))
            except Exception as exc:                          # noqa: BLE001 - AccountError (server said no) or network
                self.q.put(("allow", False, str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def is_admin(self) -> bool:
        """True se a conta logada e o dono do servidor (o servidor decide: campo `admin` de /accounts/me)."""
        return bool(self.account and self.account.get("admin"))

    @staticmethod
    def _download(path: str, dest: Path, want_tag: str):
        """Baixa http://SERVER_ADDR<path> para `dest`, conferindo o sha256[:16]. Levanta AccountError."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        digest = hashlib.sha256()
        req = urllib.request.Request(f"{SERVER_URL}{path}", headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp, open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    digest.update(chunk)
                    f.write(chunk)
            if digest.hexdigest()[:16] != want_tag:
                raise AccountError(tr("err.corrupt"))
            os.replace(tmp, dest)
        except urllib.error.HTTPError as exc:
            raise AccountError(tr("err.dl_http", code=exc.code)) from None
        except (urllib.error.URLError, OSError) as exc:
            raise AccountError(tr("err.dl_fail", why=getattr(exc, "reason", exc))) from None
        finally:
            tmp.unlink(missing_ok=True)

    def sync_default(self):
        """Baixa o save state (e o symbols.json, se nao for o dono) do servidor quando o local for diferente.
        Roda em thread; evento: ("sync", ok, texto)."""
        admin = self.is_admin()

        def work():
            try:
                try:
                    info = api_call(SERVER_URL, "GET", "/default")
                except AccountError as exc:
                    if exc.code == 404:
                        raise AccountError(tr("err.no_default")) from None
                    raise
                wanted = [("state", STATE_PATH)] + ([] if admin else [("symbols", SYMBOLS_PATH)])
                for kind, dest in wanted:
                    want = info.get(kind)
                    if not want:
                        raise AccountError(tr("err.no_kind", kind=kind))
                    local = state_tag(dest) if dest.is_file() else ""
                    if local != want:
                        self.q.put(("line", tr("log.dl_default", kind=kind)))
                        self._download(f"/default/{kind}", dest, want)
                self.q.put(("sync", True, ""))
            except AccountError as exc:
                self.q.put(("sync", False, str(exc)))
            except OSError as exc:
                self.q.put(("sync", False, str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def publish_default(self):
        """So o dono: sobe o symbols.json e o estado recem-criado para o servidor. Evento: ("publish", ok, texto)."""
        token = self.session
        symbols = find_symbols(HERE) or SYMBOLS_PATH

        def work():
            try:
                for kind, path in (("symbols", symbols), ("state", STATE_PATH)):
                    req = urllib.request.Request(
                        f"{SERVER_URL}/default/{kind}", data=Path(path).read_bytes(), method="PUT",
                        headers={"User-Agent": UA, "Authorization": "Bearer " + token,
                                 "Content-Type": "application/octet-stream"})
                    with urllib.request.urlopen(req, timeout=180) as resp:
                        resp.read()
                self.q.put(("publish", True, ""))
            except urllib.error.HTTPError as exc:
                try:
                    msg = json.loads(exc.read().decode("utf-8"))["error"]
                except Exception:                           # noqa: BLE001
                    msg = f"server error {exc.code}"
                self.q.put(("publish", False, msg))
            except (urllib.error.URLError, OSError) as exc:
                self.q.put(("publish", False, str(getattr(exc, "reason", exc))))
        threading.Thread(target=work, daemon=True).start()

    # ---------------------------------------------------------------- encerramento
    def shutdown(self):
        self.acct_cancel_login()
        if self.running:
            try:
                self.request_stop()
                self.proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.terminate()