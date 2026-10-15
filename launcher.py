#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UN5 Netplay Launcher - FRONTEND (janela). Toda a logica esta em launcher_core.py.

Coloque os arquivos (launcher.py, launcher_core.py, launcher_theme.py, launcher_account.py, discord_auth.py,
appdata.py, i18n.py) na pasta do netplay.py e rode:
    python launcher.py

Os dados do usuario (preferencias, sessao do Discord, estado da partida) ficam em %APPDATA%\\UN5 Netplay.

Idiomas: todo texto da janela vem de i18n.tr(). O idioma fica salvo na configuracao (chave "lang"); na primeira
execucao vem de i18n.detect(). Trocar o idioma reconstroi a janela na hora (o historico do console e preservado).
"""
from __future__ import annotations

import io
import queue
import re
import shutil
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    from PIL import Image, ImageDraw, ImageTk
except ImportError:                                       # sem Pillow: a lista de salas fica sem avatares
    Image = None

import i18n
import launcher_core as core
from i18n import has, tr
from launcher_account import DiscordDialog, ProfileDialog
from launcher_core import SERVER_ADDR
from launcher_theme import (ACCENT, BG, BLUE, BORDER, COLORS, DIM, GREEN, MUTED, ON_ACCENT, PANEL, RED, TEXT, WARN,
                            AvatarView, PillButton, RoundEntry, RoundFrame, Segmented, Switch, apply_style)

ROW_AV = 30                                               # lado do avatar (px) na lista de salas
GUEST_PAD = " " * 11                                      # recuo do nome do guest (espaco do avatar)


def make_avatar(png):
    """PNG (bytes) -> PhotoImage redondo de ROW_AV px, ou None."""
    if Image is None or not png:
        return None
    try:
        big = ROW_AV * 2
        resample = getattr(Image, "Resampling", Image).LANCZOS
        im = Image.open(io.BytesIO(png)).convert("RGBA").resize((big, big), resample)
        mask = Image.new("L", (big, big), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, big - 1, big - 1), fill=255)
        im.putalpha(mask)
        return ImageTk.PhotoImage(im.resize((ROW_AV, ROW_AV), resample))
    except Exception:                                     # noqa: BLE001 - arquivo invalido: fica sem avatar
        return None


# ======================================================================================================================
# Janela principal
# ======================================================================================================================
class Launcher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("UN5 Netplay")
        self.geometry("1120x740")
        self.minsize(980, 660)
        self.configure(bg=BG)
        cfg = core.load_config()
        cfg["ignore_desync"] = False                      # debug: sempre OFF ao abrir
        cfg["lang"] = cfg.get("lang") if cfg.get("lang") in i18n.LANGS else i18n.detect()
        i18n.set_lang(cfg["lang"])
        self.v = {k: (tk.BooleanVar(value=val) if isinstance(val, bool) else tk.StringVar(value=val))
                  for k, val in cfg.items()}

        self.backend = core.Backend()                     # carrega a sessao do Discord salva no AppData
        self.dialog = None
        self._after_sync = None                           # o que fazer quando o download do save padrao terminar
        self._after_allow = None
        self.rooms_data: dict = {}
        self.guest_imgs: dict = {}                        # id da sala -> avatar do guest (ou None)
        self.guest_labels: dict = {}                      # id da sala -> Label por cima da celula GUEST
        self._avimgs: dict = {}                           # url do avatar -> PhotoImage (precisa manter referencia)
        self._history: list = []                          # (texto, tag) do console: reaplicado ao trocar de idioma
        self.status_key, self.status_color = "READY", MUTED

        apply_style(self)
        ttk.Style(self).configure("Treeview", rowheight=ROW_AV + 8)
        self.blank_av = tk.PhotoImage(width=ROW_AV, height=ROW_AV)   # transparente: alinha linhas sem avatar
        self._build()
        self._sync_mode()
        self._refresh_account()
        self.backend.acct_restore()
        self.after(80, self._drain)
        self.after(600, self._rooms_tick)
        self._set_status("READY", MUTED)
        self._log(tr("log.server", addr=SERVER_ADDR), "dim")
        self._log(tr("log.data", path=core.DATA_DIR), "dim")
        self._log(tr("log.hint"), "dim")

    # ================================================================ configuracao completa
    def raw(self) -> dict:
        return {k: v.get() for k, v in self.v.items()}

    def cfg(self) -> dict:
        """Escolhas da tela + tudo que e fixo/detectado (core.full_config) + o nick do perfil do Discord."""
        cfg = core.full_config(self.raw(), self.backend.is_admin())
        acc = self.backend.account
        cfg["nick"] = core.account_game_nick(acc) if acc else ""
        return cfg

    # ================================================================ idioma
    def _change_lang(self):
        code = self.v["lang"].get()
        if code not in i18n.LANGS or code == i18n.get_lang():
            return
        i18n.set_lang(code)
        core.save_config({**self.raw(), "ignore_desync": False})
        self.after_idle(self._rebuild)                    # nao destroi o widget dentro do proprio clique

    def _rebuild(self):
        """Reconstroi a janela inteira no idioma atual. O console e o status sao restaurados."""
        if self.dialog is not None and self.dialog.winfo_exists():
            self.dialog.destroy()
        self.dialog = None
        for w in self.winfo_children():
            w.destroy()
        self.guest_imgs, self.guest_labels = {}, {}
        self._build()
        self._sync_mode()
        self._refresh_account()
        self._set_buttons(self.backend.running)
        self._set_status(self.status_key, self.status_color)
        for text, tag in self._history:
            self._write(text, tag)
        self._log(tr("log.lang"), "info")
        self._relayout()
        self.after(80, self._relayout)
        self.after(300, self._relayout)

    # ================================================================ layout
    def _relayout(self):
        """Depois de reconstruir a janela (troca de idioma): recalcula, de dentro para fora, todos os cards
        arredondados com o tamanho real, sem depender dos eventos <Configure> (que podem nao chegar)."""
        frames = []

        def walk(w):
            for c in w.winfo_children():
                walk(c)
                if isinstance(c, RoundFrame):
                    frames.append(c)
        try:
            for _ in range(3):
                frames.clear()
                walk(self)
                self.update_idletasks()
                for rf in frames:
                    rf.refresh()
            self.update_idletasks()
        except tk.TclError:
            pass

    def _build(self):
        self._header()

        body = tk.Frame(self, bg=BG, padx=22)
        body.pack(fill="both", expand=True)

        left = tk.Frame(body, bg=BG, width=360)
        left.pack(side="left", fill="y", padx=(0, 18))
        left.pack_propagate(False)
        self._card_main(left)

        right = tk.Frame(body, bg=BG)
        right.pack(side="left", fill="both", expand=True)
        console_host = tk.Frame(right, bg=BG)             # console pequeno, colado embaixo
        console_host.pack(side="bottom", fill="x")
        self._console(console_host)
        self._rooms_panel(right)                          # as salas ocupam o resto: e a informacao principal

        foot = tk.Frame(self, bg=BG, padx=22, pady=8)
        foot.pack(fill="x")
        tk.Label(foot, text=tr("footer"), bg=BG, fg=DIM, font=("Segoe UI", 8)).pack(side="left")

    def _header(self):
        head = tk.Frame(self, bg=BG, padx=22, pady=14)
        head.pack(fill="x")
        tk.Frame(head, bg=ACCENT, width=5, height=44).pack(side="left", padx=(0, 13))
        box = tk.Frame(head, bg=BG)
        box.pack(side="left")
        tk.Label(box, text="UN5 NETPLAY", bg=BG, fg=TEXT, font=("Segoe UI", 21, "bold")).pack(anchor="w")
        tk.Label(box, text=tr("subtitle"), bg=BG, fg=MUTED, font=("Segoe UI", 8, "bold")).pack(anchor="w")

        self._account_strip(head)
        self._lang_switch(head)

    def _lang_switch(self, parent):
        box = tk.Frame(parent, bg=BG)
        box.pack(side="right", padx=(0, 18))
        tk.Label(box, text=tr("lang.label"), bg=BG, fg=MUTED, font=("Segoe UI", 8, "bold")).pack(anchor="w",
                                                                                                pady=(0, 3))
        seg = Segmented(box, [(code, code.upper()) for code in i18n.LANGS], self.v["lang"], self._change_lang)
        seg.configure(width=110, height=Segmented.HEIGHT)
        seg.pack()

    # ---- faixa da conta (cabecalho)
    def _account_strip(self, parent):
        strip = RoundFrame(parent, radius=16, padx=12, pady=7, stretch=False)
        strip.pack(side="right")
        inner = strip.inner
        self.acct_av = AvatarView(inner, 30, bg=PANEL)
        self.acct_av.pack(side="left")
        box = tk.Frame(inner, bg=PANEL)
        box.pack(side="left", padx=(10, 0))
        self.acct_name = tk.Label(box, text=tr("acct.guest"), bg=PANEL, fg=TEXT, font=("Segoe UI", 10, "bold"),
                                  anchor="w")
        self.acct_name.pack(anchor="w")
        self.acct_sub = tk.Label(box, text=tr("acct.login"), bg=PANEL, fg=ACCENT, font=("Segoe UI", 8, "bold"),
                                 anchor="w")
        self.acct_sub.pack(anchor="w")
        for w in (strip, inner, self.acct_av, box, self.acct_name, self.acct_sub):
            w.configure(cursor="hand2")
            w.bind("<Button-1>", lambda _e: self.open_account())

    def open_account(self):
        if self.dialog is not None and self.dialog.winfo_exists():
            self.dialog.lift()
            return
        self.dialog = (ProfileDialog if self.backend.account else DiscordDialog)(self)

    def _refresh_account(self):
        acc, png = self.backend.account, self.backend.avatar_png
        self.acct_name.configure(text=acc["nick"] if acc else tr("acct.guest"))
        self.acct_sub.configure(text=tr("acct.profile") if acc else tr("acct.login"))
        self.acct_av.set(png if acc else None, acc["nick"][:1] if acc else "?")
        self._nick_hint()
        self._sync_admin()
        self._set_buttons(self.backend.running)           # PREPARE so existe para o dono

    def _on_acct(self, op, ok, err):
        self._refresh_account()
        if op == "restore":
            if ok:
                self._log(tr("log.signed_in", nick=self.backend.account["nick"]), "success")
            else:
                self._log(tr("log.login_not_restored", err=err), "warn")
        elif op == "logout":
            self._log(tr("log.signed_out"), "info")
        if self.dialog is not None and self.dialog.winfo_exists():
            self.dialog.on_result(op, ok, err)

    def _card(self, parent, number, title):
        card = RoundFrame(parent, radius=18, padx=16, pady=14)
        card.pack(fill="x", pady=(0, 13))
        inner = card.inner
        head = tk.Frame(inner, bg=PANEL)
        head.pack(fill="x", pady=(0, 6))
        tk.Label(head, text=number, bg=ACCENT, fg=ON_ACCENT, font=("Segoe UI", 9, "bold"), width=2).pack(side="left")
        tk.Label(head, text=title, bg=PANEL, fg=TEXT, font=("Segoe UI", 11, "bold")).pack(side="left", padx=(9, 0))
        return inner

    def _label(self, parent, text):
        tk.Label(parent, text=text, bg=PANEL, fg=MUTED, font=("Segoe UI", 8, "bold")).pack(anchor="w", pady=(7, 3))

    def _note(self, parent, text, wrap=300):
        tk.Label(parent, text=text, bg=PANEL, fg=DIM, font=("Segoe UI", 8), wraplength=wrap,
                 justify="left").pack(anchor="w", pady=(5, 0))

    # ---- card unico: jogador + partida + executar (o nick vem do Discord: nao ha campo)
    def _card_main(self, parent):
        card = RoundFrame(parent, radius=18, padx=16, pady=14)
        card.pack(side="top", fill="x", anchor="n")
        inner = card.inner
        tk.Label(inner, text=tr("card.match"), bg=PANEL, fg=TEXT, font=("Segoe UI", 11, "bold")).pack(anchor="w",
                                                                                                    pady=(0, 6))

        tk.Label(inner, text=tr("playing_as"), bg=PANEL, fg=ACCENT, font=("Segoe UI", 8, "bold")).pack(anchor="w",
                                                                                                     pady=(2, 3))
        self.nick_hint = tk.Label(inner, text="", bg=PANEL, fg=TEXT, font=("Segoe UI", 10, "bold"), anchor="w",
                                  justify="left", wraplength=300)
        self.nick_hint.pack(anchor="w")
        self.nick_sub = tk.Label(inner, text="", bg=PANEL, fg=DIM, font=("Segoe UI", 8), anchor="w",
                                 justify="left", wraplength=300)
        self.nick_sub.pack(anchor="w", pady=(2, 0))

        self._label(inner, tr("your_role"))
        Segmented(inner, [("host", "HOST  ·  P1"), ("guest", "GUEST  ·  P2")], self.v["mode"],
                  self._sync_mode).pack(fill="x")
        self._note(inner, tr("note.delay"))

        self.mode_box = tk.Frame(inner, bg=PANEL)         # host_box / guest_box alternam aqui dentro
        self.mode_box.pack(fill="x")
        self.host_box = tk.Frame(self.mode_box, bg=PANEL)
        Switch(self.host_box, tr("ignore_desync"), self.v["ignore_desync"]).pack(anchor="w", pady=(9, 0))
        self._note(self.host_box, tr("note.host"))
        self.guest_box = tk.Frame(self.mode_box, bg=PANEL)
        self._label(self.guest_box, tr("room.label"))
        RoundEntry(self.guest_box, textvariable=self.v["room_id"], state="readonly").pack(fill="x")
        self._note(self.guest_box, tr("note.guest"))

        tk.Frame(inner, bg=BORDER, height=1).pack(fill="x", pady=(14, 12))   # separador

        self.start_btn = PillButton(inner, text=tr("btn.start_host"), style="Accent.TButton", command=self.start)
        self.start_btn.pack(fill="x", pady=(0, 6))
        self.stop_btn = PillButton(inner, text=tr("btn.stop"), style="Danger.TButton", command=self.stop,
                                   state="disabled")
        self.stop_btn.pack(fill="x")

        # PREPARAR PARTIDA e Ferramentas: so para o dono (ver _sync_admin)
        self.admin_box = tk.Frame(inner, bg=PANEL)
        self.prepare_btn = PillButton(self.admin_box, text=tr("btn.prepare"), command=self.prepare)
        self.prepare_btn.pack(fill="x", pady=(6, 0))
        self.tools_open = False
        self.tools_toggle = tk.Label(self.admin_box, text="▸ " + tr("tools"), bg=PANEL, fg=ACCENT,
                                     font=("Segoe UI", 9, "bold"), cursor="hand2")
        self.tools_toggle.pack(anchor="w", pady=(11, 0))
        self.tools_toggle.bind("<Button-1>", lambda _e: self._toggle_tools())
        self.tools = tk.Frame(self.admin_box, bg=PANEL)
        self.load_btn = PillButton(self.tools, text=tr("btn.load"), command=self.load_state)
        self.load_btn.pack(fill="x", pady=(8, 6))
        row = tk.Frame(self.tools, bg=PANEL)
        row.pack(fill="x", pady=(0, 6))
        self.solo_btn = PillButton(row, text=tr("btn.solo"), command=self.solo)
        self.solo_btn.pack(side="left")
        ctl = Segmented(row, [("p1", "P1"), ("p2", "P2"), ("both", "BOTH")], self.v["control"])
        ctl.configure(width=170, height=Segmented.HEIGHT)
        ctl.pack(side="left", fill="x", expand=True, padx=(8, 0))
        self.pine_btn = PillButton(self.tools, text=tr("btn.pine"), command=self.test_pine)
        self.pine_btn.pack(fill="x", pady=(0, 6))
        self.release_btn = PillButton(self.tools, text=tr("btn.release"), command=self.release_game)
        self.release_btn.pack(fill="x")
        self._note(self.tools, tr("note.tools"))

    def _sync_admin(self):
        # o servidor decide quem e o dono (campo admin da conta); aqui o bloco so aparece/some
        if not hasattr(self, "admin_box"):
            return
        if self.backend.is_admin():
            self.admin_box.pack(fill="x")
        else:
            self.admin_box.pack_forget()

    def _toggle_tools(self):
        self.tools_open = not self.tools_open
        self.tools_toggle.configure(text=("▾ " if self.tools_open else "▸ ") + tr("tools"))
        (self.tools.pack(fill="x") if self.tools_open else self.tools.pack_forget())

    def _rooms_panel(self, parent):
        """As salas: o painel principal da janela (ocupa todo o espaco livre da coluna direita)."""
        panel = RoundFrame(parent, radius=20, padx=16, pady=14, grow=True)
        panel.pack(side="top", fill="both", expand=True, pady=(0, 13))
        inner = panel.inner

        top = tk.Frame(inner, bg=PANEL)
        top.pack(fill="x", pady=(0, 8))
        tk.Label(top, text=tr("rooms.title"), bg=PANEL, fg=TEXT, font=("Segoe UI", 14, "bold")).pack(side="left")
        self.rooms_status = tk.Label(top, text="", bg=PANEL, fg=DIM, font=("Segoe UI", 9), anchor="e",
                                     justify="right")
        self.rooms_status.pack(side="right")

        wrap = tk.Frame(inner, bg=PANEL)
        wrap.pack(fill="both", expand=True)
        sb = ttk.Scrollbar(wrap, orient="vertical")
        cols = (("guest", tr("col.guest"), 200, 150), ("ping", tr("col.ping"), 76, 70),
                ("status", tr("col.status"), 120, 110), ("time", tr("col.time"), 64, 60))
        self.rooms_tree = ttk.Treeview(wrap, columns=[c[0] for c in cols], show="tree headings", height=8,
                                       selectmode="browse",
                                       yscrollcommand=lambda lo, hi: self._rooms_scrolled(sb, lo, hi))
        sb.configure(command=self.rooms_tree.yview)
        self.rooms_tree.heading("#0", text=tr("col.host"), anchor="w")   # a coluna #0 leva o avatar + o nome do host
        self.rooms_tree.column("#0", width=230, anchor="w", stretch=True)
        for key, text, width, minw in cols:
            self.rooms_tree.heading(key, text=text, anchor="w")
            self.rooms_tree.column(key, width=width, minwidth=minw, anchor="w", stretch=False)
        sb.pack(side="right", fill="y")
        self.rooms_tree.pack(side="left", fill="both", expand=True)
        self.rooms_tree.bind("<<TreeviewSelect>>", lambda _e: self.pick_room())
        for ev in ("<<TreeviewSelect>>", "<Configure>", "<B1-Motion>", "<ButtonRelease-1>"):
            self.rooms_tree.bind(ev, lambda _e: self.after_idle(self._place_guest_avs), add="+")

        tk.Label(inner, text=tr("rooms.hint"), bg=PANEL, fg=DIM, font=("Segoe UI", 8), anchor="w",
                 justify="left", wraplength=640).pack(fill="x", pady=(8, 0))

    def _console(self, parent):
        panel = RoundFrame(parent, radius=18, padx=14, pady=10)
        panel.pack(fill="x")
        inner = panel.inner
        top = tk.Frame(inner, bg=PANEL)
        top.pack(fill="x", pady=(0, 6))
        tk.Label(top, text=tr("console"), bg=PANEL, fg=MUTED, font=("Segoe UI", 8, "bold")).pack(side="left")
        PillButton(top, text=tr("btn.clear"), style="Small.TButton", command=self.clear).pack(side="right")
        PillButton(top, text=tr("btn.copy"), style="Small.TButton", command=self.copy_console).pack(side="right",
                                                                                                   padx=(0, 6))
        PillButton(top, text=tr("btn.copy_cmd"), style="Small.TButton", command=self.copy_command).pack(
            side="right", padx=(0, 6))
        box = RoundFrame(inner, fill="#070a10", border=BORDER, radius=12, padx=6, pady=6, bg=PANEL)
        box.pack(fill="x")
        wrap = box.inner
        sb = ttk.Scrollbar(wrap, orient="vertical")
        self.console = tk.Text(wrap, bg="#070a10", fg="#c8d6ea", insertbackground=TEXT, selectbackground="#2a4a72",
                               relief="flat", wrap="word", font=("Cascadia Mono", 8), padx=8, pady=4, height=7,
                               state="disabled", yscrollcommand=sb.set, highlightthickness=0, bd=0)
        sb.configure(command=self.console.yview)
        sb.pack(side="right", fill="y")
        self.console.pack(side="left", fill="x", expand=True)
        for tag, color in (("error", "#ff8497"), ("success", "#62e2ac"), ("info", "#80b9ff"), ("warn", WARN),
                           ("dim", DIM)):
            self.console.tag_configure(tag, foreground=color)

    # ================================================================ dicas
    def _nick_hint(self):
        acc = self.backend.account
        if acc:
            shown = core.account_game_nick(acc)
            self.nick_hint.configure(text=acc["nick"], fg=TEXT)
            self.nick_sub.configure(text=tr("nick.sub_game", shown=shown) if shown else tr("nick.sub_default"))
        else:
            self.nick_hint.configure(text=tr("nick.none"), fg=MUTED)
            self.nick_sub.configure(text=tr("nick.none_sub"))

    def _sync_mode(self):
        guest = self.v["mode"].get() == "guest"
        (self.host_box.pack_forget() if guest else self.host_box.pack(fill="x"))
        (self.guest_box.pack(fill="x") if guest else self.guest_box.pack_forget())
        if hasattr(self, "start_btn"):
            self.start_btn.configure(text=tr("btn.start_guest") if guest else tr("btn.start_host"))

    def _ensure_state_dir(self) -> bool:
        """A pasta de save do PCSX2 e achada sozinha. So se isso falhar o launcher pergunta (uma vez, fica salva)."""
        if Path(self.cfg()["state_dir"] or "?").is_dir():
            return True
        messagebox.showinfo(tr("dlg.sstates_title"), tr("dlg.sstates_body"))
        path = filedialog.askdirectory(title=tr("dlg.sstates_pick"), initialdir=str(Path.home()))
        if not path:
            return False
        self.v["state_dir"].set(path)
        return True

    # ================================================================ salas
    def _avatar_image(self, url):
        """PhotoImage do avatar (baixado em segundo plano pelo backend) ou um espaco transparente enquanto isso."""
        img = self._avimgs.get(url)
        if img is not None:
            return img
        if url:
            img = make_avatar(self.backend.avatar_for(url))
            if img is not None:
                self._avimgs[url] = img
                return img
        return self.blank_av

    def _room_row(self, r):
        """(nome do host, avatar, valores das demais colunas, tag de cor) de uma sala."""
        players = r.get("players") or {}
        host = players.get("host") or {}
        guest = players.get("guest") or {}
        playing = r.get("status") == "playing"
        if playing:
            phase_key = "phase." + str(r.get("phase"))
            status = tr(phase_key) if has(phase_key) else tr("room.in_match")
        else:
            status = tr("room.open")
        ping = r.get("ping")
        ping_txt = f"{ping} ms" if (playing and ping is not None) else "—"
        elapsed = r.get("elapsed")
        if playing and elapsed is not None:               # o servidor manda o tempo no instante da resposta
            s = int(elapsed + time.monotonic() - r.get("fetched", time.monotonic()))
            time_txt = f"{s // 60:02d}:{s % 60:02d}"
        else:
            time_txt = "—"
        values = (GUEST_PAD + (guest.get("nick") or "—"), ping_txt, status.upper(), time_txt)
        return (host.get("nick") or r.get("nick") or "?", self._avatar_image(host.get("avatar_url")), values, ())

    def _guest_img(self, r):
        """Avatar do guest da sala, ou None se a sala nao tem guest."""
        guest = (r.get("players") or {}).get("guest") or {}
        if not guest.get("nick"):
            return None
        return self._avatar_image(guest.get("avatar_url"))

    def _rooms_scrolled(self, sb, lo, hi):
        sb.set(lo, hi)
        self.after_idle(self._place_guest_avs)

    def _place_guest_avs(self):
        """O Treeview so mostra imagem na coluna #0: o avatar do guest e um Label colocado sobre a celula GUEST
        (o texto da celula tem um recuo para deixar o espaco do avatar)."""
        tree = self.rooms_tree
        style = ttk.Style(self)
        normal = style.lookup("Treeview", "background") or PANEL
        picked = style.lookup("Treeview", "background", ("selected",)) or normal
        selected = set(tree.selection())
        height = tree.winfo_height()
        for iid, img in self.guest_imgs.items():
            box = tree.bbox(iid, "guest") if (img is not None and tree.exists(iid)) else ""
            lab = self.guest_labels.get(iid)
            if not box or box[1] + box[3] > height:
                if lab is not None:
                    lab.place_forget()
                continue
            x, y, _w, h = box
            if lab is None:
                lab = tk.Label(tree, bd=0, highlightthickness=0)
                lab.bind("<Button-1>", lambda _e, i=iid: tree.selection_set(i))
                self.guest_labels[iid] = lab
            lab.configure(image=img, bg=picked if iid in selected else normal)
            lab.image = img
            lab.place(x=x + 4, y=y + max(0, (h - ROW_AV) // 2))
        for iid in [i for i in self.guest_labels if i not in self.guest_imgs]:
            self.guest_labels.pop(iid).destroy()

    def _rooms_tick(self):
        try:
            self._rooms_refresh()
        except Exception as exc:                          # noqa: BLE001 - a lista nunca derruba o loop
            self._log(tr("log.rooms_err", exc=exc), "error")
        finally:
            self.after(500, self._rooms_tick)

    def _rooms_refresh(self):
        self.backend.poll_rooms()
        found, error = self.backend.online_rooms, self.backend.online_error
        self.rooms_data = {r["id"]: r for r in found}
        ids = [r["id"] for r in found]
        tree = self.rooms_tree
        if ids != list(tree.get_children()):              # so reconstroi se o conjunto de salas mudou
            selected = tree.selection()
            tree.delete(*tree.get_children())
            for r in found:
                tree.insert("", "end", iid=r["id"], text="")
            if selected and selected[0] in self.rooms_data:
                tree.selection_set(selected[0])
        for r in found:                                   # atualiza no lugar: nao pisca nem perde a selecao
            name, img, values, tags = self._room_row(r)
            tree.item(r["id"], text="  " + name, image=img, values=values, tags=tags)
        self.guest_imgs = {r["id"]: self._guest_img(r) for r in found}
        self._place_guest_avs()
        if error:
            text = tr("rooms.server_err", err=error)
        elif found:
            text = tr("rooms.count", n=len(found))
        else:
            text = tr("rooms.none")
        self.rooms_status.configure(text=text, fg=RED if error else DIM)

    def pick_room(self):
        """One click on a room: fills the room id and switches to guest. Nothing else to confirm."""
        if self.backend.running:
            return
        selected = self.rooms_tree.selection()
        room = self.rooms_data.get(selected[0]) if selected else None
        if room is None:
            return
        self.v["mode"].set("guest")
        self._sync_mode()
        self.v["room_id"].set(room["id"])

    def _picked_room(self):
        """The waiting room currently selected in the list, or None."""
        selected = self.rooms_tree.selection()
        room = self.rooms_data.get(selected[0]) if selected else None
        return room if room and room["status"] == "waiting" else None

    # ================================================================ console / status
    def _write(self, text, tag=None):
        self.console.configure(state="normal")
        self.console.insert("end", text.rstrip() + "\n", tag or ())
        self.console.see("end")
        self.console.configure(state="disabled")

    def _log(self, text, tag=None):
        self._history.append((text, tag))
        if len(self._history) > 2000:
            del self._history[:500]
        self._write(text, tag)

    def clear(self):
        self._history.clear()
        self.console.configure(state="normal")
        self.console.delete("1.0", "end")
        self.console.configure(state="disabled")

    def _clip(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)

    def copy_console(self):
        self._clip(self.console.get("1.0", "end"))

    def copy_command(self):
        self._clip(self.backend.last_cmd or tr("log.nothing_run"))

    def _set_status(self, text, color):
        """`text` e a chave do status (READY, IN MATCH, ...); o rotulo mostrado vem do idioma atual."""
        color = COLORS.get(color, color)                  # aceita nome do backend ou hex
        self.status_key, self.status_color = text, color
        key = "st." + text
        # o indicador visual foi removido do cabecalho

    def _set_buttons(self, running):
        state = "disabled" if running else "normal"
        for b in (self.load_btn, self.start_btn, self.solo_btn, self.pine_btn, self.release_btn):
            b.configure(state=state)
        # PREPARE: so o dono (o servidor decide quem e o dono; o launcher so esconde o botao)
        self.prepare_btn.configure(state="normal" if (not running and self.backend.is_admin()) else "disabled")
        self.stop_btn.configure(state="normal" if running else "disabled")

    # ================================================================ acoes
    def _spawn(self, args, label, allow_missing_symbols=False):
        try:
            self.backend.spawn(self.cfg(), args, allow_missing_symbols)
        except RuntimeError as exc:
            return messagebox.showwarning(tr("dlg.busy"), str(exc))
        except ValueError as exc:
            return messagebox.showerror(tr("dlg.cannot_start"), str(exc))
        except OSError as exc:
            return self._log(tr("log.could_not_launch", exc=exc), "error")
        self._log(f"\n▶ {label}", "info")
        self._set_status("STARTING", BLUE)
        self._set_buttons(True)

    def _args(self, action, state=None):
        try:
            return core.make_args(self.cfg(), action, state)
        except ValueError as exc:
            messagebox.showerror(tr("dlg.cannot_continue"), str(exc))
            return None

    def prepare(self):
        if not self.backend.is_admin():
            return messagebox.showinfo(tr("dlg.prepare"), tr("dlg.prepare_owner"))
        args = self._args("prepare")
        if args:
            self._spawn(args, tr("run.prepare"))

    def load_state(self):
        if not self._ensure_state_dir():
            return
        args = self._args("load", self.cfg()["state_file"])
        if args:
            self._spawn(args, tr("run.load"))

    def start(self):
        if not self._ensure_state_dir():
            return
        role = self.v["mode"].get()
        if role == "guest":
            room = self._picked_room()
            if room is None:
                return messagebox.showinfo(tr("dlg.rooms"), tr("dlg.pick_room"))
            self.v["room_id"].set(room["id"])
        # primeiro baixa o save state padrao do servidor (se o local for diferente); depois sobe a sessao
        self._after_sync = lambda: self._launch(role)
        self._log("\n▶ " + tr("run.checking"), "info")
        self._set_status("LOADING", BLUE)
        self._set_buttons(True)
        self.stop_btn.configure(state="disabled")
        self.backend.sync_default()

    def _launch(self, role):
        if role == "guest":                               # the server decides first (same IP / same account)
            self._log(tr("run.permission"), "info")
            self._set_status("LOADING", BLUE)
            self._set_buttons(True)
            self.stop_btn.configure(state="disabled")
            self._after_allow = lambda: self._launch_now(role)
            self.backend.check_join(self.v["room_id"].get())
            return
        self._launch_now(role)

    def _launch_now(self, role):
        args = self._args(role, self.cfg()["state_file"])
        if not args:
            self._set_status("READY", MUTED)
            return
        label = tr("run.host") if role == "host" else tr("run.guest")
        if role == "host" and self.v["ignore_desync"].get():
            label += tr("run.ignore_on")
        self._spawn(args, label, allow_missing_symbols=(role == "guest"))

    def solo(self):
        args = self._args("solo")
        if args:
            self._spawn(args, tr("run.solo", c=self.v["control"].get()))

    def stop(self):
        if not self.backend.running:
            return
        try:
            self.backend.request_stop()
        except OSError as exc:
            self._log(tr("log.stopfile", exc=exc), "error")
        self._log(tr("log.stopping"), "info")
        self.stop_btn.configure(state="disabled")
        self.after(4500, self._force)

    def _force(self):
        if self.backend.force_stop():
            self._log(tr("log.forced"), "warn")
            self.after(600, self.release_game)

    def release_game(self):
        if self.backend.running:
            return
        self._oneshot(["stop"], tr("run.releasing"), "release")

    def _oneshot(self, args, label, token):
        try:
            self.backend.oneshot(self.cfg(), args, token)
        except ValueError as exc:
            return messagebox.showerror(tr("dlg.cannot_run"), str(exc))
        self._log(f"\n▶ {label}", "info")
        self._set_buttons(True)
        self.stop_btn.configure(state="disabled")

    def test_pine(self):
        if self.backend.running:
            return
        try:
            self.backend.test_pine(self.cfg())
        except ValueError as exc:
            return messagebox.showerror(tr("dlg.pine_missing"), str(exc))
        self.pine_btn.configure(state="disabled")
        self._log("\n▶ " + tr("run.testing_pine"), "info")

    # ================================================================ eventos do backend
    def _drain(self):
        try:
            while True:
                ev = self.backend.q.get_nowait()
                kind = ev[0]
                if kind == "line":
                    self._on_line(ev[1])
                elif kind == "exit":
                    self._on_exit(ev[1])
                elif kind == "avatar":
                    pass                                  # a lista de salas pega o avatar pronto no proximo refresh
                elif kind == "pine":
                    ok = ev[1]
                    self._log(tr("log.pine_ok" if ok else "log.pine_fail") + (ev[2] or ""),
                              "success" if ok else "error")
                    self.pine_btn.configure(state="normal")
                elif kind == "acct":
                    self._on_acct(ev[1], ev[2], ev[3])
                elif kind == "sync":
                    cont, self._after_sync = self._after_sync, None
                    self._set_buttons(False)
                    if ev[1]:
                        if cont:
                            cont()
                    else:
                        self._set_status("READY", MUTED)
                        self._log(ev[2], "error")
                        messagebox.showerror(tr("dlg.default_state"), ev[2])
                elif kind == "allow":
                    cont, self._after_allow = self._after_allow, None
                    self._set_buttons(False)
                    if ev[1]:
                        if cont:
                            cont()
                    else:
                        self._set_status("READY", MUTED)
                        self._log(ev[2], "error")
                        messagebox.showerror(tr("dlg.cannot_join"), ev[2])
                elif kind == "publish":
                    self._log(tr("log.published") if ev[1] else tr("log.publish_fail", msg=ev[2]),
                              "success" if ev[1] else "error")
                elif kind == "oneshot":
                    self._log(ev[2] or tr("log.no_output"), "success" if ev[1] == 0 else "error")
                    self._set_buttons(False)
                    if ev[3] == "release":
                        self._set_status("READY", MUTED)
        except queue.Empty:
            pass
        except Exception as exc:                          # noqa: BLE001 - nunca derruba o loop
            self._log(tr("log.ui_err", exc=exc), "error")
        self.after(80, self._drain)

    def _on_line(self, line):
        tag, status = core.classify(line)
        self._log(line, tag)
        if status:
            self._set_status(*status)
        m = re.search(r"state saved: (.+\.p2s)\s*$", line)
        if m:
            self._adopt_saved_state(Path(m.group(1).strip()))

    def _adopt_saved_state(self, saved: Path):
        """O estado que o PREPARE acabou de salvar vira UN5-partida.p2s na pasta de saves do AppData e, se a conta
        logada for a do dono, e publicado no servidor como o save state padrao."""
        dest = core.STATE_PATH
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(saved, dest)
        except OSError as exc:
            return self._log(tr("log.copy_fail", dest=dest, exc=exc), "error")
        self._log(tr("log.copied", dest=dest), "success")
        if self.backend.is_admin():
            self._log(tr("log.publishing"), "info")
            self.backend.publish_default()

    def _on_exit(self, code):
        self._log(tr("log.finished", code=code), "success" if code in (0, None) else "error")
        self.backend.on_exit()
        self._set_buttons(False)
        if self.status_key not in ("DESYNC", "FROZEN"):
            self._set_status("READY", MUTED)

    def destroy(self):
        core.save_config({**self.raw(), "ignore_desync": False})   # o switch de debug nunca e salvo como ON
        self.backend.shutdown()
        super().destroy()


if __name__ == "__main__":
    Launcher().mainloop()