"""Kit visual da UI (tokens, PNG anti-aliased gerado com stdlib, fontes, controles no Canvas) e o overlay de legenda.
Sem dependências: tkinter + ctypes (Windows: cantos/vidro do DWM, ocultar da captura de tela)."""
from __future__ import annotations

import base64, math, re, struct, sys, zlib
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from types import SimpleNamespace

WIN, MAC = sys.platform == "win32", sys.platform == "darwin"
BUILD = sys.getwindowsversion().build if WIN else 0
ASSETS = Path(__file__).with_name("assets")

# tokens (relatório de UI: amostrados da referência; contraste sobre bg: text 17,7 · muted 8,1 · primary 5,0)
C = SimpleNamespace(bg="#0B0A18", shell="#13111F", s0="#15122B", s1="#1A1433", hair="#2A2545", hair_hi="#3E3670",
                    field="#100E1F", text="#F4F2FF", muted="#A6A3C9", subtle="#6E6B8F", primary="#7C6CF6",
                    ga="#2E5CEA", gb="#6D3FEA", t0="#4F6AF0", t1="#8B5CF6", eyebrow="#8B8DFF", sel="#2A2160",
                    sec="#C4B5FD", ok="#34D399", warn="#FBBF24", err="#F87171", info="#60A5FA", dl="#8B8DFF",
                    off="#2A2745", ov="#0F0D22")
SPK = ["#8B8DFF", "#2DD4BF", "#F472B6", "#FBBF24", "#60A5FA", "#A3E635", "#FB923C", "#C084FC"]  # presa ao id: id % 8
LEVEL = {"info": C.info, "ready": C.ok, "download": C.dl, "warn": C.warn, "error": C.err}
# ícones: Segoe Fluent Icons / MDL2 (traço fino, nada de emoji); fora do Windows, um glifo de texto equivalente
ICON = {"gear": ("\uE713", "≡"), "refresh": ("\uE72C", "↻"), "pin": ("\uE718", "⊤"), "main": ("\uE8A7", "⟲"),
        "stop": ("\uE71A", "■"), "arrow": ("\uE72A", "→"), "play": ("\uE768", "›"), "down": ("\uE70D", "⌄"),
        "plus": ("\uE710", "+"), "minus": ("\uE738", "−"), "sun": ("\uE706", "◐"), "check": ("\uE73E", "✓"),
        "doc": ("\uE8A5", "≡"), "back": ("\uE72B", "←"), "cc": ("\uE7F0", "≡"), "folder": ("\uE838", "□"),
        "audio": ("\uE767", "♪"), "globe": ("\uE774", "○"), "lock": ("\uE72E", "•")}


def caps(s):  # MAIÚSCULAS com espaçamento entre letras (hair space)
    return "\u200A".join(s.upper())


def spk_color(spk):
    return "#8E8BA8" if spk is None else SPK[spk % 8]


def _rgb(c):
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


def _mix(a, b, t):
    return tuple(x + (y - x) * t for x, y in zip(a, b))


def mix(a, b, t):
    """Cor hex entre a e b (t = 0..1)."""
    return "#%02x%02x%02x" % tuple(round(v) for v in _mix(_rgb(a), _rgb(b), t))


def shape(w, h, r, fill, border=None, glow=None, gm=0, horiz=False, bw=1.0, fa=1.0):
    """PNG RGBA de um retângulo arredondado anti-aliased (só stdlib). fill/border = (cor0, cor1) em gradiente vertical
    (horiz=True: horizontal); fa = opacidade do miolo; glow = (cor, alfa) esmaecendo numa margem de gm px."""
    W, H, r = w + 2 * gm, h + 2 * gm, min(r, w / 2, h / 2)
    f0, f1 = _rgb(fill[0]), _rgb(fill[1])
    b0, b1 = (_rgb(border[0]), _rgb(border[1])) if border else (None, None)
    gc, ga = (_rgb(glow[0]), glow[1]) if glow else ((0, 0, 0), 0.0)
    hw, hh = w / 2, h / 2

    def px(X, Y):
        x, y = X + .5 - gm, Y + .5 - gm
        qx, qy = abs(x - hw) - (hw - r), abs(y - hh) - (hh - r)
        d = min(max(qx, qy), 0.0) + math.hypot(max(qx, 0.0), max(qy, 0.0)) - r  # distância com sinal (< 0 = dentro)
        g = ga * max(0.0, 1 - max(d, 0.0) / gm) ** 2 if gm else 0.0
        cov = min(max(.5 - d, 0.0), 1.0)
        if not cov:
            return bytes((*gc, round(255 * g))) if g else b"\0\0\0\0"
        t = min(max(x / w if horiz else y / h, 0.0), 1.0)
        col, a = _mix(f0, f1, t), fa
        if b0:  # anel da borda: opaco por cima do miolo
            ring = 1 - min(max(.5 - (d + bw), 0.0), 1.0)
            col, a = _mix(col, _mix(b0, b1, t), ring) if a else _mix(b0, b1, t), a + (1 - a) * ring
        a *= cov
        out = a + g * (1 - a)  # "over" sobre o glow
        if out <= 0:
            return b"\0\0\0\0"
        return bytes((*(min(255, round((c * a + k * g * (1 - a)) / out)) for c, k in zip(col, gc)), round(255 * out)))

    a0, a1, rows = int(gm + r + 1), int(gm + w - r - 1), []
    for Y in range(H):  # gradiente vertical: o miolo da linha é constante (só as pontas são calculadas pixel a pixel)
        if horiz or a1 <= a0:
            rows.append(b"".join(px(X, Y) for X in range(W)))
        else:
            rows.append(b"".join(px(X, Y) for X in range(a0)) + px(W // 2, Y) * (a1 - a0)
                        + b"".join(px(X, Y) for X in range(a1, W)))
    ch = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return (b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))
            + ch(b"IDAT", zlib.compress(b"".join(b"\0" + r for r in rows), 6)) + ch(b"IEND", b""))


def load_fonts():
    """Outfit (OFL, app/assets) só para este processo. Chamar antes de criar o Tk. No mac vem do Info.plist do .app."""
    if WIN:
        try:
            import ctypes
            ctypes.windll.gdi32.AddFontResourceExW(str(ASSETS / "Outfit.ttf"), 0x10, 0)  # FR_PRIVATE
        except Exception:
            pass


def win_attr(win, attr, val):
    """DwmSetWindowAttribute (20 = modo escuro, 33 = cantos, 34 = cor da borda, 35 = cor da barra); ignora falhas."""
    try:
        import ctypes
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()
        return ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(ctypes.c_int(val)), 4) == 0
    except Exception:
        return False


def colorref(c):
    r, g, b = _rgb(c)
    return b << 16 | g << 8 | r


def dark_titlebar(win):
    if WIN:
        win.update_idletasks()
        for attr, val in ((20, 1), (35, colorref(C.bg)), (34, colorref(C.hair))):
            win_attr(win, attr, val)


class Kit:
    """Fontes, cache de imagens e controles desenhados num Canvas. Coordenadas em px "base" (96 dpi); o Kit escala."""

    def __init__(self, root):
        self.root, self.cache, self.n, self.binds = root, {}, 0, {}
        self.s = s = max(1.0, root.winfo_fpixels("1i") / 96) if WIN else 1.0
        fams = set(tkfont.families(root))
        sysf = tkfont.nametofont("TkDefaultFont", root=root).actual("family")
        out = "Outfit" in fams
        txt = next((f for f in ("Segoe UI Variable Text", "Segoe UI") if f in fams), sysf)
        txt_b = "Segoe UI Variable Text Semibold" if "Segoe UI Variable Text Semibold" in fams else txt
        self.ofam = ("Outfit SemiBold", "normal") if "Outfit SemiBold" in fams else (txt, "bold")
        self.tfam = txt
        self.icon = next((f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in fams), None)
        F = lambda fam, px, w="normal": tkfont.Font(root=root, family=fam, size=-round(px * s), weight=w)
        ob = lambda px: F("Outfit", px, "bold") if out else F(txt, px, "bold")
        osb = lambda px: F(self.ofam[0], px, self.ofam[1])
        self.f = {"display": ob(34), "num": ob(64), "big": ob(30), "h1": osb(26), "h2": osb(19), "logo": osb(19),
                  "btn": osb(15), "btn2": osb(14), "body": F(txt, 14), "bodyb": F(txt_b, 14, "bold" if txt_b == txt else "normal"),
                  "small": F(txt, 12), "eyebrow": F(txt_b, 11, "bold" if txt_b == txt else "normal"),
                  "lead": F(txt, 15), "icon": F(self.icon or txt, 15), "icon_s": F(self.icon or txt, 12),
                  "mock": osb(15), "mock_s": F(txt, 11)}
        self._sizes = {}

    def sized(self, kind, px):
        """Fontes da legenda por tamanho (cache): kind = "sub" (Outfit SemiBold) | "orig" | "name"."""
        key = (kind, round(px))
        if key not in self._sizes:
            fam, w = self.ofam if kind != "orig" else (self.tfam, "normal")
            self._sizes[key] = tkfont.Font(root=self.root, family=fam, size=-round(px * self.s), weight=w)
        return self._sizes[key]

    def glyph(self, name):
        return ICON[name][0 if self.icon else 1]

    def img(self, w, h, r, fill, border=None, glow=None, gm=0, horiz=False, fa=1.0):
        key = (round(w), round(h), r, fill, border, glow, gm, horiz, fa)
        if key not in self.cache:
            s = self.s
            data = shape(round(w * s), round(h * s), r * s, fill, border, glow, round(gm * s), horiz, max(1.0, round(s)), fa)
            self.cache[key] = tk.PhotoImage(master=self.root, data=base64.b64encode(data))
        return self.cache[key]

    def bind(self, cv, tag, seq, fn):
        """tag_bind que se desfaz no próximo reset(cv): redesenhar não acumula comandos Tcl."""
        self.binds.setdefault(cv, []).append((tag, seq, cv.tag_bind(tag, seq, fn)))

    def reset(self, cv):
        """Antes de redesenhar um canvas inteiro: apaga os itens e solta os binds da tela anterior."""
        for tag, seq, fid in self.binds.pop(cv, ()):
            try:
                cv.tag_unbind(tag, seq, fid)
            except tk.TclError:  # item já apagado (o Tk soltou o bind): só o comando Python sobrou
                cv.deletecommand(fid)
        cv.delete("all")

    def tag(self):
        self.n += 1
        return f"w{self.n}"

    def w(self, text, font):  # largura em px base
        return self.f[font].measure(text) / self.s if isinstance(font, str) else font.measure(text) / self.s

    def fit(self, text, font, maxw):
        if self.w(text, font) <= maxw:
            return text
        while text and self.w(text + "…", font) > maxw:
            text = text[:-1]
        return text.rstrip() + "…"

    # ---------- primitivas
    def text(self, cv, x, y, s, font="body", fill=C.text, anchor="nw", width=None, tags=(), justify="left"):
        f = self.f[font] if isinstance(font, str) else font
        kw = {"width": round(width * self.s)} if width else {}
        return cv.create_text(x * self.s, y * self.s, text=s, font=f, fill=fill, anchor=anchor, tags=tags, justify=justify, **kw)

    def image(self, cv, x, y, im, anchor="nw", tags=()):
        return cv.create_image(round(x * self.s), round(y * self.s), image=im, anchor=anchor, tags=tags)

    def bottom(self, cv, item):  # base de um item, em px base
        return cv.bbox(item)[3] / self.s

    def bg(self, cv, w, h):
        """Fundo tinta quase preto com dois orbs difusos (ovais concêntricos: degradê sem PNG gigante)."""
        s = self.s
        cv.create_rectangle(0, 0, w * s + 2, h * s + 2, fill=C.bg, outline="")
        for cx, cy, R, col, a in ((w * .94, -h * .12, w * .62, C.gb, .22), (w * .02, h * 1.08, w * .5, C.ga, .14)):
            for i in range(44, 0, -1):
                rr, t = R * i / 44 * s, a * (1 - i / 44) ** 1.7
                cv.create_oval(cx * s - rr, cy * s - rr, cx * s + rr, cy * s + rr, fill=mix(C.bg, col, t), outline="")

    def card(self, cv, x, y, w, h, active=False, tags=()):
        """Card "double-bezel": casca com borda fina + núcleo em gradiente com raio concêntrico; ativo = borda primary + glow."""
        gm = 18 if active else 0
        border = (C.primary, "#4C3BB0") if active else ("#2F2950", "#1E1A33")
        self.image(cv, x - gm, y - gm, self.img(w, h, 20, (C.shell, C.shell), border, (C.gb, .30) if active else None, gm),
                   tags=tags)
        self.image(cv, x + 6, y + 6, self.img(w - 12, h - 12, 14, (C.s0, C.s1), ("#2C2650", "#1B1733")), tags=tags)

    def eyebrow(self, cv, x, y, s, anchor="nw"):
        """Pill pequena em maiúsculas (tipo "BENEFITS" da referência)."""
        s = caps(s)
        w = self.w(s, "eyebrow") + 24
        x = x - w / 2 if anchor == "n" else x
        self.image(cv, x, y, self.img(w, 24, 12, (C.gb, C.gb), (mix(C.bg, C.gb, .45), mix(C.bg, C.gb, .45)), fa=.16))
        self.text(cv, x + w / 2, y + 12, s, "eyebrow", C.eyebrow, "center")
        return w

    def dot(self, cv, x, y, color, d=8, tags=()):
        return self.image(cv, x, y, self.img(d, d, d / 2, (color, color)), "center", tags)

    def bar(self, cv, x, y, w, frac, h=6, tags=()):
        self.image(cv, x, y, self.img(w, h, h / 2, (C.off, C.off)), tags=tags)
        if frac is not None:
            fw = max(h, round(w * max(0.0, min(1.0, frac)) / 2) * 2)
            self.image(cv, x, y, self.img(fw, h, h / 2, (C.ga, C.gb), horiz=True), tags=tags)

    def num(self, cv, x, y, s, font="num", anchor="nw", tags=()):
        """Número/sigla com "gradiente" t0 -> t1 caractere a caractere (o "01/02/03" da referência)."""
        x0 = x - self.w(s, font) / 2 if anchor == "n" else x
        for i, ch in enumerate(s):
            self.text(cv, x0, y, ch, font, mix(C.t0, C.t1, i / max(1, len(s) - 1)), tags=tags)
            x0 += self.w(ch, font)

    # ---------- controles
    def _press(self, cv, tg, item, imgs, cmd, text_item=None, colors=None):
        """Hover/pressed por troca de imagem; o comando roda no soltar, se o ponteiro ainda estiver em cima."""
        def state(i):
            cv.itemconfigure(item, image=imgs[min(i, len(imgs) - 1)])
            if text_item and colors:
                cv.itemconfigure(text_item, fill=colors[min(i, len(colors) - 1)])

        def release(e):
            cv.move(tg, 0, -self.s)
            x0, y0, x1, y1 = cv.bbox(tg)
            inside = x0 <= cv.canvasx(e.x) <= x1 and y0 <= cv.canvasy(e.y) <= y1
            state(1 if inside else 0)
            if inside:
                cv.after_idle(cmd)
        self.bind(cv, tg, "<Enter>", lambda e: state(1))
        self.bind(cv, tg, "<Leave>", lambda e: state(0))
        self.bind(cv, tg, "<ButtonPress-1>", lambda e: (state(2), cv.move(tg, 0, self.s)))
        self.bind(cv, tg, "<ButtonRelease-1>", release)

    def bw(self, label, kind="cta", h=44, icon=None):
        """Largura do botão (px base)."""
        lw = self.w(label, "btn" if kind == "cta" else "btn2")
        return 24 + lw + 12 + (h - 10) + 5 if kind == "cta" and icon else 22 + lw + 22 + (24 if icon else 0)

    def button(self, cv, x, y, label, cmd, kind="cta", h=44, icon=None, anchor="nw", w=None, tags=()):
        """Pill: "cta" (gradiente + glow + ícone num círculo aninhado), "ghost" (contorno), "danger", "link"."""
        font = "btn" if kind == "cta" else "btn2"
        lw = self.w(label, font)
        nest = kind == "cta" and icon
        w = w or self.bw(label, kind, h, icon)
        x = x - w if anchor == "ne" else x - w / 2 if anchor == "n" else x
        tg = self.tag()
        tags = (tg, "click", *tags)
        if kind == "link":
            t = self.text(cv, x + w / 2, y + h / 2, label, font, C.sec, "center", tags=tags)
            hit = cv.create_rectangle(x * self.s, y * self.s, (x + w) * self.s, (y + h) * self.s, outline="", fill="",
                                      tags=tags)
            cv.tag_lower(hit, t)
            self.bind(cv, tg, "<Enter>", lambda e: cv.itemconfigure(t, fill=C.text))
            self.bind(cv, tg, "<Leave>", lambda e: cv.itemconfigure(t, fill=C.sec))
            self.bind(cv, tg, "<ButtonRelease-1>", lambda e: cv.after_idle(cmd))
            return x, w
        if kind == "cta":
            gm = 14
            imgs = [self.img(w, h, h / 2, f, b, (C.gb, .28), gm, True) for f, b in (
                ((C.ga, C.gb), None), (("#3D6AF0", "#7B4FF0"), (mix(C.gb, "#8B8DFF", .6),) * 2),
                (("#2550D0", "#5F33D6"), None))]
            colors = ["#FFFFFF"]
        else:
            gm, c = 0, C.err if kind == "danger" else C.gb
            imgs = [self.img(w, h, h / 2, (c, c), (c, c), fa=a) for a in (0.0, .12, .2)]
            colors = [C.err if kind == "danger" else C.sec, C.text]
        item = self.image(cv, x - gm, y - gm, imgs[0], tags=tags)
        if nest:
            t = self.text(cv, x + 24, y + h / 2, label, font, colors[0], "w", tags=tags)
            self.image(cv, x + w - 5 - (h - 10), y + 5, self.img(h - 10, h - 10, (h - 10) / 2, ("#FFFFFF",) * 2, fa=.16),
                       tags=tags)
            self.text(cv, x + w - 5 - (h - 10) / 2, y + h / 2, self.glyph(icon), "icon_s", "#FFFFFF", "center", tags=tags)
        else:
            tx = x + w / 2 + (12 if icon else 0)
            t = self.text(cv, tx, y + h / 2, label, font, colors[0], "center", tags=tags)
            if icon:
                self.text(cv, tx - lw / 2 - 10, y + h / 2, self.glyph(icon), "icon_s", colors[0], "e", tags=tags)
        self._press(cv, tg, item, imgs, cmd, t, colors)
        return x, w

    def icon_button(self, cv, x, y, name, cmd, size=36, active=False, label=None, tags=()):
        """Botão quadrado arredondado com um ícone (ou um rótulo curto, ex.: "A+")."""
        w = max(size, self.w(label, "btn2") + 20) if label else size
        tg = self.tag()
        tags = (tg, "click", *tags)
        fill = (C.sel, C.sel) if active else ("#1C1934", "#1C1934")
        brd = (C.primary,) * 2 if active else (C.hair_hi, C.hair_hi)
        imgs = [self.img(w, size, size * .32, fill, brd), self.img(w, size, size * .32, ("#262144",) * 2, (C.primary,) * 2),
                self.img(w, size, size * .32, (C.sel,) * 2, (C.primary,) * 2)]
        item = self.image(cv, x, y, imgs[0], tags=tags)
        t = self.text(cv, x + w / 2, y + size / 2, label or self.glyph(name), "btn2" if label else "icon_s",
                      C.text if active else C.muted, "center", tags=tags)
        self._press(cv, tg, item, imgs, cmd, t, [C.text if active else C.muted, C.text])
        return w

    def seg(self, cv, x, y, w, opts, cur, cmd, h=40):
        """Controle segmentado: trilho `field` + pastilha selecionada `sel` com borda primary."""
        tg, n = self.tag(), len(opts)
        sw = (w - 8) // n
        w = sw * n + 8
        self.image(cv, x, y, self.img(w, h, h / 2, (C.field, C.field), (C.hair, C.hair)), tags=(tg, "click"))
        for i, (val, lab) in enumerate(opts):
            cx = x + 4 + sw * i
            if val == cur:
                self.image(cv, cx, y + 4, self.img(sw, h - 8, (h - 8) / 2, (C.sel, C.sel), (C.primary, C.primary)),
                           tags=(tg, "click"))
            self.text(cv, cx + sw / 2, y + h / 2, self.fit(lab, "bodyb", sw - 12), "bodyb", C.text if val == cur else C.muted,
                      "center", tags=(tg, "click"))

        def click(e):
            i = int((cv.canvasx(e.x) / self.s - x - 4) // sw)
            if 0 <= i < n and opts[i][0] != cur:
                cv.after_idle(cmd, opts[i][0])
        self.bind(cv, tg, "<Button-1>", click)
        return w

    def toggle(self, cv, x, y, on, cmd):
        tg = self.tag()
        self.image(cv, x, y, self.img(44, 24, 12, (C.ga, C.gb) if on else (C.off, C.off),
                                      None if on else (C.hair_hi, C.hair_hi), horiz=True), tags=(tg, "click"))
        self.image(cv, x + (23 if on else 3), y + 3, self.img(18, 18, 9, ("#FFFFFF", "#E4E0FF")), tags=(tg, "click"))
        self.bind(cv, tg, "<Button-1>", lambda e: cv.after_idle(cmd, not on))

    def field(self, cv, x, y, w, label, cmd, h=40):
        """Dropdown: caixa `field` + chevron; cmd(x_root, y_root) abre o menu embaixo."""
        tg = self.tag()
        imgs = [self.img(w, h, 10, (C.field, C.field), (b, b)) for b in (C.hair, C.hair_hi, C.primary)]
        item = self.image(cv, x, y, imgs[0], tags=(tg, "click"))
        self.text(cv, x + 14, y + h / 2, self.fit(label, "body", w - 52), "body", C.text, "w", tags=(tg, "click"))
        self.text(cv, x + w - 16, y + h / 2, self.glyph("down"), "icon_s", C.muted, "e", tags=(tg, "click"))
        go = lambda: cmd(cv.winfo_rootx() + round(x * self.s), cv.winfo_rooty() + round((y + h + 4) * self.s))
        self._press(cv, tg, item, imgs, go)

    def menu(self):
        return tk.Menu(self.root, tearoff=0, bg=C.shell, fg=C.text, activebackground=C.sel, activeforeground="#FFFFFF",
                       disabledforeground=C.subtle, bd=0, relief="flat", font=self.f["body"], activeborderwidth=0,
                       selectcolor=C.primary)

    def entry(self, cv, x, y, w, value, font, done):
        """Campo de texto sobre o canvas (renomear). done(texto | None): Enter/clicar fora = ok, Esc = cancela."""
        e = tk.Entry(cv, bg=C.field, fg=C.text, insertbackground=C.text, relief="flat", font=font, highlightthickness=1,
                     highlightbackground=C.primary, highlightcolor=C.primary, selectbackground=C.sel)
        e.insert(0, value)
        e.select_range(0, "end")
        cv.create_window(x * self.s, y * self.s, window=e, anchor="nw", width=round(w * self.s))
        fired = []

        def fin(ok):
            if not fired:
                fired.append(1)
                val = e.get().strip()
                e.destroy()
                done(val if ok and val else None)
        e.bind("<Return>", lambda ev: (fin(True), "break")[1])  # "break": o Enter/Esc da janela não dispara junto
        e.bind("<Escape>", lambda ev: (fin(False), "break")[1])
        e.bind("<FocusOut>", lambda ev: fin(True))
        cv.winfo_toplevel().focus_force()
        e.focus_set()
        return e

    def hand(self, cv):
        """Cursor de mão sobre itens clicáveis."""
        cv.bind("<Motion>", lambda e: cv.configure(cursor="hand2" if "click" in cv.gettags("current") else ""), add="+")


# ---------------------------------------------------------------- overlay de legenda
EDGE = 8  # px de borda sensível ao redimensionar
CURSOR = {"n": "sb_v_double_arrow", "s": "sb_v_double_arrow", "e": "sb_h_double_arrow", "w": "sb_h_double_arrow",
          "nw": "size_nw_se", "se": "size_nw_se", "ne": "size_ne_sw", "sw": "size_ne_sw"}
ALPHAS = (1.0, .88, .75, .6)


class Overlay:
    """Janela sem moldura com as últimas falas (1 a 3), estilo legenda de cinema. Arrasta pelo meio, redimensiona pelas
    8 bordas, controles só no hover. Os dados vêm do App (`app.recent`, `app.ov_label`, `app.ov_body`)."""
    PAD, TOP = 22, 44

    def __init__(self, app):
        self.app, self.k = app, app.kit
        self.win = w = tk.Toplevel(app.root)
        w.withdraw()
        w.overrideredirect(True)
        w.minsize(360, 110)
        self.cv = cv = tk.Canvas(w, highlightthickness=0, bd=0, bg=C.ov)
        cv.pack(fill="both", expand=True)
        self.hover = self.editing = False
        self.drag = self.anim = self.last = None
        self.glass = False
        self.blink = 0
        cv.bind("<Motion>", self._motion)
        cv.bind("<ButtonPress-1>", self._press)
        cv.bind("<B1-Motion>", self._drag)
        cv.bind("<ButtonRelease-1>", self._release)
        cv.bind("<Double-Button-1>", lambda e: None if self._on_item() else self.snap())
        cv.bind("<Button-2>" if MAC else "<Button-3>", self._menu)  # clique direito (no mac o Tk chama de 2)
        cv.bind("<MouseWheel>", self._wheel)
        cv.bind("<Enter>", lambda e: self._hover(True))
        cv.bind("<Leave>", lambda e: w.after(250, self._left))
        cv.bind("<Configure>", lambda e: self.redraw())
        for keys, f in ((("<Control-plus>", "<Control-equal>", "<Control-KP_Add>"), lambda: self.font(+2)),
                        (("<Control-minus>", "<Control-KP_Subtract>"), lambda: self.font(-2)),
                        (("<Escape>",), app.open_main)):
            for key in keys:
                w.bind(key, lambda e, f=f: f())

    @property
    def o(self):
        return self.app.ov

    # ---------- janela
    def show(self):
        w, s = self.win, self.k.s
        if not w.winfo_ismapped():
            m = re.fullmatch(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", self.o.get("geometry") or "")
            gw, gh, gx, gy = map(int, m.groups()) if m else (0, 0, 0, 0)
            vx, vy, vw, vh = w.winfo_vrootx(), w.winfo_vrooty(), w.winfo_vrootwidth(), w.winfo_vrootheight()
            if m and vx - gw + 120 < gx < vx + vw - 120 and vy - 20 < gy < vy + vh - 60:
                w.geometry(f"{gw}x{gh}+{gx}+{gy}")
            else:  # 1ª vez ou monitor removido: padrão, embaixo no centro
                w.geometry(f"{round(900 * s)}x{round(210 * s)}")
                w.update_idletasks()
                self.snap(save=False)
            w.deiconify()
        w.update_idletasks()
        self.style()
        self.redraw()
        if not self.blink:
            self._blink()

    def hide(self):
        self.win.withdraw()

    def hwnd(self):
        import ctypes
        return ctypes.windll.user32.GetParent(self.win.winfo_id()) or self.win.winfo_id()

    @staticmethod
    def _accent(hwnd, state, color):
        """Vidro acrylic (SetWindowCompositionAttribute, não documentada). Devolve True se o Windows aceitou."""
        import ctypes
        from ctypes import c_int, c_size_t, c_uint, c_void_p

        class ACCENT(ctypes.Structure):
            _fields_ = [("state", c_int), ("flags", c_int), ("color", c_uint), ("anim", c_int)]

        class DATA(ctypes.Structure):
            _fields_ = [("attr", c_int), ("data", c_void_p), ("size", c_size_t)]
        ac = ACCENT(state, 2, color, 0)
        data = DATA(19, ctypes.cast(ctypes.byref(ac), c_void_p), ctypes.sizeof(ac))
        return bool(ctypes.windll.user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(data)))

    def style(self):
        """topmost, cantos e borda do DWM, vidro (com fallback para -alpha) e "ocultar da captura"."""
        o, w = self.o, self.win
        w.attributes("-topmost", bool(o["topmost"]))
        self.glass = False
        if WIN:
            win_attr(w, 33, 2)  # cantos arredondados nativos (Win11)
            win_attr(w, 34, colorref(C.hair_hi))
            try:
                h = self.hwnd()
                r, g, b = _rgb("#0D0B1E")
                tint = round(max(.3, o["alpha"] - .25) * 255) << 24 | b << 16 | g << 8 | r  # 88 % -> tinta de 63 %
                self.glass = bool(o["glass"]) and self._accent(h, 4, tint)
                if not self.glass:
                    self._accent(h, 0, 0)
                import ctypes
                ctypes.windll.user32.SetWindowDisplayAffinity(h, 0x11 if o["hide_capture"] else 0)  # WDA_EXCLUDEFROMCAPTURE
            except Exception:
                self.glass = False
        w.attributes("-alpha", 1.0 if self.glass else o["alpha"])  # no vidro, a opacidade vai na tinta (texto 100 %)
        self.cv.configure(bg="#000000" if self.glass else C.ov)  # preto = transparente sobre o acrylic

    def workarea(self):
        w = self.win
        if WIN:
            try:
                import ctypes
                from ctypes import wintypes

                class MI(ctypes.Structure):
                    _fields_ = [("cb", wintypes.DWORD), ("mon", wintypes.RECT), ("work", wintypes.RECT),
                                ("flags", wintypes.DWORD)]
                mi = MI()
                mi.cb = ctypes.sizeof(MI)
                mon = ctypes.windll.user32.MonitorFromWindow(self.hwnd(), 2)
                if ctypes.windll.user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
                    r = mi.work
                    return r.left, r.top, r.right - r.left, r.bottom - r.top
            except Exception:
                pass
        return 0, 0, w.winfo_screenwidth(), w.winfo_screenheight()

    def snap(self, save=True):
        """Duplo clique: encaixa embaixo, no centro do monitor atual."""
        w = self.win
        mx, my, mw, mh = self.workarea()
        ww, wh = w.winfo_width(), w.winfo_height()
        w.geometry(f"+{mx + (mw - ww) // 2}+{my + mh - wh - round(56 * self.k.s)}")
        if save:
            w.after(50, self._keep)

    def _keep(self):
        self.o["geometry"] = self.win.geometry()
        self.app.save()

    # ---------- mouse: mover, redimensionar, hover
    def _edge(self, e):
        w, s = self.win, self.k.s
        x, y, ww, hh = e.x_root - w.winfo_rootx(), e.y_root - w.winfo_rooty(), w.winfo_width(), w.winfo_height()
        b = EDGE * s
        return ("n" if y < b else "s" if y > hh - b else "") + ("w" if x < b else "e" if x > ww - b else "")

    def _on_item(self):
        return "click" in self.cv.gettags("current")

    def _motion(self, e):
        if self.drag:
            return
        ed = self._edge(e)
        self.cv.configure(cursor=CURSOR.get(ed) or ("hand2" if self._on_item() else "fleur"))
        if not self.hover:
            self._hover(True)

    def _press(self, e):
        self.win.focus_force()  # atalhos de teclado (Ctrl +/−, Esc); com o campo de renomear aberto, o FocusOut confirma
        if self._on_item() and not self._edge(e):
            self.drag = None
            return
        w = self.win
        self.drag = (self._edge(e), e.x_root, e.y_root, w.winfo_x(), w.winfo_y(), w.winfo_width(), w.winfo_height())

    def _drag(self, e):
        if not self.drag:
            return
        ed, x0, y0, x, y, ww, hh = self.drag
        dx, dy = e.x_root - x0, e.y_root - y0
        mw, mh = round(360 * self.k.s), round(110 * self.k.s)
        if not ed:
            x, y = x + dx, y + dy
        if "e" in ed:
            ww = max(mw, ww + dx)
        if "s" in ed:
            hh = max(mh, hh + dy)
        if "w" in ed:
            nw = max(mw, ww - dx)
            x, ww = x + ww - nw, nw
        if "n" in ed:
            nh = max(mh, hh - dy)
            y, hh = y + hh - nh, nh
        self.win.geometry(f"{ww}x{hh}+{x}+{y}")

    def _release(self, e):
        if self.drag:
            self.drag = None
            self._keep()

    def _hover(self, on):
        if on != self.hover:
            self.hover = on
            self.redraw()

    def _left(self):
        if self.editing or not self.win.winfo_ismapped():
            return
        x, y = self.win.winfo_pointerxy()
        w = self.win
        if not (w.winfo_rootx() <= x < w.winfo_rootx() + w.winfo_width() and w.winfo_rooty() <= y < w.winfo_rooty() + w.winfo_height()):
            self._hover(False)

    # ---------- ações
    def font(self, d):
        self.o["font"] = max(16, min(56, self.o["font"] + d))
        self.redraw()
        self.app.save()

    def alpha(self, a=None, d=0):
        cur = self.o["alpha"]
        a = a if a is not None else round(max(.6, min(1.0, cur + d)), 2)
        self.o["alpha"] = a
        self.style()
        self.redraw()
        self.app.save()

    def toggle(self, key):
        self.o[key] = not self.o[key]
        self.style()
        self.redraw()
        self.app.save()

    def _wheel(self, e):  # roda sobre a opacidade = opacidade; Ctrl + roda = fonte
        d = 1 if e.delta > 0 else -1
        if "alpha" in self.cv.gettags("current"):
            self.alpha(d=.05 * d)
        elif e.state & 4:
            self.font(2 * d)

    def _cycle_alpha(self):
        cur = self.o["alpha"]
        nxt = next((a for a in ALPHAS if a < cur - .01), ALPHAS[0])
        self.alpha(nxt)

    def _menu(self, e):
        a, m = self.app, self.k.menu()
        tags = self.cv.gettags("current")
        spk = next((int(t[4:]) for t in tags if t.startswith("spk:")), None)
        if spk is not None:
            a.speaker_menu(m, spk)
            m.add_separator()
        T = a.t
        m.add_command(label=T("open_main"), command=a.open_main)
        show, top = tk.BooleanVar(m, self.o["show_orig"]), tk.BooleanVar(m, self.o["topmost"])
        m.add_checkbutton(label=T("show_orig"), variable=show, command=lambda: self.toggle("show_orig"))
        m.add_checkbutton(label=T("topmost"), variable=top, command=lambda: self.toggle("topmost"))
        lines = tk.Menu(m, tearoff=0, bg=C.shell, fg=C.text, activebackground=C.sel, font=self.k.f["body"],
                        selectcolor=C.primary)
        var = tk.IntVar(m, self.o["lines"])
        for n in (1, 2, 3):
            lines.add_radiobutton(label=str(n), value=n, variable=var, command=lambda n=n: a.set_ov("lines", n))
        m.add_cascade(label=T("lines"), menu=lines)
        m.add_command(label=T("snap"), command=self.snap)
        m.add_separator()
        m.add_command(label=T("transcript"), command=a.show_transcript)
        m.add_command(label=T("save_txt"), command=lambda: a.save_transcript("txt"))
        m.add_command(label=T("save_srt"), command=lambda: a.save_transcript("srt"))
        m.add_separator()
        m.add_command(label=T("stop_sub"), command=a.stop)
        m.add_command(label=T("quit"), command=a.close)
        m.tk_popup(e.x_root, e.y_root)

    def rename(self, spk, item):
        if self.editing:
            return
        x0, y0, x1, y1 = (v / self.k.s for v in self.cv.bbox(item))
        self.editing = True

        def done(name):
            self.editing = False
            if name and name != self.app.name(spk):  # clicar fora sem mudar nada não fixa a voz
                self.app.rename(spk, name)
            self.redraw()
        # depois do _press do canvas (roda logo após o bind do item e põe o foco na janela): o campo fica com o foco
        self.win.after_idle(self.k.entry, self.cv, x0 - 4, y0 - 4, max(200, x1 - x0 + 60), self.app.name(spk),
                            self.k.sized("name", max(13, self.o["font"] * .5)), done)

    def _blink(self):
        """Ponto "ao vivo" pulsando enquanto captura (troca de imagem a cada 0,7 s)."""
        self.blink += 1
        if self.win.winfo_ismapped() and self.cv.find_withtag("live"):
            col, live = self.app.ov_dot()
            on = not live or self.blink % 2
            self.cv.itemconfigure("live", image=self.k.img(10, 10, 5, ((col if on else mix(col, C.ov, .55)),) * 2))
        self.win.after(700, self._blink)

    # ---------- desenho
    def redraw(self):
        if self.editing or not self.win.winfo_ismapped():
            return
        if self.anim:
            self.win.after_cancel(self.anim)
            self.anim = None
        cv, k, o, a, s = self.cv, self.k, self.o, self.app, self.k.s
        k.reset(cv)
        W, H = cv.winfo_width() / s, cv.winfo_height() / s
        if W < 200:
            return
        P, bgc = self.PAD, ("#000000" if self.glass else C.ov)
        wrap = W - 2 * P
        blocks = a.recent(o["lines"])
        fs = o["font"]
        f_sub, f_orig, f_name = k.sized("sub", fs), k.sized("orig", max(12, fs * .55)), k.sized("name", max(12, fs * .5))
        while True:
            y, slide, fade, cut = H - 14, 0.0, [], 0
            for i, ln in enumerate(reversed(blocks)):  # do mais novo (embaixo) para o mais antigo
                tg, top = ("blk", f"b{i}"), y
                dim = .5 if i else 0.0
                sub = ln.sub or ln.orig
                orig = ln.orig if o["show_orig"] and ln.orig and ln.orig != sub else ""
                col = C.text if ln.final else C.muted  # parcial em muted -> final em text
                items = []
                if orig:
                    it = k.text(cv, P, y, orig, f_orig, mix(C.muted, bgc, dim), "sw", wrap, tg)
                    items.append((it, C.muted))
                    y = cv.bbox(it)[1] / s - 2
                it = k.text(cv, P, y, sub, f_sub, mix(col, bgc, dim), "sw", wrap, tg)
                items.append((it, col))
                y = cv.bbox(it)[1] / s - 4
                older = blocks[len(blocks) - 2 - i] if i < len(blocks) - 1 else None
                if older is None or older.spk != ln.spk:
                    live = ln.spk is not None and ln.key[0] == a.epoch  # sessão anterior: o id já pode ser outra pessoa
                    tags = (*tg, "click", f"spk:{ln.spk}") if live else tg
                    it = k.text(cv, P, y, a.name(ln.spk).upper(), f_name, mix(spk_color(ln.spk), bgc, dim), "sw", tags=tags)
                    items.append((it, spk_color(ln.spk)))
                    if live:
                        k.bind(cv, it, "<Button-1>", lambda e, sp=ln.spk, it=it: self.rename(sp, it))
                    y = cv.bbox(it)[1] / s
                y -= 12
                if i and y < self.TOP - 8:  # bloco antigo que não cabe: sai, e o mais antigo que sobrou ganha o nome
                    cut = i
                    break
                if i == 0:
                    slide = top - y
                elif i == 1:
                    fade = items
            if not cut:
                break
            cv.delete("blk")
            blocks = blocks[-cut:]
        if blocks and cv.bbox("blk") and cv.bbox("blk")[1] < self.TOP * s:  # a fala atual passou do topo: máscara
            cv.create_rectangle(0, 0, W * s, self.TOP * s, fill=bgc, outline="")
        if not blocks:
            text, frac, col = a.ov_body()
            it = k.text(cv, P, H / 2 + 4, text, k.sized("sub", max(15, fs * .62)), col, "w", wrap)
            if frac is not False:
                k.bar(cv, P, k.bottom(cv, it) + 10, min(320, wrap), frac)
        self._header(W, bgc)
        key = blocks[-1].key if blocks else None
        if key != self.last and self.last is not None and len(blocks) > 1 and slide:
            self._slide(slide, fade, bgc)  # fala nova: a anterior sobe e esmaece
        self.last = key

    def _slide(self, dy, fade, bgc, steps=6):
        s = self.k.s
        self.cv.move("blk", 0, dy * s)
        for it, c in fade:
            self.cv.itemconfigure(it, fill=c)

        def step(i):
            self.cv.move("blk", 0, -dy * s / steps)
            for it, c in fade:
                self.cv.itemconfigure(it, fill=mix(c, bgc, .5 * i / steps))
            self.anim = self.win.after(25, step, i + 1) if i < steps else None
        self.anim = self.win.after(16, step, 1)

    def _header(self, W, bgc):
        cv, k, a, P = self.cv, self.k, self.app, self.PAD
        col, _ = a.ov_dot()
        k.image(cv, P + 5, 22, k.img(10, 10, 5, (col, col)), "center", ("live",))
        label, lcol = a.ov_label()
        if not self.hover:
            k.text(cv, P + 18, 22, k.fit(caps(label), "eyebrow", W - 2 * P - 24), "eyebrow",
                   mix(lcol, bgc, .15), "w")
            return
        o, x, y = self.o, W - P, 9  # controles, da direita para a esquerda
        for name, cmd, act, lab in (("stop", a.stop, False, None), ("main", a.open_main, False, None)):
            x -= 30
            k.icon_button(cv, x, y, name, cmd, 30, act, lab)
            x -= 6
        x -= 10
        for name, cmd, act, lab in (("pin", lambda: self.toggle("topmost"), o["topmost"], None),
                                    ("sun", self._cycle_alpha, False, f"{round(o['alpha'] * 100)} %"),
                                    (None, lambda: self.font(+2), False, "A+"), (None, lambda: self.font(-2), False, "A−")):
            wd = max(30, k.w(lab, "btn2") + 20) if lab else 30
            x -= wd
            k.icon_button(cv, x, y, name, cmd, 30, act, lab, tags=("alpha",) if name == "sun" else ())
            x -= 6
        k.text(cv, P + 18, 24, k.fit(caps(label), "eyebrow", x - P - 30), "eyebrow", lcol, "w")


def contrast(a, b):
    """Razão de contraste WCAG entre duas cores hex."""
    def lum(c):
        v = [x / 255 for x in _rgb(c)]
        v = [x / 12.92 if x <= .03928 else ((x + .055) / 1.055) ** 2.4 for x in v]
        return .2126 * v[0] + .7152 * v[1] + .0722 * v[2]
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + .05) / (lo + .05)


if __name__ == "__main__":  # check: contraste dos textos (>= 4,5:1) e o PNG gerado com stdlib
    for fg in (C.text, C.muted, C.eyebrow, C.sec, C.err, C.ok, C.warn, C.info, spk_color(None), *SPK):
        for bg in (C.bg, C.s1, C.ov, C.field):
            assert contrast(fg, bg) >= 4.5, (fg, bg, round(contrast(fg, bg), 2))
    assert contrast(C.text, C.sel) >= 4.5 and contrast("#FFFFFF", C.ga) >= 4.5 and contrast("#FFFFFF", C.gb) >= 4.5  # texto do CTA
    tint = mix("#FFFFFF", "#0D0B1E", .63)  # vidro a 88 % (tinta de 63 %) sobre uma página branca
    assert contrast(C.text, tint) >= 4.5, round(contrast(C.text, tint), 2)
    png = shape(40, 20, 10, (C.ga, C.gb), (C.hair, C.hair), (C.gb, .3), 6, True)
    assert png[:4] == bytes((137, 80, 78, 71)) and struct.unpack(">II", png[16:24]) == (52, 32)
    raw = zlib.decompress(png[png.index(b"IDAT") + 4:-16])
    assert len(raw) == 32 * (1 + 52 * 4) and raw[1 + 4 * 0 + 3] == 0  # canto do glow: transparente
    print("ok", round(contrast(C.muted, C.bg), 1), round(contrast(C.text, tint), 1))
