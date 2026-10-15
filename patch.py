#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Patch 2 (rode DEPOIS do patch.py), na pasta do launcher:
    python patch2.py

launcher_theme.py
  - RoundFrame.refresh(): recalcula tamanho e redesenha o card sem depender de eventos <Configure>.
  - Segmented/Switch/RoundScale ignoram callbacks de variavel quando o widget ja foi destruido
    (sobravam do _rebuild ao trocar de idioma).
launcher.py
  - _relayout(): refaz todos os cards, de dentro para fora, com o tamanho real (funciona maximizado).
  - Cards 1 e 2 viram um card so.
  - PREPARAR PARTIDA e Ferramentas so aparecem para a conta do dono (admin).

Cria launcher.py.bak2 e launcher_theme.py.bak2. Se ja estiver aplicado, nao faz nada.
"""
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAUNCHER = HERE / "launcher.py"
THEME = HERE / "launcher_theme.py"


def load(path):
    if not path.is_file():
        sys.exit(f"Nao achei {path}")
    raw = path.read_bytes().decode("utf-8")
    return raw.replace("\r\n", "\n"), "\r\n" in raw


def save(path, src, crlf):
    shutil.copy2(path, path.with_name(path.name + ".bak2"))
    path.write_bytes((src.replace("\n", "\r\n") if crlf else src).encode("utf-8"))


def replace_once(src, old, new, what):
    if src.count(old) != 1:
        sys.exit(f"Falhou: {what} nao encontrado (arquivo diferente do esperado).")
    return src.replace(old, new)


def regex_once(src, pattern, new, what):
    out, n = re.subn(pattern, lambda _m: new, src, count=1, flags=re.S)
    if n != 1:
        sys.exit(f"Falhou: {what} nao encontrado (arquivo diferente do esperado).")
    return out


# ======================================================================================================================
# launcher_theme.py
# ======================================================================================================================
GUARD = "        if not self.winfo_exists():\n            return\n"

REFRESH = '''    def refresh(self):
        """Recalcula o tamanho natural e redesenha com o tamanho atual do canvas, sem esperar o <Configure>."""
        self.inner.update_idletasks()
        self._request()
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        if w > 4 and h > 4:
            self._on_canvas(type("E", (), {"width": w, "height": h})())

'''


def patch_theme():
    src, crlf = load(THEME)
    if "Recalcula o tamanho natural e redesenha" in src:
        return None
    src = replace_once(src, '    def refresh(self):\n        self.delete("all")\n',
                       '    def refresh(self):\n' + GUARD + '        self.delete("all")\n', "Segmented.refresh")
    src = replace_once(src, '    def _draw(self):\n        self.delete("all")\n        _reset_aa(self)\n        on = bool(',
                       '    def _draw(self):\n' + GUARD + '        self.delete("all")\n        _reset_aa(self)\n        on = bool(',
                       "Switch._draw")
    src = replace_once(src, '    def _draw(self):\n        self.delete("all")\n        _reset_aa(self)\n        x0, x1, _w = self._span()',
                       '    def _draw(self):\n' + GUARD + '        self.delete("all")\n        _reset_aa(self)\n        x0, x1, _w = self._span()',
                       "RoundScale._draw")
    src = replace_once(src, "    def _tiles(self, r):", REFRESH + "    def _tiles(self, r):", "RoundFrame._tiles")
    return src, crlf


# ======================================================================================================================
# launcher.py
# ======================================================================================================================
LEFT_COLUMN = '''        left = tk.Frame(body, bg=BG, width=360)
        left.pack(side="left", fill="y", padx=(0, 18))
        left.pack_propagate(False)
        self._card_main(left)
'''

RELAYOUT = '''    def _relayout(self):
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

'''

CARD_MAIN = '''    # ---- card unico: jogador + partida + executar (o nick vem do Discord: nao ha campo)
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
        Segmented(inner, [("host", "HOST  \u00b7  P1"), ("guest", "GUEST  \u00b7  P2")], self.v["mode"],
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
        self.tools_toggle = tk.Label(self.admin_box, text="\u25b8 " + tr("tools"), bg=PANEL, fg=ACCENT,
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

'''


def patch_launcher():
    src, crlf = load(LAUNCHER)
    if "def _card_main(self, parent):" in src:
        return None
    if "def _relayout(self):" not in src:
        sys.exit("Rode o patch.py antes (o launcher.py ainda nao tem o _relayout).")

    src = regex_once(src, r'        left = tk\.Frame\(body, bg=BG, width=360\)\n.*?        self\._card_match\(top\)\n',
                     LEFT_COLUMN, "coluna esquerda do _build")
    src = regex_once(src, r'    # ---- card 1: jogador \+ partida.*?(?=    def _toggle_tools\(self\):)',
                     CARD_MAIN, "cards 1 e 2")
    src = regex_once(src, r'    def _relayout\(self\):.*?(?=    def _build\(self\):)', RELAYOUT, "_relayout")
    src = replace_once(src, "        self.after(80, self._relayout)\n",
                       "        self.after(80, self._relayout)\n        self.after(300, self._relayout)\n",
                       "agendamento do _relayout")
    src, n = re.subn(r'(        self\._nick_hint\(\)\n)(        self\._set_buttons\(self\.backend\.running\)[^\n]*\n)',
                     lambda m: m.group(1) + "        self._sync_admin()\n" + m.group(2), src, count=1)
    if n != 1:
        sys.exit("Falhou: _refresh_account nao encontrado.")
    return src, crlf


def main():
    theme = patch_theme()
    launcher = patch_launcher()
    if theme is None and launcher is None:
        print("Patch 2 ja aplicado. Nada a fazer.")
        return
    if theme is not None:
        save(THEME, *theme)
        print("OK: launcher_theme.py atualizado (backup .bak2).")
    if launcher is not None:
        save(LAUNCHER, *launcher)
        print("OK: launcher.py atualizado (backup .bak2).")


if __name__ == "__main__":
    main()