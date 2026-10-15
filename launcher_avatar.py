# -*- coding: utf-8 -*-
"""Launcher - FRONTEND: editor de recorte do avatar (arrastar + zoom, estilo Discord).

Uso: png = crop_avatar(janela_pai, caminho_da_imagem)
    -> bytes PNG quadrado valido (pronto para o servidor), ou None se o usuario cancelou.
    Sem Pillow instalado, cai no comportamento antigo (core.prepare_avatar: so aceita um PNG ja valido).
"""
from __future__ import annotations

import io
import tkinter as tk

import launcher_core as core
from launcher_theme import ACCENT, BG, DIM, MUTED, PANEL, TEXT, PillButton, RoundFrame, RoundScale

try:
    from PIL import Image, ImageDraw, ImageOps, ImageTk
    RES = getattr(Image, "Resampling", Image).LANCZOS
    BIL = getattr(Image, "Resampling", Image).BILINEAR
    AFFINE = getattr(Image, "Transform", Image).AFFINE
except ImportError:                                           # sem Pillow: crop_avatar usa o caminho antigo
    Image = None

OUT_SIZES = (256, 192, 128, 96, 64, 48, 32)                   # tenta do maior (mais nitido) ate caber no limite


class CropDialog(tk.Toplevel):
    V, D, PREV = 340, 288, 72                                  # janela, diametro do circulo, preview

    def __init__(self, parent, img):
        super().__init__(parent)
        self.title("Adjust avatar")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(parent)
        self.result = None

        # imagem de trabalho: lado menor limitado a ~600 px (arrastar fica fluido)
        k = min(1.0, 600 / min(img.size), 2400 / max(img.size))
        if k < 1:
            img = img.resize((max(1, round(img.width * k)), max(1, round(img.height * k))), RES)
        self.img, self.w, self.h = img, img.width, img.height
        self.smin = self.D / min(self.w, self.h)               # menor escala: o circulo sempre fica coberto
        self.zoom = tk.DoubleVar(value=1.0)
        self._z_prev = 1.0
        self.cx = self.cy = self.V / 2                         # centro da imagem na janela
        self._drag = None
        self._overlay = self._make_overlay()
        self._photo = self._prev_photo = None

        card = RoundFrame(self, fill=PANEL, radius=16, padx=18, pady=16, bg=BG)
        card.pack(fill="both", expand=True, padx=14, pady=14)
        body = card.inner
        tk.Label(body, text="ADJUST AVATAR", bg=PANEL, fg=TEXT, font=("Segoe UI", 14, "bold")).pack(anchor="w")
        tk.Label(body, text="Drag to move  ·  slider or mouse wheel to zoom", bg=PANEL, fg=DIM,
                 font=("Segoe UI", 8)).pack(anchor="w", pady=(2, 10))

        self.canvas = tk.Canvas(body, width=self.V, height=self.V, bg=PANEL, highlightthickness=0, bd=0,
                                cursor="fleur")
        self.canvas.pack()
        self.canvas.bind("<ButtonPress-1>", self._grab)
        self.canvas.bind("<B1-Motion>", self._move)
        self.canvas.bind("<MouseWheel>", lambda e: self._wheel(1 if e.delta > 0 else -1))
        self.canvas.bind("<Button-4>", lambda _e: self._wheel(1))
        self.canvas.bind("<Button-5>", lambda _e: self._wheel(-1))

        row = tk.Frame(body, bg=PANEL)
        row.pack(fill="x", pady=(12, 0))
        tk.Label(row, text="–", bg=PANEL, fg=MUTED, font=("Segoe UI", 12, "bold")).pack(side="left")
        RoundScale(row, 1.0, 5.0, self.zoom, self._on_zoom).pack(side="left", fill="x", expand=True, padx=8)
        tk.Label(row, text="+", bg=PANEL, fg=MUTED, font=("Segoe UI", 12, "bold")).pack(side="left")

        foot = tk.Frame(body, bg=PANEL)
        foot.pack(fill="x", pady=(14, 0))
        self.prev = tk.Label(foot, bg=PANEL, bd=0)
        self.prev.pack(side="left")
        tk.Label(foot, text="Preview", bg=PANEL, fg=DIM, font=("Segoe UI", 8)).pack(side="left", padx=(8, 0))
        PillButton(foot, "APPLY", self.apply, "Accent.TButton").pack(side="right")
        PillButton(foot, "CANCEL", self.close).pack(side="right", padx=(0, 8))

        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda _e: self.close())
        self.bind("<Return>", lambda _e: self.apply())
        self._render()
        card.sync()
        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        try:
            self.grab_set()
        except tk.TclError:
            pass
        self.focus_set()

    # ---------------------------------------------------------------- geometria
    @property
    def s(self):
        return self.smin * self.zoom.get()

    def _clamp(self):
        vc, mx = self.V / 2, max(0.0, self.w * self.s / 2 - self.D / 2)
        my = max(0.0, self.h * self.s / 2 - self.D / 2)
        self.cx = min(max(self.cx, vc - mx), vc + mx)
        self.cy = min(max(self.cy, vc - my), vc + my)

    def _box(self):
        """Quadrado do circulo, em coordenadas da imagem de trabalho (sempre dentro dela)."""
        s, off = self.s, (self.V - self.D) / 2
        l = (off - self.cx) / s + self.w / 2
        t = (off - self.cy) / s + self.h / 2
        side = self.D / s
        l = min(max(0.0, l), max(0.0, self.w - side))
        t = min(max(0.0, t), max(0.0, self.h - side))
        return (l, t, min(self.w, l + side), min(self.h, t + side))

    # ---------------------------------------------------------------- eventos
    def _grab(self, e):
        self._drag = (e.x, e.y, self.cx, self.cy)

    def _move(self, e):
        if self._drag:
            x0, y0, cx0, cy0 = self._drag
            self.cx, self.cy = cx0 + e.x - x0, cy0 + e.y - y0
            self._clamp()
            self._render()

    def _wheel(self, direction):
        self.zoom.set(min(5.0, max(1.0, self.zoom.get() + 0.15 * direction)))
        self._on_zoom()

    def _on_zoom(self, *_):
        z = self.zoom.get()
        ratio, vc = z / self._z_prev, self.V / 2
        self.cx, self.cy = vc + (self.cx - vc) * ratio, vc + (self.cy - vc) * ratio   # zoom em torno do centro
        self._z_prev = z
        self._clamp()
        self._render()

    # ---------------------------------------------------------------- desenho
    def _make_overlay(self):
        V, D, k = self.V, self.D, 4
        lo, hi = (V - D) / 2 * k, (V + D) / 2 * k
        hole = Image.new("L", (V * k, V * k), 0)
        ImageDraw.Draw(hole).ellipse((lo, lo, hi, hi), fill=255)
        hole = hole.resize((V, V), RES)
        dark = Image.new("RGBA", (V, V), (6, 9, 15, 255))
        dark.putalpha(hole.point(lambda p: round(185 * (255 - p) / 255)))
        ring = Image.new("L", (V * k, V * k), 0)
        ImageDraw.Draw(ring).ellipse((lo, lo, hi, hi), outline=255, width=2 * k)
        ring_img = Image.new("RGBA", (V, V), (255, 138, 53, 255))
        ring_img.putalpha(ring.resize((V, V), RES))
        dark.alpha_composite(ring_img)
        return dark

    def _render(self):
        s, V = self.s, self.V
        l = (0 - self.cx) / s + self.w / 2
        t = (0 - self.cy) / s + self.h / 2
        view = self.img.transform((V, V), AFFINE, (1 / s, 0, l, 0, 1 / s, t), BIL)
        base = Image.new("RGBA", (V, V), (17, 24, 35, 255))
        base.alpha_composite(view)
        base.alpha_composite(self._overlay)
        self._photo = ImageTk.PhotoImage(base.convert("RGB"))
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo)
        self._render_preview()

    def _render_preview(self):
        p, k = self.PREV, 4
        crop = self.img.resize((p, p), RES, box=self._box())
        mask = Image.new("L", (p * k, p * k), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, p * k - 1, p * k - 1), fill=255)
        bg = Image.new("RGBA", (p, p), (17, 24, 35, 255))
        bg.paste(crop, (0, 0), mask.resize((p, p), RES))
        self._prev_photo = ImageTk.PhotoImage(bg.convert("RGB"))
        self.prev.configure(image=self._prev_photo)

    # ---------------------------------------------------------------- fim
    def apply(self):
        box = self._box()
        for px in OUT_SIZES:                                   # o maior tamanho que o servidor aceita
            out = io.BytesIO()
            self.img.resize((px, px), RES, box=box).save(out, "PNG", optimize=True)
            if core.png_ok(out.getvalue()):
                self.result = out.getvalue()
                break
        self.close()

    def close(self):
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()


def crop_avatar(parent, path):
    """Abre o editor de recorte. Devolve PNG (bytes) ou None se cancelado. Levanta ValueError se nao for imagem."""
    if Image is None:                                          # sem Pillow: nao ha como recortar
        return core.prepare_avatar(path)
    try:
        img = Image.open(path)
        img.load()
        img = ImageOps.exif_transpose(img).convert("RGBA")
    except Exception:                                          # noqa: BLE001
        raise ValueError("That file is not a readable image.") from None
    dlg = CropDialog(parent, img)
    parent.wait_window(dlg)
    return dlg.result