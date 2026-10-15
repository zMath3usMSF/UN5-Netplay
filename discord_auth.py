#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UN5 Netplay - login com Discord (lado do launcher, sem GUI).

Fluxo: o launcher pede ao servidor um link de login (POST /auth/discord/start), abre o navegador nele e fica
consultando (GET /auth/discord/poll) ate o servidor concluir o OAuth2 com o Discord. O client secret fica so no
servidor. O launcher recebe um token de sessao + nick/avatar do Discord e guarda tudo em %APPDATA% (appdata.py).

`DiscordMixin` e misturado na classe Backend (launcher_core.py); cada operacao roda em thread e termina com o evento
("acct", op, ok, erro_ou_None), op = "login" | "logout" | "restore".
"""
from __future__ import annotations

import json
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import webbrowser

import appdata

CDN_PREFIX = "https://cdn.discordapp.com/"      # o avatar so e baixado do CDN do Discord
PNG_SIG = b"\x89PNG\r\n\x1a\n"
AVATAR_MAX = 2 * 1024 * 1024
POLL_EVERY = 1.5
UA = "UN5Netplay/1.0"


class AccountError(Exception):
    """Erro de conta/rede. code = status HTTP (0 = sem conexao)."""

    def __init__(self, message, code=0):
        super().__init__(message)
        self.code = code


# ---- nick -----------------------------------------------------------------------------------------------------------
def ascii_nick(value) -> str:
    """O jogo so desenha 1-15 ASCII imprimiveis (sem < >): 'José ♥ Silva' -> 'Jose Silva'."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(c for c in text if 32 <= ord(c) <= 126 and c not in "<>")
    return " ".join(text.split())[:15].strip()


def account_game_nick(acc) -> str:
    """Nick usado no jogo para uma conta do Discord (apelido de exibicao, senao o @usuario). '' = padrao do jogo."""
    for name in ((acc or {}).get("nick"), (acc or {}).get("username")):
        nick = ascii_nick(name)
        if nick:
            return nick
    return ""


# ---- HTTP -----------------------------------------------------------------------------------------------------------
def api_call(server, method, path, body=None, token="", timeout=8.0) -> dict:
    headers, data = {"User-Agent": UA}, None
    if body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode("utf-8"), "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    base = server if server.startswith(("http://", "https://")) else f"http://{server}"
    req = urllib.request.Request(f"{base.rstrip('/')}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            msg = json.loads(exc.read().decode("utf-8"))["error"]
        except Exception:                                   # noqa: BLE001
            msg = f"Server error {exc.code}."
        raise AccountError(msg, exc.code) from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise AccountError(f"Cannot reach the server ({getattr(exc, 'reason', exc)}).", 0) from None


def fetch_avatar(url: str) -> bytes:
    if not str(url).startswith(CDN_PREFIX):
        raise AccountError("Unexpected avatar address.")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=8) as resp:
            data = resp.read(AVATAR_MAX + 1)
    except (urllib.error.URLError, OSError) as exc:
        raise AccountError(f"Could not download the avatar ({getattr(exc, 'reason', exc)}).") from None
    if len(data) > AVATAR_MAX or data[:8] != PNG_SIG:
        raise AccountError("The Discord avatar is not a PNG.")
    return data


# ---- Backend --------------------------------------------------------------------------------------------------------
class DiscordMixin:
    """Precisa de self.q (queue). Chame self._init_account(SERVER_ADDR) no __init__ do Backend."""

    def _init_account(self, server: str):
        self.server = server
        self.session, self.account = appdata.load_session()      # sessao do ultimo uso (validada por acct_restore)
        self.avatar_png: bytes | None = None
        if self.account:
            try:
                self.avatar_png = appdata.AVATAR_FILE.read_bytes()
            except OSError:
                pass
        self.login_url = ""
        self._login_cancel = threading.Event()

    def _acct_run(self, op, fn):
        def work():
            err = None
            try:
                fn()
            except AccountError as exc:
                if exc.code == 401 and op != "login":              # sessao expirou
                    self.session, self.account, self.avatar_png = "", None, None
                err = exc
            except Exception as exc:                               # noqa: BLE001
                err = AccountError(str(exc), 0)
            if self.session and self.account:
                appdata.save_session(self.session, self.account)
            elif not self.session:
                appdata.clear_session()
            self.q.put(("acct", op, err is None, err))
        threading.Thread(target=work, daemon=True).start()

    def _load_avatar(self):
        """Baixa o avatar do Discord. Se falhar, fica o que ja estava (cache)."""
        try:
            png = fetch_avatar((self.account or {}).get("avatar_url", ""))
        except AccountError:
            return
        self.avatar_png = png
        try:
            appdata.AVATAR_FILE.write_bytes(png)
        except OSError:
            pass

    def _enter(self, result):
        self.session, self.account, self.avatar_png = result["token"], result["account"], None
        self._load_avatar()

    def acct_restore(self):
        if not self.session:
            return

        def fn():
            self.account = api_call(self.server, "GET", "/accounts/me", token=self.session)
            self._load_avatar()
        self._acct_run("restore", fn)

    def acct_login_discord(self):
        self.acct_cancel_login()
        cancel = self._login_cancel = threading.Event()

        def fn():
            start = api_call(self.server, "POST", "/auth/discord/start", {})
            self.login_url = start["url"]
            webbrowser.open(self.login_url)                        # se falhar, a janela oferece COPY LINK
            deadline = time.monotonic() + float(start.get("expires", 300))
            while time.monotonic() < deadline:
                if cancel.wait(POLL_EVERY):
                    raise AccountError("Login cancelled.")
                try:
                    r = api_call(self.server, "GET", "/auth/discord/poll?state=" + start["state"])
                except AccountError as exc:
                    if exc.code == 0:                              # falha de rede passageira: tenta de novo
                        continue
                    raise
                if r.get("status") == "ok":
                    self._enter(r)
                    return
                if r.get("status") == "error":
                    raise AccountError(r.get("error", "Login failed."))
            raise AccountError("Login timed out. Try again.")
        self._acct_run("login", fn)

    def acct_cancel_login(self):
        self._login_cancel.set()

    def acct_logout(self):
        token = self.session

        def fn():
            try:
                api_call(self.server, "POST", "/accounts/logout", token=token)
            except AccountError:
                pass                                               # sai localmente de qualquer jeito
            self.session, self.account, self.avatar_png = "", None, None
        self._acct_run("logout", fn)