"""Interface do TranslateAPP (tkinter puro, sem dependências): 1ª abertura (idioma do app + tutorial), tela principal
(fonte de áudio e idiomas), overlay de legenda (app/ui_overlay.py), transcrição e configurações. Não importa
audio/pipeline/diar: recebe callbacks. `python -m app.ui --demo [--first-run]` roda com dados falsos;
`--stress` faz rajadas de updates + checagens (lógica das linhas, overlay, resize, vidro)."""
from __future__ import annotations

import itertools, json, logging, os, queue, re, subprocess, sys, threading, time
import tkinter as tk
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from random import Random
from tkinter import filedialog, font as tkfont, ttk
from types import SimpleNamespace

from app.events import Status, Update
from app.paths import DATA_DIR, SETTINGS
from app.ui_i18n import STR, system_lang, t as tr
from app.ui_overlay import C, LEVEL, MAC, WIN, Kit, Overlay, caps, dark_titlebar, load_fonts, mix, spk_color

log = logging.getLogger(__name__)
MAX_LINES = 2000      # utterances no widget da transcrição; as mais antigas saem da tela (continuam no arquivo salvo)
EXTRA_ID = 1_000_000  # utt_id das linhas extras de um final dividido (= app.pipeline.EXTRA_ID; a UI não importa o pipeline)
BUDGET = 0.012        # s por tick processando updates: uma rajada nunca prende a UI
DEF = {"call_lang": "auto", "sub_lang": "pt", "source": "all", "device": None, "audio_app": None, "main_geometry": None}
CHOICES = {"call_lang": ("auto", "en", "pt"), "sub_lang": ("pt", "en"), "source": ("all", "app")}
OV_DEF = {"geometry": None, "font": 28, "alpha": 0.88, "topmost": True,
          "show_orig": True, "lines": 2, "hide_capture": WIN}
SIZES = {"lang": (760, 520), "tour": (760, 520), "main": (960, 680), "settings": (960, 680)}


def _load():
    """settings.json (dono único: a UI). Chaves antigas são ignoradas; valores inválidos voltam ao padrão."""
    try:
        raw = json.loads(SETTINGS.read_text("utf-8"))
        raw = raw if isinstance(raw, dict) else {}
    except Exception:
        raw = {}
    cfg = {k: raw.get(k, v) for k, v in DEF.items()}
    for k, ok in CHOICES.items():
        cfg[k] = cfg[k] if cfg[k] in ok else DEF[k]
    ov = dict(OV_DEF)
    if isinstance(raw.get("overlay"), dict):
        ov.update({k: v for k, v in raw["overlay"].items() if k in OV_DEF})
    try:
        ov["font"], ov["alpha"] = max(16, min(56, int(ov["font"]))), max(.6, min(1.0, float(ov["alpha"])))
        ov["lines"] = max(1, min(3, int(ov["lines"])))
    except (TypeError, ValueError):
        ov.update(font=OV_DEF["font"], alpha=OV_DEF["alpha"], lines=OV_DEF["lines"])
    sp = raw.get("speakers") if isinstance(raw.get("speakers"), dict) else {}
    names = {int(k): str(v) for k, v in sp.items() if str(k).isdigit() and v}
    return cfg, ov, names, raw.get("ui_lang") if raw.get("ui_lang") in ("en", "pt") else None


def _app_name(app_id):  # "Discord.exe" -> "Discord"; "com.hnc.Discord" -> "Discord"
    return app_id[:-4] if app_id.lower().endswith(".exe") else app_id.rsplit(".", 1)[-1]


class App:
    def __init__(self, out_q, *, get_devices, get_apps, get_speakers, on_start, on_stop, on_speaker, autostart=False):
        self.q, self.get_devices, self.get_apps, self.get_speakers = out_q, get_devices, get_apps, get_speakers
        self.on_start, self.on_stop, self.on_speaker = on_start, on_stop, on_speaker
        self.ctl = ThreadPoolExecutor(1, thread_name_prefix="ui-ctl")
        if WIN:
            try:
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sem isso o Windows estica a janela (texto borrado)
            except Exception:
                pass
        load_fonts()
        self.cfg, self.ov, self.names, lang = _load()
        self.has_lang, self.lang = bool(lang), lang or system_lang()
        self.root = r = tk.Tk()
        r.title("TranslateAPP")
        r.configure(bg=C.bg)
        r.resizable(False, False)
        self.kit = k = Kit(r)
        self._ico = ico = k.img(64, 64, 16, (C.ga, C.gb), horiz=True).copy()  # ícone: gradiente + 2 linhas de legenda
        for box in ((14, 34, 50, 39), (14, 44, 40, 49)):
            ico.put("#FFFFFF", to=tuple(round(v * k.s) for v in box))
        r.iconphoto(True, ico)
        self.cv = tk.Canvas(r, bg=C.bg, highlightthickness=0, bd=0)
        self.cv.pack(fill="both", expand=True)
        k.hand(self.cv)
        self.cv.bind("<MouseWheel>", lambda e: self._scroll_apps(-1 if e.delta > 0 else 1)
                     if "applist" in self.cv.gettags("current") else None)
        # linhas da transcrição: (época, utt_id) -> linha (lista ligada na ordem de chegada)
        self.lines, self.arquivo, self.tail = {}, [], None
        self.lags, self.busy, self.job = deque(maxlen=10), 0.0, None
        self.epoch = self.n = self.done = self.app_off = 0  # a época sobe a cada início (o utt_id pode reiniciar em 0)
        self.running = self.ready = self.dirty = self.det_dirty = False
        self.follow, self.screen, self.step, self.section = True, None, 0, 0
        self.devices, self.apps, self.saved, self.seen, self.alias = [], [], set(), {}, {}
        self.detected = self.started = None
        self.st, self.lag_txt = ("info", self.t("st_idle"), None, None), ""  # (nível, texto, progresso, ação)
        self._build_transcript()
        self.overlay = Overlay(self)
        r.protocol("WM_DELETE_WINDOW", self.close)
        r.report_callback_exception = lambda *exc: log.error("callback do Tk", exc_info=exc)  # no log, não só no stderr
        r.bind("<Escape>", lambda e: self._esc())
        r.bind("<Return>", lambda e: self._enter())
        self._devices()
        self.show("main" if lang else "lang")
        self._place()
        dark_titlebar(r)
        self._bg(get_speakers, then=self._got_speakers, fail=lambda m: log.warning("get_speakers: %s", m))
        if self.cfg["source"] == "app":
            self.refresh_apps()
        if autostart and lang:
            r.after(300, self.start)

    def t(self, key, **kw):
        return tr(self.lang, key, **kw)

    # ---------- preferências e janela
    def save(self):
        r = self.root
        if self.screen and r.state() == "normal":
            self.cfg["main_geometry"] = f"+{r.winfo_x()}+{r.winfo_y()}"
        data = {**self.cfg, "overlay": self.ov, "speakers": {str(k): v for k, v in self.names.items()}}
        if self.has_lang:
            data["ui_lang"] = self.lang
        try:
            SETTINGS.parent.mkdir(parents=True, exist_ok=True)  # empacotado: a pasta de dados pode não existir ainda
            SETTINGS.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")
        except OSError:
            pass

    def _place(self):
        r, g = self.root, self.cfg["main_geometry"] or ""
        r.update_idletasks()
        m = re.fullmatch(r"\+(-?\d+)\+(-?\d+)", g)
        vx, vy, vw, vh = r.winfo_vrootx(), r.winfo_vrooty(), r.winfo_vrootwidth(), r.winfo_vrootheight()
        if m and vx - 200 < int(m[1]) < vx + vw - 200 and vy - 20 < int(m[2]) < vy + vh - 120:
            r.geometry(g)
        else:  # primeira vez ou monitor removido: centraliza no principal
            r.geometry(f"+{(r.winfo_screenwidth() - r.winfo_width()) // 2}+{(r.winfo_screenheight() - r.winfo_height()) // 3}")

    def show(self, screen):
        self.screen = screen
        w, h = SIZES[screen]
        self.root.geometry(f"{round(w * self.kit.s)}x{round(h * self.kit.s)}")
        self.render()

    def render(self):
        cv = self.cv
        self.kit.reset(cv)
        w, h = SIZES[self.screen]
        self.kit.bg(cv, w, h)
        getattr(self, "_scr_" + self.screen)(w, h)

    def _esc(self):
        if self.screen == "settings":
            self.show("main")
        elif self.screen == "tour":
            self._tour_end()
        elif self.screen == "main" and self.running:
            self.resume()

    def _enter(self):
        if isinstance(self.root.focus_get(), tk.Entry):
            return
        {"lang": self._lang_ok, "tour": self._tour_next, "main": self.resume if self.running else self.start,
         "settings": lambda: None}[self.screen]()

    # ---------- 1ª abertura: idioma do app e tutorial
    def _scr_lang(self, w, h):
        k, cv = self.kit, self.cv
        k.eyebrow(cv, w / 2, 46, "TranslateAPP", "n")
        k.text(cv, w / 2, 88, STR["en"]["pick_title"], "display", C.text, "n")
        k.text(cv, w / 2, 132, STR["pt"]["pick_title"], "display", C.eyebrow, "n")
        for i, code in enumerate(("en", "pt")):
            x, y, cw, ch = w / 2 - 312 + i * 324, 214, 300, 168
            tg, on = k.tag(), self.lang == code
            k.card(cv, x, y, cw, ch, on, tags=(tg, "click"))
            k.num(cv, x + 30, y + 26, code.upper(), "big", tags=(tg, "click"))
            k.text(cv, x + 30, y + 78, "English" if code == "en" else "Português", "h2", tags=(tg, "click"))
            k.text(cv, x + 30, y + 110, tr(code, f"lang_{code}_sub"), "body", C.muted, tags=(tg, "click"))
            if on:
                k.image(cv, x + cw - 34, y + 34, k.img(28, 28, 14, (C.ga, C.gb), horiz=True), "center", (tg, "click"))
                k.text(cv, x + cw - 34, y + 34, k.glyph("check"), "icon_s", "#FFFFFF", "center", tags=(tg, "click"))
            k.bind(cv, tg, "<Button-1>", lambda e, c=code: cv.after_idle(self._pick_lang, c))
        k.button(cv, w / 2, 424, self.t("continue"), self._lang_ok, "cta", 48, "arrow", "n")

    def _pick_lang(self, code):
        self._tr_status(code)
        self.lang = code
        self.render()

    def _lang_ok(self):
        self.has_lang = True
        self.save()
        self.step = 0
        self.show("tour")

    def _scr_tour(self, w, h):
        k, cv, last = self.kit, self.cv, self.step == 3
        title, body = self.t("tour")[self.step]
        k.eyebrow(cv, 48, 44, self.t("how"))
        k.button(cv, 712, 41, self.t("skip"), self._tour_end, "link", 30, anchor="ne")
        k.num(cv, 46, 86, f"{self.step + 1:02d}")
        y = k.bottom(cv, k.text(cv, 48, 182, title, "h1", C.text, width=330))
        k.text(cv, 48, y + 12, body, "lead", C.muted, width=330)
        k.card(cv, 412, 86, 300, 316)
        self._mock(self.step, 442, 118, 240)
        x = 48
        for i in range(4):  # pontinhos de progresso
            on = i == self.step
            k.image(cv, x, 450, k.img(24 if on else 8, 8, 4, (C.ga, C.gb) if on else (C.hair_hi,) * 2, horiz=True))
            x += 32 if on else 16
        bx, _ = k.button(cv, w - 48, 432, self.t("begin" if last else "next"), self._tour_next, "cta", 44, "arrow", "ne")
        if self.step:
            k.button(cv, bx - 12, 432, self.t("back"), lambda: self._tour_go(-1), "ghost", 44, anchor="ne")

    def _mock(self, step, x, y, w):
        """Ilustração de cada passo do tutorial, com os próprios controles da UI."""
        k, cv, nop = self.kit, self.cv, lambda *a: None
        if step == 0:
            k.text(cv, x, y, self.t("audio_h"), "h2")
            k.seg(cv, x, y + 40, w, [("all", self.t("src_all")), ("app", self.t("src_app"))], "app", nop, 36)
            for i, (name, act) in enumerate((("Discord", True), ("Google Chrome", True), ("Spotify", False))):
                yy = y + 96 + i * 40
                if i == 0:
                    k.image(cv, x, yy, k.img(w, 34, 10, (C.sel,) * 2, (C.primary,) * 2))
                k.dot(cv, x + 16, yy + 17, C.ok if act else C.subtle)
                k.text(cv, x + 30, yy + 17, name, "bodyb", C.text if i == 0 else C.muted, "w")
                k.text(cv, x + w - 12, yy + 17, self.t("playing" if act else "silent"), "small", C.muted, "e")
        elif step == 1:
            k.text(cv, x, y, self.t("call_lang"), "small", C.muted)
            k.seg(cv, x, y + 22, w, [("auto", self.t("auto")), ("en", "EN"), ("pt", "PT")], "auto", nop, 36)
            k.text(cv, x, y + 82, self.t("sub_lang"), "small", C.muted)
            k.seg(cv, x, y + 104, w, [("pt", "PT"), ("en", "EN")], "pt", nop, 36)
            k.dot(cv, x + 5, y + 176, C.ok)
            k.text(cv, x + 16, y + 176, self.t("detected", lang=self.t("lang_name")["en"]), "small", C.muted, "w",
                   width=w - 16)
        elif step == 2:
            pt = self.lang == "pt"
            k.image(cv, x - 8, y + 8, k.img(w + 16, 176, 12, (C.ov, "#141130"), (C.hair_hi, C.hair)))
            k.dot(cv, x + 8, y + 28, C.ok)
            k.text(cv, x + 18, y + 28, caps(self.t("live") + " · Discord"), "eyebrow", C.muted, "w")
            k.text(cv, x + 8, y + 46, "BRUNO", "mock_s", mix(spk_color(1), C.ov, .5))
            k.text(cv, x + 8, y + 62, "Pode ser." if pt else "Sounds good.", "mock", mix(C.text, C.ov, .5))
            k.text(cv, x + 8, y + 94, "ANA", "mock_s", spk_color(0))
            yy = k.bottom(cv, k.text(cv, x + 8, y + 110, "Acho que dá para entregar na sexta." if pt
                                     else "I think we can ship it on Friday.", "mock", C.text, width=w - 16))
            k.text(cv, x + 8, yy + 4, "I think we can ship it on Friday." if pt else "Acho que dá para entregar na sexta.",
                   "mock_s", C.muted, width=w - 16)
            for cx, cy in ((x - 8, y + 8), (x + w + 8, y + 8), (x - 8, y + 184), (x + w + 8, y + 184)):
                k.dot(cv, cx, cy, C.primary, 10)
            bx = x + w / 2 - 78
            for name, lab, on in ((None, "A−", False), (None, "A+", False), ("sun", None, False), ("pin", None, True)):
                bx += k.icon_button(cv, bx, y + 212, name, nop, 30, on, lab) + 6
        else:
            for i, (name, n) in enumerate((("Ana", 0), ("Bruno", 1), (self.t("speaker", n=3), 2))):
                yy = y + 10 + i * 50
                k.dot(cv, x + 6, yy + 12, spk_color(n), 10)
                k.text(cv, x + 22, yy + 12, name, "mock", spk_color(n), "w")
                if i == 0:
                    k.icon_button(cv, x + w - 92, yy - 4, None, nop, 30, label=self.t("rename_btn"))
            k.image(cv, x, y + 196, k.img(w, 44, 22, (C.field,) * 2, (C.hair,) * 2))
            k.text(cv, x + 18, y + 218, k.glyph("lock"), "icon_s", C.ok, "w")
            k.text(cv, x + 42, y + 218, self.t("tour_local"), "bodyb", C.text, "w")

    def _tour_go(self, d):
        self.step = max(0, min(3, self.step + d))
        self.render()

    def _tour_next(self):
        self._tour_go(1) if self.step < 3 else self._tour_end()

    def _tour_end(self):
        self.show("main")

    # ---------- tela principal
    def _header(self, w, back=False):
        k, cv = self.kit, self.cv
        k.image(cv, 32, 28, k.img(36, 36, 11, (C.ga, C.gb), horiz=True))
        k.text(cv, 50, 46, k.glyph("cc"), "icon_s", "#FFFFFF", "center")
        k.text(cv, 80, 46, "TranslateAPP", "logo", C.text, "w")
        if back:
            k.button(cv, w - 32, 26, self.t("back"), lambda: self.show("main"), "ghost", 40, "back", "ne")
            return
        k.icon_button(cv, w - 32 - 40, 26, "gear", lambda: self._settings(0), 40)
        k.seg(cv, w - 32 - 40 - 12 - 116, 26, 116, [("en", "EN"), ("pt", "PT")], self.lang, self._set_lang)

    def _scr_main(self, w, h):
        k, cv, c = self.kit, self.cv, self.cfg
        self._header(w)
        k.text(cv, 32, 90, self.t("tagline"), "h1")
        k.text(cv, 32, 128, self.t("tagline_sub"), "body", C.muted)
        # card 01 · áudio
        x, y, iw = 62, 168, 380
        k.card(cv, 32, y, 440, 320)
        k.eyebrow(cv, x, y + 28, self.t("card_audio"))
        k.text(cv, x, y + 62, self.t("audio_h"), "h2")
        yy = y + 100
        if self.get_apps is not None:
            k.seg(cv, x, yy, iw, [("all", self.t("src_all")), ("app", self.t("src_app"))], c["source"], self._source)
            yy += 56
        if c["source"] == "all" or self.get_apps is None:
            self._device_row(x, yy, iw)
        else:
            k.text(cv, x, yy, self.t("apps"), "small", C.muted)
            k.icon_button(cv, x + iw - 28, yy - 6, "refresh", self.refresh_apps, 28)
            self._app_list(x, yy + 30, iw)
        # card 02 · idiomas
        x = 518
        k.card(cv, 488, y, 440, 320)
        k.eyebrow(cv, x, y + 28, self.t("card_lang"))
        k.text(cv, x, y + 62, self.t("lang_h"), "h2")
        k.text(cv, x, y + 100, self.t("call_lang"), "small", C.muted)
        k.seg(cv, x, y + 122, iw, [("auto", self.t("auto")), ("en", self.t("English")), ("pt", self.t("Português"))],
              c["call_lang"], lambda v: self._set("call_lang", v))
        k.text(cv, x, y + 178, self.t("sub_lang"), "small", C.muted)
        k.seg(cv, x, y + 200, iw, [("pt", self.t("Português")), ("en", self.t("English"))], c["sub_lang"],
              lambda v: self._set("sub_lang", v))
        det = self.detected
        k.dot(cv, x + 4, y + 278, C.ok if det else C.subtle)
        k.text(cv, x + 16, y + 278, self.t("detected", lang=self.t("lang_name")[det]) if det else self.t("detected_none"),
               "small", C.muted, "w", width=iw - 16)
        self._draw_status()
        # botões
        if not self.running:
            bts = [(self.t("start"), self.start, "cta", "play"), (self.t("transcript"), self.show_transcript, "ghost", "doc")]
        else:
            changed = self._params() != self.started
            bts = [(self.t("apply" if changed else "show_sub"), self.resume, "cta", "arrow"),
                   (self.t("stop"), self.stop, "ghost", "stop"), (self.t("transcript"), self.show_transcript, "ghost", "doc")]
        ws = [k.bw(lab, kind, 48 if kind == "cta" else 44, ic) for lab, _, kind, ic in bts]
        bx = (w - sum(ws) - 12 * (len(ws) - 1)) / 2
        for (lab, cmd, kind, ic), bw in zip(bts, ws):
            k.button(cv, bx, 592 if kind == "cta" else 594, lab, cmd, kind, 48 if kind == "cta" else 44, ic)
            bx += bw + 12

    def _device_row(self, x, y, w):
        k = self.kit
        k.text(self.cv, x, y, self.t("device"), "small", C.muted)
        k.field(self.cv, x, y + 22, w - 48, self.cfg["device"] or self.t("default_device"), self._dev_menu)
        k.icon_button(self.cv, x + w - 40, y + 22, "refresh", self._refresh_devices, 40)

    def _app_items(self):
        items, cur = list(self.apps), self.cfg["audio_app"]
        if cur and all(a.id != cur for a in items):  # salvo pelo nome do exe: aparece mesmo fora da lista agora
            items.append(SimpleNamespace(id=cur, name=_app_name(cur), active=False))
        return items

    def _app_list(self, x, y, w, rows=3):
        k, cv = self.kit, self.cv
        items = self._app_items()
        if not items:
            k.text(cv, x, y + 4, self.t("no_apps"), "body", C.muted, width=w)
            return
        self.app_off = max(0, min(self.app_off, len(items) - rows))
        for i, a in enumerate(items[self.app_off:self.app_off + rows]):
            yy, on, tg = y + i * 34, a.id == self.cfg["audio_app"], k.tag()
            k.image(cv, x, yy, k.img(w, 32, 10, (C.sel,) * 2, (C.primary,) * 2) if on
                    else k.img(w, 32, 10, (C.bg,) * 2, fa=0.0), tags=(tg, "click", "applist"))
            k.dot(cv, x + 16, yy + 16, C.ok if a.active else C.subtle, tags=(tg, "click", "applist"))
            k.text(cv, x + 32, yy + 16, k.fit(a.name, "bodyb", w - 140), "bodyb", C.text if on else C.muted, "w",
                   tags=(tg, "click", "applist"))
            k.text(cv, x + w - 14, yy + 16, self.t("playing" if a.active else "silent"), "small", C.muted, "e",
                   tags=(tg, "click", "applist"))
            k.bind(cv, tg, "<Button-1>", lambda e, i=a.id: cv.after_idle(self._set, "audio_app", i))
        if len(items) > rows:  # mais apps: rola com a roda do mouse
            k.text(cv, x + w - 38, y - 18, f"{self.app_off + 1}–{self.app_off + rows} / {len(items)}", "small",
                   C.muted, "e")

    def _scroll_apps(self, d):
        self.app_off += d
        self.render()

    def _draw_status(self):
        """Pílula de status (tag "status"): ponto colorido + texto (+ barra de download, ação, atraso)."""
        if self.screen != "main":
            return
        k, cv = self.kit, self.cv
        cv.delete("status")
        lvl, text, prog, action = self.st
        x, y, w, h, tg = 32, 512, 896, 44, ("status",)
        k.image(cv, x, y, k.img(w, h, h / 2, (C.shell, C.shell), (C.hair, C.hair)), tags=tg)
        k.dot(cv, x + 24, y + h / 2, LEVEL.get(lvl, C.info), 8, tg)
        right = x + w - 22
        if self.running and self.lag_txt:
            k.text(cv, right, y + h / 2, self.lag_txt, "small", C.muted, "e", tags=tg)
            right -= k.w(self.lag_txt, "small") + 18
        if action:
            bx, _ = k.button(cv, right, y + 6, action[0], action[1], "link", 32, anchor="ne", tags=tg)
            right = bx - 8
        if lvl == "download":
            k.bar(cv, right - 200, y + h / 2 - 3, 200, prog, tags=tg)
            right -= 216
        k.text(cv, x + 40, y + h / 2, k.fit(text, "body", right - x - 48), "body", C.text, "w", tags=tg)

    # ---------- configurações
    def _settings(self, sec):
        self.section = sec
        if sec == 2:  # Locutores: a lista vem de get_speakers (fora da thread do Tk)
            self._bg(self.get_speakers, then=self._got_saved)
        self.show("settings") if self.screen != "settings" else self.render()

    def _scr_settings(self, w, h):
        k, cv = self.kit, self.cv
        self._header(w, back=True)
        k.text(cv, 32, 90, self.t("settings"), "h1")
        for i, name in enumerate(self.t("sec")):
            y, on, tg = 146 + i * 46, i == self.section, k.tag()
            k.image(cv, 32, y, k.img(200, 40, 12, (C.sel,) * 2, (C.primary,) * 2) if on else
                    k.img(200, 40, 12, (C.bg,) * 2, fa=0.0), tags=(tg, "click"))
            k.text(cv, 52, y + 20, name, "bodyb", C.text if on else C.muted, "w", tags=(tg, "click"))
            k.bind(cv, tg, "<Button-1>", lambda e, i=i: cv.after_idle(self._settings, i))
        k.card(cv, 256, 140, 672, 500)
        x, y, cw = 286, 168, 612
        k.text(cv, x, y, self.t("sec")[self.section], "h2")
        (self._sec_general, self._sec_subtitle, self._sec_speakers, self._sec_audio, self._sec_models)[self.section](
            x, y + 50, cw)

    def _row(self, x, y, label, sub=None, w=None):
        k = self.kit
        k.text(self.cv, x, y + 10, label, "bodyb", C.text, "w")
        if sub:
            k.text(self.cv, x, y + 30, sub, "small", C.muted, width=w)

    def _sec_general(self, x, y, w):
        k = self.kit
        self._row(x, y, self.t("app_lang"))
        k.seg(self.cv, x + w - 260, y - 10, 260, [("en", "English"), ("pt", "Português")], self.lang, self._set_lang)
        self._row(x, y + 70, self.t("tutorial"), self.t("tutorial_sub"))
        k.button(self.cv, x + w, y + 64, self.t("replay"), self._replay, "ghost", 40, anchor="ne")

    def _sec_subtitle(self, x, y, w):
        k, o, cv = self.kit, self.ov, self.cv
        rows = [("font", f"{o['font']} px", ("font", -2, 2)), ("opacity", f"{round(o['alpha'] * 100)} %", ("alpha", -.05, .05))]
        rows += [(key, None, key) for key in ("topmost", "show_orig")]
        rows += [("lines", None, None)] + ([("hide_capture", None, "hide_capture")] if WIN else []) + [("position", None, None)]
        yy = y - 46
        for key, val, ctl in rows:
            yy += 64 if key == "position" and WIN else 46  # a linha "ocultar" tem uma explicação embaixo
            self._row(x, yy, self.t(key), self.t("hide_capture_sub") if key == "hide_capture" else None, w - 80)
            if isinstance(ctl, tuple):
                name, lo, hi = ctl
                k.icon_button(cv, x + w - 36, yy - 6, "plus", lambda n=name, d=hi: self._step_ov(n, d), 32)
                k.text(cv, x + w - 52, yy + 10, val, "bodyb", C.text, "e")
                k.icon_button(cv, x + w - 52 - 64 - 36, yy - 6, "minus", lambda n=name, d=lo: self._step_ov(n, d), 32)
            elif ctl:
                k.toggle(cv, x + w - 44, yy - 2, bool(o[ctl]), lambda v, kk=ctl: self.set_ov(kk, v))
            elif key == "lines":
                k.seg(cv, x + w - 150, yy - 8, 150, [(n, str(n)) for n in (1, 2, 3)], o["lines"],
                      lambda v: self.set_ov("lines", v), 36)
            else:
                k.button(cv, x + w, yy - 8, self.t("recenter"), self._recenter, "ghost", 36, anchor="ne")

    def _sec_speakers(self, x, y, w):
        k, cv = self.kit, self.cv
        yy = k.bottom(cv, k.text(cv, x, y - 8, self.t("spk_note"), "body", C.muted, width=w)) + 18
        ids = sorted(self.saved)
        if not ids:
            k.text(cv, x, yy, self.t("no_voices"), "body", C.muted, width=w)
        for i, spk in enumerate(ids[:6]):  # ponytail: mostra até 6 vozes salvas; rolar se alguém salvar dezenas
            ry = yy + i * 46
            k.dot(cv, x + 6, ry + 18, spk_color(spk), 10)
            nm = k.text(cv, x + 24, ry + 18, k.fit(self.name(spk), "bodyb", w - 280), "bodyb", spk_color(spk), "w")
            bx, _ = k.button(cv, x + w, ry, self.t("forget_one"), lambda s=spk: self.forget(s), "danger", 36, anchor="ne")
            k.button(cv, bx - 8, ry, self.t("rename_btn"), lambda s=spk, it=nm: self._rename_here(s, it), "ghost", 36,
                     anchor="ne")
        if ids:
            k.button(cv, x, 578, self.t("forget_all"), self.forget_all, "danger", 40)

    def _rename_here(self, spk, item):
        x0, y0, x1, y1 = (v / self.kit.s for v in self.cv.bbox(item))
        self.kit.entry(self.cv, x0 - 6, y0 - 6, 260, self.name(spk), self.kit.f["bodyb"],
                       lambda name: self.rename(spk, name) if name else self.render())

    def _sec_audio(self, x, y, w):
        k, cv = self.kit, self.cv
        yy = k.bottom(cv, k.text(cv, x, y - 8, self.t("audio_note"), "body", C.muted, width=w)) + 16
        on = self.get_apps is not None
        k.dot(cv, x + 5, yy + 9, C.ok if on else C.warn)
        k.text(cv, x + 18, yy + 9, self.t("app_capture_on" if on else "app_capture_off"), "body", C.text, "w", width=w - 18)
        self._device_row(x, yy + 44, w)

    def _sec_models(self, x, y, w):
        k, cv = self.kit, self.cv
        lvl, text, prog, _ = self.st
        k.dot(cv, x + 5, y + 10, LEVEL.get(lvl, C.info))
        yy = k.bottom(cv, k.text(cv, x + 18, y + 10, text, "body", C.text, "w", width=w - 18))
        if lvl == "download":
            k.bar(cv, x, yy + 10, 300, prog)
            yy += 16
        yy = k.bottom(cv, k.text(cv, x, yy + 22, self.t("models_note"), "body", C.muted, width=w))
        k.text(cv, x, yy + 22, self.t("models_where"), "small", C.muted)
        k.text(cv, x, yy + 42, str(DATA_DIR / "models"), "bodyb", C.text, width=w)
        k.button(cv, x, yy + 80, self.t("open_folder"), self._open_models, "ghost", 40, "folder")

    def _open_models(self):
        p = DATA_DIR / "models"
        p.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(p) if WIN else subprocess.Popen(["open" if MAC else "xdg-open", str(p)])
        except Exception as e:
            self._status("error", str(e))

    def _replay(self):
        self.step = 0
        self.show("tour")

    def _recenter(self):
        if self.overlay.win.winfo_ismapped():
            self.overlay.snap()
        else:
            self.ov["geometry"] = None
            self.save()

    def _step_ov(self, key, d):
        lo, hi = (16, 56) if key == "font" else (.6, 1.0)
        self.set_ov(key, round(max(lo, min(hi, self.ov[key] + d)), 2))

    def set_ov(self, key, val):
        self.ov[key] = val
        if self.overlay.win.winfo_ismapped():
            self.overlay.style()
            self.overlay.redraw()
        self.save()
        if self.screen == "settings":
            self.render()

    # ---------- ações
    def _set(self, key, val):
        self.cfg[key] = val
        self.save()
        self.render()

    def _tr_status(self, code):
        """Traduz o status da própria UI para `code` (chamar antes de trocar self.lang)."""
        lvl, text, *rest = self.st
        for key in ("st_idle", "st_stopped", "st_starting", "st_loading", "pick_app"):
            text = tr(code, key) if text == self.t(key) else text
        if lvl == "ready":
            text = tr(code, "st_ready", src=self._src_name())
        self.st = (lvl, text, *rest)

    def _set_lang(self, code):
        self._tr_status(code)
        self.lang, self.has_lang = code, True
        self.save()
        self.render()
        self._rebuild()  # "Locutor"/"Speaker" na transcrição
        self._tr_head()
        self.overlay.redraw()

    def _source(self, v):
        self._set("source", v)
        if v == "app":
            self.refresh_apps()

    def _devices(self):
        try:
            self.devices = list(self.get_devices())
        except Exception as e:
            self.devices = []
            self._status("warn", self.t("dev_fail", e=e))

    def _refresh_devices(self):
        self._devices()
        self.render()

    def _dev_menu(self, xr, yr):
        m, var = self.kit.menu(), tk.StringVar(self.root, self.cfg["device"] or "")
        for name, val in ((self.t("default_device"), ""), *((d, d) for d in self.devices)):
            m.add_radiobutton(label=name, value=val, variable=var, command=lambda v=val: self._set("device", v or None))
        m.tk_popup(xr, yr)

    def refresh_apps(self):
        if self.get_apps is not None:
            self._bg(self.get_apps, then=self._got_apps)

    def _got_apps(self, items):
        self.apps = list(items or [])
        if self.screen in ("main", "settings"):
            self.render()

    def _params(self):
        c, app = self.cfg, self.get_apps is not None and self.cfg["source"] == "app"
        return dict(device=None if app else c["device"], app=c["audio_app"] if app else None,
                    call_lang=c["call_lang"], sub_lang=c["sub_lang"])

    def _src_name(self):
        p = self._params()
        if p["app"]:
            return next((a.name for a in self.apps if a.id == p["app"]), _app_name(p["app"]))
        return p["device"] or self.t("default_device")

    def start(self):
        if self.cfg["source"] == "app" and self.get_apps is not None and not self.cfg["audio_app"]:
            return self._status("warn", self.t("pick_app"))
        self._launch()
        self.root.withdraw()  # o app some; fica só a legenda
        self.overlay.show()

    def _launch(self):
        self._freeze_partials()
        self.epoch += 1
        self.seen, self.alias = {}, {}  # o diar.reset() reaproveita ids: alias e "Juntar com" valem só na sessão
        self.running, self.ready, self.started = True, False, self._params()
        self.save()
        self._status("info", self.t("st_starting"))
        self._bg(self.on_start, fail=self._fatal, **self.started)

    def resume(self):
        """Volta à legenda; se fonte ou idioma mudou, reinicia a sessão (on_start de novo)."""
        if not self.running:
            return self.start()
        if self._params() != self.started:
            self._launch()
        self.root.withdraw()
        self.overlay.show()

    def stop(self):
        if self.running:
            self._freeze_partials()
            self.running = False
            self._bg(self.on_stop)
        self.overlay.hide()
        self._status("info", self.t("st_stopped"))
        self.open_main()

    def open_main(self):
        r = self.root
        r.deiconify()
        self.show("main") if self.screen not in ("main", "settings") else self.render()
        r.lift()
        r.focus_force()

    def show_transcript(self):
        self.tw.deiconify()
        dark_titlebar(self.tw)  # só vale com a janela mapeada
        self.tw.lift()
        self._end()

    def _fatal(self, msg):
        """A sessão morreu (falha ao iniciar/capturar): volta à principal com o erro; com app, oferece a saída inteira."""
        self.running = False
        self._bg(self.on_stop)  # a captura por app que falhou continuaria tentando: para a sessão (stop é idempotente)
        self.overlay.hide()
        self._status("error", msg)
        self.open_main()

    def _use_all(self):
        self.cfg["source"] = "all"
        self.start()

    def _bg(self, fn, *a, then=None, fail=None, **kw):
        """Callbacks do app fora da thread do Tk, em ordem (1 worker "ui-ctl"); o resultado volta pela fila."""
        def go():
            try:
                r = fn(*a, **kw)
            except Exception as e:
                log.exception("ui-ctl")
                msg = str(e) or type(e).__name__  # o 'e' some no fim do except: guarda o texto antes
                self.q.put(lambda: (fail or (lambda m: self._status("error", m)))(msg))
                return
            if then:
                self.q.put(lambda: then(r))
        try:
            self.ctl.submit(go)
        except RuntimeError:
            pass  # fechando

    # ---------- status (D13: o pipeline manda pt-BR; a UI mostra o rótulo do nível no idioma dela)
    def _on_status(self, s):
        lvl, text, prog, en = s.level, s.text, s.progress, self.lang == "en"
        if lvl == "error" and self.running and text.startswith(("Falha", "Erro ao iniciar")):
            return self._fatal(text)
        if lvl == "ready" or (lvl == "info" and self.ready and text.startswith("Capturando")):
            self.ready, lvl, text = True, "ready", self.t("st_ready", src=self._src_name())
        elif lvl == "info" and en:
            text = self.t("st_loading")
        elif lvl == "download":
            m = re.search(r"\(\d+/\d+\)", text)
            text = f"{self.t('st_download')}{' ' + m.group() if m else ''}…" if en else text
            text += f" {prog * 100:.0f} %" if prog is not None else ""
        self._status(lvl, text, prog if lvl == "download" else None)

    def _status(self, lvl, text, prog=None):
        act = (self.t("use_all"), self._use_all) if lvl == "error" and self.cfg["source"] == "app" else None
        self.st = (lvl, text, prog, act)
        if self.screen == "main":
            self._draw_status()
        elif self.screen == "settings" and self.section == 4:
            self.render()
        self.dirty = True

    # ---------- dados para o overlay
    def recent(self, n):
        out, ln = [], self.tail
        while ln and len(out) < n:
            out.append(ln)
            ln = ln.prev
        return out[::-1]

    def ov_dot(self):
        lvl = self.st[0]
        if lvl in ("warn", "error", "download"):
            return LEVEL[lvl], False
        return (C.ok, True) if self.ready else (C.info, False)

    def ov_label(self):
        lvl, text = self.st[:2]
        if lvl in ("warn", "error", "download") or not self.ready:
            return text, LEVEL.get(lvl, C.muted) if lvl != "info" else C.muted
        c = self.cfg
        return f"{self.t('live')} · {self._src_name()} · {c['call_lang'].upper()} → {c['sub_lang'].upper()}", C.muted

    def ov_body(self):
        lvl, text, prog, _ = self.st
        if lvl == "download":
            return text, prog, C.text
        if lvl in ("warn", "error"):
            return text, False, LEVEL[lvl]
        if not self.ready:
            return text, False, C.muted
        return self.t("waiting", src=self._src_name()), False, C.muted

    # ---------- locutores (D8/D10: renomear = fixar a voz; juntar é manual e troca o src pelo dst nas linhas)
    def name(self, spk):
        return self.t("speaker_unk") if spk is None else self.names.get(spk) or self.t("speaker", n=spk + 1)

    def _got_speakers(self, ids):
        """Ao abrir: nomes de vozes que não estão mais salvas são descartados."""
        self.saved = set(ids or [])
        for spk in [s for s in self.names if s not in self.saved]:
            del self.names[spk]
        self.save()

    def _got_saved(self, ids):
        self.saved = set(ids or [])
        if self.screen == "settings":
            self.render()

    def _spk_changed(self):
        self.save()
        self._rebuild()
        self.overlay.redraw()
        if self.screen == "settings":
            self.render()

    def rename(self, spk, name):
        self.names[spk] = name
        self.saved.add(spk)
        self._bg(self.on_speaker, "pin", spk)
        self._spk_changed()

    def merge(self, src, dst):
        self._bg(self.on_speaker, "merge", src, dst)
        self.alias = {k: (dst if v == src else v) for k, v in self.alias.items()}
        self.alias[src] = dst
        for ln in itertools.chain(self.arquivo, self.lines.values()):
            if ln.spk == src and ln.key[0] == self.epoch:  # sessão anterior: o mesmo id era outra pessoa
                ln.spk = dst
        if src in self.names:
            self.names.setdefault(dst, self.names[src])
            del self.names[src]
        if src in self.saved:
            self.saved.discard(src)
            self.saved.add(dst)
        self.seen.pop(src, None)
        self._spk_changed()

    def forget(self, spk):
        self._bg(self.on_speaker, "forget", spk)
        self.names.pop(spk, None)
        self.saved.discard(spk)
        self._spk_changed()

    def forget_all(self):
        self._bg(self.on_speaker, "forget_all")
        self.names.clear()
        self.saved.clear()
        self._spk_changed()

    def speaker_menu(self, m, spk):
        ov = self.overlay
        m.add_command(label=self.t("rename"), command=lambda: ov.rename(spk, ov.cv.find_withtag(f"spk:{spk}")[-1]))
        others = [s for s in dict.fromkeys([*self.seen, *sorted(self.saved)]) if s != spk]
        sub = tk.Menu(m, tearoff=0, bg=C.shell, fg=C.text, activebackground=C.sel, font=self.kit.f["body"])
        for s in others:
            sub.add_command(label=self.name(s), foreground=spk_color(s), command=lambda s=s: self.merge(spk, s))
        m.add_cascade(label=self.t("merge"), menu=sub, state="normal" if others else "disabled")
        m.add_command(label=self.t("forget"), command=lambda: self.forget(spk))

    # ---------- transcrição (janela "Ver transcrição")
    def _build_transcript(self):
        k, s = self.kit, self.kit.s
        w = self.tw = tk.Toplevel(self.root)
        w.withdraw()
        w.title(f"{self.t('tr_title')} — TranslateAPP")
        w.configure(bg=C.bg)
        w.geometry(f"{round(820 * s)}x{round(600 * s)}")
        w.minsize(round(480 * s), round(300 * s))
        w.protocol("WM_DELETE_WINDOW", w.withdraw)
        self.th = tk.Canvas(w, height=round(64 * s), bg=C.bg, highlightthickness=0, bd=0)
        self.th.pack(fill="x")
        k.hand(self.th)
        self.th.bind("<Configure>", lambda e: self._tr_head())
        st = ttk.Style(w)
        st.theme_use("clam")
        st.configure("Vertical.TScrollbar", background="#2A2545", troughcolor=C.bg, bordercolor=C.bg, lightcolor="#2A2545",
                     darkcolor="#2A2545", arrowsize=0, width=round(10 * s))
        st.map("Vertical.TScrollbar", background=[("active", C.hair_hi)])
        st.layout("Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
            ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])  # sem setas
        self.paused = tk.Label(w, text=self.t("paused"), bg=C.shell, fg=C.warn, cursor="hand2", font=k.f["small"], pady=6)
        self.paused.bind("<Button-1>", self._end)
        body = self.tbody = tk.Frame(w, bg=C.bg)
        body.pack(fill="both", expand=True)
        t = self.text = tk.Text(body, wrap="word", bg=C.bg, fg=C.text, bd=0, highlightthickness=0, padx=round(28 * s),
                                pady=round(6 * s), font=(k.tfam, 3), state="disabled", insertwidth=0, selectbackground=C.sel,
                                inactiveselectbackground=C.sel, cursor="arrow")
        vs = ttk.Scrollbar(body, command=lambda *a: (t.yview(*a), self._check()))  # arrastar a barra pausa/retoma
        t.configure(yscrollcommand=vs.set)
        vs.pack(side="right", fill="y")
        t.pack(side="left", fill="both", expand=True)
        F = lambda fam, px, wt="normal": tkfont.Font(root=self.root, family=fam, size=-round(px * s), weight=wt)
        f_sub, f_orig, f_hdr = F(k.tfam, 16), F(k.tfam, 13), F(k.ofam[0], 13, k.ofam[1])
        for tag, fg, f, sp in (("sub", C.text, f_sub, 3), ("subp", C.muted, f_sub, 3), ("orig", C.muted, f_orig, 1),
                               ("origp", C.muted, f_orig, 1)):
            t.tag_configure(tag, foreground=fg, font=f, spacing1=round(sp * s), spacing3=round(s))
        for i, col in enumerate([*[spk_color(i) for i in range(8)], spk_color(None)]):
            t.tag_configure(f"h{i}", foreground=col, font=f_hdr, spacing1=round(16 * s))
        t.tag_raise("sel")
        for ev in ("<MouseWheel>", "<KeyRelease>", "<ButtonRelease-1>"):  # o usuário rolou: pausa/retoma o autoscroll
            t.bind(ev, lambda e: self.root.after_idle(self._check), add="+")
        t.bind("<Configure>", lambda e: self._stick(), add="+")
        for key, f in (("s", lambda: self.save_transcript("txt")), ("l", self.clear)):
            for kk in (key, key.upper()):
                w.bind(f"<{'Command' if MAC else 'Control'}-{kk}>", lambda e, f=f: f())

    def _tr_head(self):
        k, cv = self.kit, self.th
        k.reset(cv)
        W = cv.winfo_width() / k.s
        self.tw.title(f"{self.t('tr_title')} — TranslateAPP")
        k.text(cv, 28, 32, self.t("tr_title"), "h2", C.text, "w")
        x = W - 24
        for lab, cmd in ((self.t("clear"), self.clear), (".srt", lambda: self.save_transcript("srt")),
                         (".txt", lambda: self.save_transcript("txt"))):
            x, _ = k.button(cv, x, 14, lab, cmd, "ghost", 36, "doc" if lab.startswith(".") else None, "ne")
            x -= 8
        cv.create_rectangle(0, cv.winfo_height() - 1, W * k.s, cv.winfo_height(), fill=C.hair, outline="")

    def _check(self):
        self.follow = bool(self.text.dlineinfo("end-1c"))
        if self.follow:
            self.paused.pack_forget()
        elif not self.paused.winfo_ismapped():
            self.paused.pack(side="bottom", fill="x", before=self.tbody)

    def _end(self, _=None):
        self.follow = True
        self.paused.pack_forget()
        self.text.yview_moveto(1.0)

    def _stick(self):
        if self.follow:
            self.root.after_idle(lambda: self.text.yview_moveto(1.0))

    def _freeze_partials(self):  # parou no meio da fala: a linha parcial vira definitiva
        t = self.text
        t.configure(state="normal")
        try:
            for ln in self.lines.values():
                if not ln.final:
                    ln.final = True
                    self._render(ln)
        finally:
            t.configure(state="disabled")
        self.dirty = True

    def clear(self):
        t = self.text
        t.configure(state="normal")
        t.delete("1.0", "end")
        t.configure(state="disabled")
        if self.lines:
            t.mark_unset(*[ln.mark for ln in self.lines.values() if not ln.fresh])
        self.lines.clear()
        self.arquivo.clear()
        self.tail = None
        self._end()
        self.overlay.redraw()

    def save_transcript(self, kind="txt"):
        todas = self.arquivo + list(self.lines.values())
        if not todas:
            return self._status("warn", self.t("nothing"))
        parent = next((w for w in (self.tw, self.root, self.overlay.win) if w.winfo_ismapped()), self.root)
        path = filedialog.asksaveasfilename(parent=parent, defaultextension=f".{kind}",
                                            initialfile=time.strftime(f"transcricao-%Y%m%d-%H%M%S.{kind}"),
                                            filetypes=[(self.t("txt") if kind == "txt" else "SubRip", f"*.{kind}"),
                                                       (self.t("all_files"), "*.*")])
        if not path:
            return
        try:
            Path(path).write_text(self._export(todas, kind), "utf-8")
            self._status("info", self.t("saved", p=path))
        except OSError as e:
            self._status("error", self.t("save_fail", e=e))

    def _export(self, todas, kind):
        if kind == "txt":
            off = time.time() - time.monotonic()  # t0 é monotonic: converte para a hora do relógio
            return "".join(f"[{time.strftime('%H:%M:%S', time.localtime(ln.t0 + off))}] {self.name(ln.spk)}: "
                           f"{ln.sub or ln.orig}\n" + (f"    {ln.orig}\n" if ln.sub and ln.orig != ln.sub else "")
                           for ln in todas)

        def ts(v):
            h, ms = divmod(max(0, round(v * 1000)), 3_600_000)
            return "%02d:%02d:%02d,%03d" % (h, *divmod(ms // 1000, 60), ms % 1000)
        base = todas[0].t0
        return "\n".join(f"{i}\n{ts(ln.t0 - base)} --> {ts(max(ln.t_end, ln.t0 + 1) - base)}\n"
                         f"{self.name(ln.spk)}: {ln.sub or ln.orig}\n" for i, ln in enumerate(todas, 1))

    # ---------- updates
    def _tick(self):
        try:
            if not self.q.empty():
                self._drain()
        finally:
            self.job = self.root.after(1 if not self.q.empty() else 30, self._tick)

    def _drain(self):
        t, t0, n = self.text, time.perf_counter(), 0
        t.configure(state="normal")
        try:
            while time.perf_counter() - t0 < BUDGET:
                try:
                    it = self.q.get_nowait()
                except queue.Empty:
                    break
                n += 1
                if isinstance(it, Update):
                    self._apply(it)
                elif isinstance(it, Status):
                    self._on_status(it)
                elif callable(it):  # resultado de um callback do ui-ctl
                    it()
            self._trim()
        finally:
            t.configure(state="disabled")
        if self.follow:
            t.yview_moveto(1.0)
        if self.dirty:
            self.dirty = False
            self.overlay.redraw()
        if self.lags:
            lag = self.t("lag", s=f"{sum(self.lags) / len(self.lags):.1f}".replace(".", "," if self.lang == "pt" else "."))
            if lag != self.lag_txt:
                self.lag_txt = lag
                self._draw_status() if self.root.state() == "normal" else None
        if self.det_dirty and self.screen == "main" and self.root.state() == "normal":
            self.det_dirty = False
            self.render()
        self.busy += time.perf_counter() - t0
        self.done += n

    def _apply(self, u):
        spk = self.alias.get(u.speaker, u.speaker)
        key, orig, sub = (self.epoch, u.utt_id), " ".join((u.orig or "").split()), " ".join((u.sub or "").split())
        ln = self.lines.get(key)
        self.dirty = True
        if u.final and not orig:  # descarte (alucinação/ruído)
            return self._drop(ln) if ln else None
        if (ln and ln.final and not u.final) or not (orig or sub):  # parcial atrasada ou vazia
            return
        if ln is None:
            self.n += 1
            ln = self.lines[key] = SimpleNamespace(key=key, mark=f"m{self.n}", fresh=True, spk=spk, prev=self.tail, next=None)
            if self.tail:
                self.tail.next = ln
            self.tail = ln
        old = ln.spk
        ln.spk, ln.orig, ln.sub, ln.final, ln.t0, ln.t_end = spk, orig, sub, u.final, u.t0, u.t_end
        if spk is not None:
            self.seen[spk] = True
        if u.final:
            if u.lang in ("en", "pt") and u.lang != self.detected:
                self.detected, self.det_dirty = u.lang, True
            if u.utt_id >= EXTRA_ID and self.lags:
                self.lags.pop()  # final dividido por locutor: só a última linha mede o atraso
            self.lags.append(max(0.0, u.t_ready - u.t_end))
        self._render(ln)
        if ln.next and ln.spk != old:  # o cabeçalho do vizinho depende do locutor desta linha
            self._render(ln.next)

    def _render(self, ln):
        """(Re)escreve a linha no lugar: cabeçalho (se o locutor mudou) + legenda + original. Cada linha tem uma marca no
        início da sua região; o texto novo entra no fim dela e o velho é apagado (sem flicker, custo O(1))."""
        t, parts = self.text, []
        if ln.prev is None or ln.prev.spk != ln.spk or ln.prev.key[0] != ln.key[0]:  # sessão nova: cabeçalho de novo
            parts += [self.name(ln.spk) + "\n", f"h{8 if ln.spk is None else ln.spk % 8}"]
        main = ln.sub or ln.orig
        parts += [main + "\n", "sub" if ln.final else "subp"]
        if ln.orig and ln.orig != main:
            parts += [ln.orig + "\n", "orig" if ln.final else "origp"]
        at = t.index(ln.next.mark if ln.next and not ln.next.fresh else "end-1c")  # fim da região = início da próxima
        t.insert(at, *parts)
        if ln.fresh:
            ln.fresh = False
            t.mark_set(ln.mark, at)
        else:
            t.delete(ln.mark, at)

    def _rebuild(self):
        """Reescreve a transcrição inteira (renomear/juntar/esquecer locutor, trocar o idioma do app)."""
        t = self.text
        t.configure(state="normal")
        try:
            t.delete("1.0", "end")
            if self.lines:
                t.mark_unset(*[ln.mark for ln in self.lines.values() if not ln.fresh])
            for ln in self.lines.values():
                ln.fresh = True
            for ln in self.lines.values():
                self._render(ln)
        finally:
            t.configure(state="disabled")
        self._stick()

    def _drop(self, ln):
        t = self.text
        t.delete(ln.mark, t.index(ln.next.mark if ln.next else "end-1c"))
        t.mark_unset(ln.mark)
        del self.lines[ln.key]
        if ln.prev:
            ln.prev.next = ln.next
        if self.tail is ln:
            self.tail = ln.prev
        if ln.next:
            ln.next.prev = ln.prev
            self._render(ln.next)  # pode precisar de cabeçalho agora

    def _trim(self):
        extra = len(self.lines) - MAX_LINES
        if extra <= 0:
            return
        old = [self.lines[k] for k in itertools.islice(self.lines, extra)]  # dict mantém a ordem de chegada
        head = old[-1].next
        self.text.delete("1.0", head.mark)
        self.text.mark_unset(*[ln.mark for ln in old])
        for ln in old:
            del self.lines[ln.key]
        self.arquivo += old
        head.prev = None
        self._render(head)  # a primeira linha da tela sempre leva cabeçalho

    def run(self):
        self._tick()
        self.root.mainloop()

    def close(self):
        self.save()
        self.root.withdraw()  # some na hora; on_stop pode levar até ~1 s
        self.overlay.hide()
        self.ctl.shutdown(wait=False, cancel_futures=True)  # um Iniciar ainda na fila não pode rodar depois de fechar
        try:
            self.on_stop()
        except Exception as e:
            print(f"on_stop falhou: {e}", file=sys.stderr)
        if self.job:
            self.root.after_cancel(self.job)
        self.root.destroy()


# ---------------------------------------------------------------- demo e teste de estresse
DIALOGO = [  # (locutor, idioma falado, EN, PT); texto vazio = linha descartada (ruído)
    (0, "en", "Hello everyone, thanks for joining the call today.", "Olá a todos, obrigado por entrarem na chamada hoje."),
    (0, "en", "We have a lot to cover, so let's get started.", "Temos muito a cobrir, então vamos começar."),
    (1, "en", "Sure. First, the new build is live in production.", "Claro. Primeiro, a nova versão já está no ar em produção."),
    (1, "en", "But we noticed some lag on the login page.", "Mas notamos um pouco de lag na página de login."),
    (2, "pt", "I can take a look at it this afternoon, if that works.", "Posso dar uma olhada nisso hoje à tarde, se estiver bom."),
    (1, "en", "you", ""),
    (0, "en", "Perfect, thank you. Any other blockers?", "Perfeito, obrigado. Mais algum impedimento?"),
    (2, "pt", "Quick question: did the ping improve after the deploy?", "Uma pergunta rápida: o ping melhorou depois do deploy?"),
    (1, "en", "Yes, it dropped from two hundred to about eighty milliseconds.", "Sim, caiu de duzentos para cerca de oitenta milissegundos."),
    (2, "pt", "Nice.", "Legal."),
    (0, "en", "Great work, team. Let's wrap up and send the notes by tonight.", "Ótimo trabalho, pessoal. Vamos encerrar e enviar as anotações até a noite."),
]


def _roteiro(q, run, stop, conf):
    """Thread geradora do demo: parciais crescendo palavra a palavra (~0,6 s) e depois o final."""
    rng, uid = Random(7), 0
    for spk, lang, en, pt in itertools.cycle(DIALOGO):
        while not run.wait(0.1):  # parado: espera o "Iniciar"
            if stop.is_set():
                return
        orig, sub = (en, pt) if lang == "en" else (pt, en)
        if conf["sub_lang"] == lang:
            sub = orig  # já está no idioma da legenda: não traduz
        ow, sw, t0, k = orig.split(), sub.split(), time.monotonic(), 0

        def up(final, o, s, sp):
            now = time.monotonic()
            q.put(Update(uid, sp, o, s, final, t0, now - (rng.uniform(0.3, 0.5) if final else 0.15), now, lang))
        if not pt:  # ruído: aparece um parcial e o final manda apagar
            up(False, en, "você", None)
            stop.wait(0.8)
        while k < len(ow) - 1:
            k = min(k + rng.choice((1, 2)), len(ow) - 1)
            up(False, " ".join(ow[:k]), " ".join(sw[:round(k * len(sw) / len(ow)) or 1]), None if k < 3 else spk)
            stop.wait(0.6)
        stop.wait(0.5)  # silêncio de fim + ASR final
        up(True, orig if pt else "", sub, spk)
        uid += 1
        if stop.wait(rng.uniform(0.6, 1.2)):
            return


def _scratch_settings(text=None):
    """Troca o SETTINGS por um arquivo temporário (cópia do real, ou `text`): demo e stress não mexem no de verdade."""
    global SETTINGS
    import shutil, tempfile
    tmp = Path(tempfile.mkdtemp()) / "settings.json"
    if text is not None:
        tmp.write_text(text, "utf-8")
    elif SETTINGS.exists():
        shutil.copyfile(SETTINGS, tmp)
    SETTINGS = tmp


def demo_app(autostart=False):
    """App com callbacks falsos (demo e screenshots). Devolve (app, stop): chamar stop() ao fechar."""
    _scratch_settings()
    q, run, stop = queue.Queue(), threading.Event(), threading.Event()
    nomes = ["Alto-falantes (Realtek High Definition Audio)", "LG HDR WFHD (NVIDIA High Definition Audio)", "NVIDIA HDMI Output"]
    apps = [SimpleNamespace(id="Discord.exe", name="Discord", active=True),
            SimpleNamespace(id="chrome.exe", name="Google Chrome", active=True),
            SimpleNamespace(id="Spotify.exe", name="Spotify", active=False),
            SimpleNamespace(id="Zoom.exe", name="Zoom Workplace", active=False)]
    conf, pinned, first = {"sub_lang": "pt"}, set(), [True]

    def start(device=None, app=None, call_lang="auto", sub_lang="pt"):
        run.clear()
        conf.update(sub_lang=sub_lang)
        if first[0]:  # 1ª vez: simula o download dos modelos (2 determinados, 1 indeterminado) antes do "ready"
            first[0] = False
            for nome, n in (("modelo de voz (large-v3-turbo)", 1), ("tradutor EN→PT (opus-mt)", 2), ("detector de locutor", 3)):
                q.put(Status(f"Baixando {nome}… ({n}/3)", "download", None if n == 3 else 0.0))
                for i in range(1, 21 if n < 3 else 12):
                    if stop.wait(0.08 if n < 3 else 0.2):
                        return
                    if n < 3:
                        q.put(Status(f"Baixando {nome}… ({n}/3)", "download", i / 20))
        q.put(Status("Carregando modelos…"))
        stop.wait(0.4)
        q.put(Status(f"Pronto — escutando {app or device or 'o áudio do PC'} (demo)", "ready"))
        run.set()

    def speaker(cmd, *a):
        {"pin": lambda: pinned.add(a[0]), "merge": lambda: pinned.discard(a[0]), "forget": lambda: pinned.discard(a[0]),
         "forget_all": pinned.clear}[cmd]()
    app = App(q, get_devices=lambda: nomes, get_apps=lambda: (time.sleep(0.05), apps)[1],
              get_speakers=lambda: sorted(pinned), on_start=start, on_stop=run.clear, on_speaker=speaker,
              autostart=autostart)
    th = threading.Thread(target=_roteiro, args=(q, run, stop, conf), name="demo", daemon=True)
    th.start()
    return app, lambda: (stop.set(), th.join(3))


def _demo():
    app, stop = demo_app()
    if "--first-run" in sys.argv:  # força o onboarding (o demo grava numa cópia do settings.json)
        app.show("lang")
        app._place()
    try:
        app.run()
    finally:
        stop()


def _stress():
    _scratch_settings('{"ui_lang": "pt", "overlay": {"lines": 3, "hide_capture": false}}')
    q, calls = queue.Queue(), []
    app = App(q, get_devices=lambda: [], get_apps=None, get_speakers=lambda: [0], on_start=lambda **kw: calls.append(kw),
              on_stop=lambda: None, on_speaker=lambda *a: calls.append(a))
    r, ov = app.root, app.overlay
    U = lambda i, s, o, sb, fin: Update(i, s, o, sb, fin, 0.0, 1.0, 1.8, "en")
    txt = lambda: app.text.get("1.0", "end-1c")

    def feed(*us):  # aplica já, sem esperar o tick
        for u in us:
            q.put(u)
        app._drain()
    r.update()
    app._pick_lang("en")  # escolher o idioma no onboarding traduz também o status
    assert app.st[1] == tr("en", "st_idle"), app.st
    app._pick_lang("pt")
    # lógica: cabeçalho só quando o locutor muda; None -> N; rótulo corrigido; descarte; parcial atrasada; Limpar
    feed(U(0, 0, "aa", "AA", True), U(1, None, "bb", "BB", False), U(2, 0, "cc", "CC", True))
    assert txt().count("Locutor") == 3 and "Locutor ?" in txt()
    feed(U(1, 0, "bb", "BB", True))  # BB vira Locutor 1: somem os cabeçalhos de BB e de CC
    assert txt().count("Locutor") == 1 and txt().index("AA") < txt().index("BB") < txt().index("CC"), txt()
    feed(U(1, 0, "", "", True), U(0, 0, "aa", "XX", False))  # descarta BB; parcial depois do final é ignorada
    assert "BB" not in txt() and "XX" not in txt() and txt().count("Locutor") == 1
    feed(U(0, 1, "aa", "AA", True))  # AA vira Locutor 2: ganha cabeçalho e CC (Locutor 1) também
    assert txt().count("Locutor") == 2 and app.lag_txt == "atraso ~0,8 s", txt()
    n = len(app.lags)  # final dividido: a 1ª linha acabou 5 s antes, mas só a última (0,8 s) entra no atraso
    feed(Update(8, 0, "dd", "DD", True, 0.0, 0.0, 5.0), Update(EXTRA_ID, 1, "ee", "EE", True, 0.0, 1.0, 1.8))
    assert len(app.lags) == n + 1 and app.lag_txt == "atraso ~0,8 s", list(app.lags)
    feed(U(20, 2, "same", "same", True))  # falado = legenda: sem linha "original" duplicada
    assert txt().count("same") == 1
    # locutores: renomear (pin + nome), juntar (troca nas linhas já mostradas e nas que chegarem), esquecer
    app.rename(1, "Ana")
    assert "Ana" in txt() and "Locutor 2" not in txt() and app.names == {1: "Ana"}
    app.merge(2, 1)
    feed(U(21, 2, "late2", "LATE2", True))  # update atrasado com o id antigo vira o destino
    assert all(ln.spk != 2 for ln in app.lines.values()) and "Locutor 3" not in txt()
    app.forget(1)
    assert "Ana" not in txt() and 1 not in app.names
    app.ctl.submit(lambda: None).result(2)
    assert ("pin", 1) in calls and ("merge", 2, 1) in calls and ("forget", 1) in calls, calls
    srt = app._export(list(app.lines.values()), "srt")
    assert srt.startswith("1\n00:00:00,000 --> 00:00:01,000\n"), srt[:60]
    app.clear()
    assert not txt() and not app.lines
    # overlay: aparece no início, opacidade, arrastar e redimensionar pelas bordas
    app.cfg["source"] = "all"
    app.start()
    r.update()
    app.ctl.submit(lambda: None).result(2)
    assert not r.winfo_ismapped() and ov.win.winfo_ismapped() and calls[-1]["sub_lang"] == "pt", calls[-1]
    assert abs(float(ov.win.attributes("-alpha")) - app.ov["alpha"]) < .01
    w = ov.win
    w.geometry("700x200+300+300")
    r.update()

    def drag(x0, y0, dx, dy):  # pressiona em (x0, y0) relativo à janela e arrasta
        rx, ry = w.winfo_rootx(), w.winfo_rooty()
        ov.cv.event_generate("<Motion>", x=x0, y=y0, rootx=rx + x0, rooty=ry + y0)
        ov.cv.event_generate("<ButtonPress-1>", x=x0, y=y0, rootx=rx + x0, rooty=ry + y0)
        for i in range(1, 6):
            ov.cv.event_generate("<B1-Motion>", x=x0 + dx * i // 5, y=y0 + dy * i // 5, rootx=rx + x0 + dx * i // 5,
                                 rooty=ry + y0 + dy * i // 5, state=0x100)
        ov.cv.event_generate("<ButtonRelease-1>", x=x0 + dx, y=y0 + dy, rootx=rx + x0 + dx, rooty=ry + y0 + dy)
        r.update()
        return w.winfo_x(), w.winfo_y(), w.winfo_width(), w.winfo_height()
    assert drag(697, 197, 100, 40) == (300, 300, 800, 240)        # canto SE
    assert drag(2, 100, -60, 0) == (240, 300, 860, 240)           # borda W
    assert drag(430, 2, 0, -20) == (240, 280, 860, 260)           # borda N
    assert drag(400, 150, 50, 30) == (290, 310, 860, 260)         # meio = mover
    # rajadas: 2 x (1500 utterances x parcial + final) = 2 x 3000 updates; a 2ª passa de MAX_LINES e poda
    gaps, last = [], [time.perf_counter()]

    def beat():  # batimento de 2 ms: o maior intervalo entre batimentos = pior travada da UI
        now = time.perf_counter()
        gaps.append(now - last[0])
        last[0] = now
        r.after(2, beat)
    r.update()
    beat()
    app._tick()
    for nome, base in (("rajada 1", 0), ("rajada 2", 1500)):
        d0, b0, t0, g0 = app.done, app.busy, time.perf_counter(), len(gaps)
        for i in range(base, base + 1500):
            o, s = f"this is test sentence number {i} with a few words", f"esta é a frase de teste número {i} com algumas palavras"
            q.put(U(i, None, o[:24], s[:26], False))
            q.put(U(i, i // 2 % 3, o, s, True))
        while not q.empty():
            r.update()
        r.update()
        print(f"{nome}: {app.done - d0} updates em {time.perf_counter() - t0:.2f} s | {(app.busy - b0) / (app.done - d0) * 1e6:.0f} "
              f"us/update | pior travada da UI {max(gaps[g0:]) * 1e3:.0f} ms | linhas na tela {len(app.lines)}")
        assert max(gaps[g0:]) < 0.05, "travada > 50 ms"
    app._check()
    assert len(app.lines) == MAX_LINES and len(app.arquivo) == 1000 and app.follow and app.text.yview()[1] > 0.999
    assert not app.text.tag_ranges("subp")  # nenhum parcial sobrou
    for ln in app.lines.values():  # cabeçalho exatamente onde o locutor muda
        tem = any(re.fullmatch(r"h\d", g) for g in app.text.tag_names(app.text.index(ln.mark)))
        assert tem == (ln.prev is None or ln.prev.spk != ln.spk), ln
    ov._hover(True)  # controles visíveis: redesenhar 200 vezes não acumula binds/comandos Tcl
    n0 = len(ov.cv._tclCommands or [])
    for _ in range(200):
        ov.redraw()
    assert len(ov.cv._tclCommands or []) <= n0 + 2, (n0, len(ov.cv._tclCommands))
    shown = ov.cv.find_withtag("blk")
    assert shown and "2999" in " ".join(ov.cv.itemcget(i, "text") for i in shown)  # overlay mostra a última fala
    app.text.yview_moveto(0.3)  # usuário rolou para cima: pausa o autoscroll
    app._check()
    feed(U(9999, 0, "late", "TARDE", True))
    assert not app.follow and app.text.yview()[1] < 0.99
    app._end()
    assert app.follow and app.text.yview()[1] > 0.999
    # sessão nova (o diar.reset() reaproveita ids): o "Juntar" da sessão anterior não vale para quem chega agora
    app.merge(1, 0)
    app._launch()
    feed(U(0, 1, "new", "NOVA", True))
    assert app.lines[(app.epoch, 0)].spk == 1 and not app.alias and list(app.seen) == [1], (app.alias, app.seen)
    ov.redraw()
    names = [i for i in ov.cv.find_withtag("click") if any(t.startswith("spk:") for t in ov.cv.gettags(i))]
    assert [ov.cv.gettags(i)[-1] for i in names] == ["spk:1"]  # nome de linha antiga não abre renomear/juntar

    def click(x, y):
        rx, ry = w.winfo_rootx(), w.winfo_rooty()
        for ev in ("<Motion>", "<ButtonPress-1>", "<ButtonRelease-1>"):
            ov.cv.event_generate(ev, x=x, y=y, rootx=rx + x, rooty=ry + y)
        r.update()
    x0, y0, x1, y1 = ov.cv.bbox(names[0])
    click((x0 + x1) // 2, (y0 + y1) // 2)  # clicar no nome: o campo abre com o foco
    assert ov.editing and isinstance(r.focus_get(), tk.Entry), r.focus_get()
    click(w.winfo_width() // 2, w.winfo_height() - 4)  # clicar fora: confirma (sem mudar o nome, não fixa a voz)
    assert not ov.editing and not ov.cv.winfo_children() and 1 not in app.names, app.names
    app.stop()
    r.update()
    assert r.winfo_ismapped() and not w.winfo_ismapped() and app.screen == "main"
    app.on_start = lambda **kw: 1 / 0  # falha ao iniciar: volta à principal com o erro
    app.start()
    app.ctl.submit(lambda: None).result(2)
    app._drain()
    r.update()
    assert not app.running and app.st[0] == "error" and "division" in app.st[1] and r.winfo_ismapped()
    print("ok")
    app.close()


if __name__ == "__main__":
    if "--stress" in sys.argv:
        _stress()
    elif "--demo" in sys.argv:
        _demo()
    else:
        print("Uso: python -m app.ui --demo [--first-run] | --stress  (a UI real é montada por app.main)")
