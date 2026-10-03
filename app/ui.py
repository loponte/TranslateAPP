"""Janela de legendas ao vivo (tkinter/ttk, sem dependências).
`python -m app.ui --demo` roda com dados falsos; `--stress` faz rajadas de 3000 updates + checagens."""
from __future__ import annotations

import ctypes, itertools, json, queue, re, sys, threading, time
import tkinter as tk
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from random import Random
from tkinter import filedialog, font as tkfont, ttk
from types import SimpleNamespace

from app.events import Status, Update

SETTINGS = Path(__file__).resolve().parent.parent / "settings.json"
DEFAULTS = {"font": 15, "alpha": 1.0, "topmost": False, "show_en": True, "device": None, "geometry": "1100x660"}
PADRAO = "Padrão do Windows"
MAX_LINHAS = 2000  # utterances no widget; as mais antigas saem da tela (continuam no .txt salvo)
EXTRA_ID = 1_000_000  # utt_id das linhas extras de um final dividido por locutor (= app.pipeline.EXTRA_ID; a UI não importa o pipeline)
BUDGET = 0.012  # s por tick processando updates: uma rajada nunca prende a UI
BG, BAR, FIELD, LINE, FG, BTN, HI, SEL = "#14161a", "#1b1e24", "#101216", "#2a2e36", "#e8eaed", "#272b33", "#343a46", "#2f4b7c"
LOCUTORES = ["#6cb6ff", "#ffa657", "#7ee787", "#ff7eb6", "#bc8cff", "#f2cc60", "#56d4dd", "#ff7b72", "#9aa3b0"]  # 8 + "?"
NIVEIS = {"info": "#9aa3b0", "ready": "#7ee787", "warn": "#f2cc60", "error": "#ff7b72"}


class App:
    def __init__(self, out_q, *, get_devices, on_start, on_stop):
        self.q, self.get_devices, self.on_start, self.on_stop = out_q, get_devices, on_start, on_stop
        self.ctl = ThreadPoolExecutor(1, thread_name_prefix="ui-ctl")
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sem isso o Windows estica a janela (texto borrado)
        except Exception:
            pass  # fora do Windows ou já definido
        self.cfg = cfg = self._load()
        self.root = r = tk.Tk()
        # (época, utt_id) -> linha; linhas podadas (ponytail: ficam na RAM, ~200 B cada; gravar em disco se rodar por dias)
        self.lines, self.arquivo, self.tail = {}, [], None
        self.lags, self.busy, self.job = deque(maxlen=10), 0.0, None
        self.epoch = self.n = self.done = 0  # a época sobe a cada "Iniciar" (o utt_id pode reiniciar em 0)
        self.running, self.follow, self.size = False, True, cfg["font"]
        r.title("TranslateAPP — Legendas ao vivo")
        r.configure(bg=BG)
        r.minsize(660, 340)
        r.attributes("-alpha", cfg["alpha"], "-topmost", cfg["topmost"])
        self._theme()
        self._build()
        self._place()
        self._titlebar()
        self._devices(cfg["device"])
        self._status(Status("Parado. Escolha o dispositivo e clique em Iniciar."))
        r.protocol("WM_DELETE_WINDOW", self._close)
        for k, f in (("l", self.clear), ("s", self.save)):
            for key in (k, k.upper()):
                r.bind(f"<Control-{key}>", lambda e, f=f: f())

    # ---------- preferências e janela
    def _load(self):
        try:
            cfg = {**DEFAULTS, **json.loads(SETTINGS.read_text("utf-8"))}
            cfg["font"], cfg["alpha"] = max(9, min(40, int(cfg["font"]))), max(0.35, min(1.0, float(cfg["alpha"])))
            cfg["topmost"], cfg["show_en"] = bool(cfg["topmost"]), bool(cfg["show_en"])
            return cfg
        except Exception:
            return dict(DEFAULTS)  # sem arquivo ou inválido

    def _save(self):
        r = self.root
        cfg = {"font": self.size, "alpha": round(float(r.attributes("-alpha")), 2), "topmost": self.top.get(),
               "show_en": self.show_en.get(), "device": self._device(),
               "geometry": r.geometry() if r.state() == "normal" else self.cfg["geometry"]}
        try:
            SETTINGS.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), "utf-8")
        except OSError:
            pass

    def _place(self):
        r, g = self.root, str(self.cfg["geometry"])
        full = re.fullmatch(r"\d+x\d+([+-]-?\d+){2}", g)
        r.geometry(g if full else (re.match(r"\d+x\d+", g) or [DEFAULTS["geometry"]])[0])
        r.update_idletasks()
        w, h = r.winfo_width(), r.winfo_height()
        vx, vy, vw, vh = r.winfo_vrootx(), r.winfo_vrooty(), r.winfo_vrootwidth(), r.winfo_vrootheight()
        if not full:  # primeira execução: centraliza no monitor principal
            r.geometry(f"+{(r.winfo_screenwidth() - w) // 2}+{(r.winfo_screenheight() - h) // 3}")
        elif not (vx - w + 120 < r.winfo_x() < vx + vw - 120 and vy - 30 < r.winfo_y() < vy + vh - 120):
            r.geometry(f"+{vx + 80}+{vy + 80}")  # monitor removido: não deixa a janela fora da tela

    def _titlebar(self):
        try:  # barra de título escura (Windows 10 2004+/11)
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            for attr, val in ((20, 1), (35, 0x241E1B)):  # modo escuro; cor da barra = BAR (BGR)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(ctypes.c_int(val)), 4)
        except Exception:
            pass

    # ---------- construção
    def _theme(self):
        r = self.root
        for k, v in (("background", FIELD), ("foreground", FG), ("selectBackground", SEL), ("selectForeground", FG),
                     ("font", ("Segoe UI", 10))):
            r.option_add(f"*TCombobox*Listbox.{k}", v)
        st = ttk.Style(r)
        st.theme_use("clam")
        st.configure(".", background=BAR, foreground=FG, font=("Segoe UI", 10), bordercolor=LINE, lightcolor=BAR,
                     darkcolor=BAR, troughcolor=FIELD, focuscolor=BAR)

        def flat(name, bg, hi, **kw):  # estilo sem relevo; cor de realce no hover
            c = dict(background=bg, bordercolor=bg, lightcolor=bg, darkcolor=bg)
            st.configure(name, **{**c, **kw})
            st.map(name, **{k: [("active", hi)] for k in c})
        flat("TButton", BTN, HI, padding=(12, 5))
        flat("Go.TButton", "#238636", "#2ea043", foreground="#ffffff", padding=(12, 5))
        flat("Stop.TButton", "#b62324", "#da3633", foreground="#ffffff", padding=(12, 5))
        flat("Vertical.TScrollbar", "#323843", "#485060", troughcolor=BG, bordercolor=BG, width=12)
        st.layout("Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
            ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])  # sem setas
        st.configure("Horizontal.TScale", background="#aab2bf", troughcolor=FIELD, bordercolor=LINE, lightcolor=FIELD,
                     darkcolor=FIELD, gripcount=0, sliderlength=16)
        st.map("Horizontal.TScale", background=[("active", "#ffffff")])
        st.configure("TCombobox", fieldbackground=FIELD, background=BTN, arrowcolor=FG, bordercolor=LINE, lightcolor=FIELD,
                     darkcolor=FIELD, padding=5)
        st.map("TCombobox", fieldbackground=[("readonly", FIELD)], foreground=[("readonly", FG)], background=[("active", HI)],
               selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", FG)])
        st.configure("TCheckbutton", indicatorbackground=FIELD, indicatorforeground="#ffffff", upperbordercolor="#4a5160",
                     lowerbordercolor="#4a5160", indicatormargin=(0, 1, 6, 1))
        st.map("TCheckbutton", background=[("active", BAR)], indicatorbackground=[("selected", "#2f81f7")],
               upperbordercolor=[("selected", "#2f81f7")], lowerbordercolor=[("selected", "#2f81f7")])

    def _build(self):
        r, cfg = self.root, self.cfg
        F = lambda **kw: tkfont.Font(root=r, family="Segoe UI", **kw)
        f = self.fonts = {"pt": F(), "ptp": F(slant="italic"), "en": F(), "enp": F(slant="italic"), "hdr": F(weight="bold")}
        self._fonts()
        bar = ttk.Frame(r, padding=(14, 12, 14, 10))  # barra superior
        bar.pack(fill="x")
        bar.columnconfigure(1, weight=1)
        ttk.Label(bar, text="Dispositivo").grid(row=0, column=0, padx=(0, 10))
        self.dev = ttk.Combobox(bar, state="readonly")
        self.dev.grid(row=0, column=1, sticky="ew")
        self.dev.bind("<<ComboboxSelected>>", lambda e: self.dev.selection_clear())
        self.refresh = ttk.Button(bar, text="Atualizar", command=self._devices)
        self.btn = ttk.Button(bar, text="Iniciar", style="Go.TButton", width=9, command=self._toggle)
        for col, w in ((2, self.refresh), (3, self.btn), (4, ttk.Button(bar, text="Limpar", command=self.clear)),
                       (5, ttk.Button(bar, text="Salvar…", command=self.save))):
            w.grid(row=0, column=col, padx=(18 if col == 3 else 8, 0))
        row = ttk.Frame(bar)
        row.grid(row=1, column=0, columnspan=6, sticky="w", pady=(10, 0))
        for txt, d in (("A−", -1), ("A+", 1)):
            ttk.Button(row, text=txt, width=3, command=lambda d=d: self._zoom(d)).pack(side="left", padx=(0, 4))
        self.show_en, self.top = tk.BooleanVar(value=cfg["show_en"]), tk.BooleanVar(value=cfg["topmost"])
        for txt, var, cmd in (("Mostrar inglês", self.show_en, self._en),
                              ("Sempre no topo", self.top, lambda: r.attributes("-topmost", self.top.get()))):
            ttk.Checkbutton(row, text=txt, variable=var, command=cmd).pack(side="left", padx=(16, 0))
        ttk.Label(row, text="Opacidade").pack(side="left", padx=(18, 8))
        ttk.Scale(row, from_=35, to=100, length=130, value=cfg["alpha"] * 100,
                  command=lambda v: r.attributes("-alpha", float(v) / 100)).pack(side="left")
        tk.Frame(r, height=1, bg=LINE).pack(fill="x")
        sb = ttk.Frame(r, padding=(14, 6))  # barra de status (antes do texto, para reservar o rodapé)
        sb.pack(side="bottom", fill="x")
        sb.columnconfigure(0, weight=1)
        self.stat = ttk.Label(sb)
        self.paused = ttk.Label(sb, text="Rolagem pausada — clique para ir ao fim ↓", foreground="#f2cc60", cursor="hand2")
        self.lag = ttk.Label(sb, text="atraso —", foreground=NIVEIS["info"])
        for col, w, px in ((0, self.stat, 0), (1, self.paused, 20), (2, self.lag, 0)):
            w.grid(row=0, column=col, padx=(0, px), sticky="w")
        self.paused.grid_remove()
        self.paused.bind("<Button-1>", self._end)
        tk.Frame(r, height=1, bg=LINE).pack(side="bottom", fill="x")
        body = tk.Frame(r, bg=BG)  # transcrição (fonte mínima no widget: define a altura da linha vazia final)
        body.pack(fill="both", expand=True)
        t = self.text = tk.Text(body, wrap="word", bg=BG, fg=FG, bd=0, highlightthickness=0, padx=22, pady=10,
                                font=("Segoe UI", 3), state="disabled", insertwidth=0, selectbackground=SEL,
                                inactiveselectbackground=SEL)
        vs = ttk.Scrollbar(body, command=lambda *a: (t.yview(*a), self._check()))  # arrastar a barra pausa/retoma
        t.configure(yscrollcommand=vs.set)
        vs.pack(side="right", fill="y")
        t.pack(side="left", fill="both", expand=True)
        for tag, kw in {"pt": dict(foreground=FG, spacing1="5p", spacing2="2p"),
                        "ptp": dict(foreground="#9aa3b2", spacing1="5p", spacing2="2p"),
                        "en": dict(foreground="#8a929e", spacing1="1p", spacing3="1p"),
                        "enp": dict(foreground="#737e90", spacing1="1p", spacing3="1p")}.items():
            t.tag_configure(tag, font=f[tag], **kw)
        for i, c in enumerate(LOCUTORES):
            t.tag_configure(f"h{i}", font=f["hdr"], foreground=c, spacing1="15p")
        t.tag_raise("sel")
        self._en()
        for ev in ("<MouseWheel>", "<KeyRelease>", "<ButtonRelease-1>"):  # o usuário rolou: pausa/retoma o autoscroll
            t.bind(ev, lambda e: r.after_idle(self._check), add="+")
        t.bind("<Configure>", lambda e: self._stick(), add="+")

    # ---------- ações da barra
    def _devices(self, keep=None):
        try:
            names = list(self.get_devices())
        except Exception as e:
            names = []
            self._status(Status(f"Não foi possível listar os dispositivos: {e}", "warn"))
        cur = keep or self.dev.get()
        self.dev.configure(values=[PADRAO, *names])
        self.dev.set(cur if cur in names else PADRAO)

    def _device(self):
        return None if self.dev.get() == PADRAO else self.dev.get()

    def _ctl(self, fn, what, *args):
        """on_start/on_stop fora da thread da UI (Pipeline.stop() pode levar ~1 s), em ordem (1 worker).
        Erros voltam como Status pela fila."""
        def go():
            try:
                fn(*args)
            except Exception as e:
                self.q.put(Status(f"Erro ao {what}: {e}", "error"))
        self.ctl.submit(go)

    def _set_running(self, run):
        self.running = run
        self.btn.configure(text="Parar" if run else "Iniciar", style="Stop.TButton" if run else "Go.TButton")
        self.dev.configure(state="disabled" if run else "readonly")
        self.refresh.configure(state="disabled" if run else "normal")
        if not run:
            self._status(Status("Parado."))

    def _freeze_partials(self):  # parou no meio da fala: a linha parcial vira definitiva (senão fica cinza/itálico)
        t = self.text
        t.configure(state="normal")
        try:
            for ln in self.lines.values():
                if not ln.final:
                    ln.final = True
                    self._render(ln)
        finally:
            t.configure(state="disabled")

    def _toggle(self):
        if self.running:
            self._freeze_partials()
            self._ctl(self.on_stop, "parar")
        else:
            self.epoch += 1
            self._status(Status("Iniciando…"))
            self._ctl(self.on_start, "iniciar", self._device())
        self._set_running(not self.running)

    def _fonts(self):
        for k, v in (("pt", 1), ("ptp", 1), ("en", .72), ("enp", .72), ("hdr", .7)):
            self.fonts[k].configure(size=max(8, round(self.size * v)))

    def _zoom(self, d):
        self.size = max(9, min(40, self.size + d))
        self._fonts()
        self._stick()

    def _en(self):  # inglês oculto = texto "elided" (instantâneo, não reconstrói nada)
        for tag in ("en", "enp"):
            self.text.tag_configure(tag, elide=not self.show_en.get())
        self._stick()

    def clear(self):
        t = self.text
        t.configure(state="normal")
        t.delete("1.0", "end")
        t.configure(state="disabled")
        if self.lines:
            t.mark_unset(*[ln.mark for ln in self.lines.values()])
        self.lines.clear()
        self.arquivo.clear()
        self.tail = None
        self._end()

    def save(self):
        todas = self.arquivo + list(self.lines.values())
        if not todas:
            return self._status(Status("Não há nada para salvar.", "warn"))
        path = filedialog.asksaveasfilename(parent=self.root, title="Salvar transcrição", defaultextension=".txt",
                                            initialfile=time.strftime("transcricao-%Y%m%d-%H%M%S.txt"),
                                            filetypes=[("Texto", "*.txt"), ("Todos os arquivos", "*.*")])
        if not path:
            return
        off = time.time() - time.monotonic()  # t0 é monotonic: converte para a hora do relógio
        out = [f"[{time.strftime('%H:%M:%S', time.localtime(ln.t0 + off))}] {self._nome(ln.spk)}: {ln.pt}"
               + (f"\n    EN: {ln.en}" if ln.en else "") for ln in todas]
        try:
            Path(path).write_text("\n".join(out) + "\n", "utf-8")
            self._status(Status(f"Transcrição salva em {path}"))
        except OSError as e:
            self._status(Status(f"Não foi possível salvar: {e}", "error"))

    # ---------- rolagem: segue o fim enquanto o fim estiver visível
    def _check(self):
        self.follow = bool(self.text.dlineinfo("end-1c"))
        (self.paused.grid_remove if self.follow else self.paused.grid)()

    def _end(self, _=None):
        self.follow = True
        self.paused.grid_remove()
        self.text.yview_moveto(1.0)

    def _stick(self):
        if self.follow:
            self.root.after_idle(lambda: self.text.yview_moveto(1.0))

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
                    if it.level == "error" and self.running and it.text.startswith(("Falha", "Erro ao iniciar")):
                        self._set_running(False)  # a sessão morreu: libera botão e dropdown para tentar de novo
                    self._status(it)
            self._trim()
        finally:
            t.configure(state="disabled")
        if self.follow:
            t.yview_moveto(1.0)
        if self.lags:
            self.lag.configure(text=f"atraso ~{sum(self.lags) / len(self.lags):.1f} s".replace(".", ","))
        self.busy += time.perf_counter() - t0
        self.done += n

    def _apply(self, u):
        key, en, pt = (self.epoch, u.utt_id), " ".join((u.en or "").split()), " ".join((u.pt or "").split())
        ln = self.lines.get(key)
        if u.final and not en:  # descarte (alucinação/ruído)
            return self._drop(ln) if ln else None
        if (ln and ln.final and not u.final) or not (en or pt):  # parcial atrasada ou vazia
            return
        if ln is None:
            self.n += 1
            ln = self.lines[key] = SimpleNamespace(key=key, mark=f"m{self.n}", fresh=True, spk=u.speaker, prev=self.tail, next=None)
            if self.tail:
                self.tail.next = ln
            self.tail = ln
        old = ln.spk
        ln.spk, ln.en, ln.pt, ln.final, ln.t0 = u.speaker, en, pt, u.final, u.t0
        if u.final:
            if u.utt_id >= EXTRA_ID and self.lags:
                self.lags.pop()  # final dividido por locutor: só a última linha mede o atraso (as outras incluem o resto da frase)
            self.lags.append(max(0.0, u.t_ready - u.t_end))
        self._render(ln)
        if ln.next and ln.spk != old:  # o cabeçalho do vizinho depende do locutor desta linha
            self._render(ln.next)

    def _render(self, ln):
        """(Re)escreve a linha no lugar: cabeçalho (se o locutor mudou) + PT + EN. Cada linha tem uma marca no
        início da sua região; o texto novo entra no fim dela e o velho é apagado (sem flicker, custo O(1))."""
        t, parts = self.text, []
        if ln.prev is None or ln.prev.spk != ln.spk or ln.prev.key[0] != ln.key[0]:  # sessão nova: locutores recomeçam
            parts += [self._nome(ln.spk) + "\n", f"h{8 if ln.spk is None else ln.spk % 8}"]
        if ln.pt:
            parts += [ln.pt + "\n", "pt" if ln.final else "ptp"]
        if ln.en:
            parts += [ln.en + "\n", "en" if ln.final else "enp"]
        at = t.index(ln.next.mark if ln.next else "end-1c")  # fim da região = início da próxima linha
        t.insert(at, *parts)
        if ln.fresh:
            ln.fresh = False
            t.mark_set(ln.mark, at)
        else:
            t.delete(ln.mark, at)

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
        extra = len(self.lines) - MAX_LINHAS
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

    def _status(self, s):
        self.stat.configure(text=s.text, foreground=NIVEIS.get(s.level, NIVEIS["info"]))

    @staticmethod
    def _nome(spk):
        return "Locutor ?" if spk is None else f"Locutor {spk + 1}"

    def run(self):
        self._tick()
        self.root.mainloop()

    def _close(self):
        self._save()
        self.root.withdraw()  # some na hora; on_stop pode levar até ~1 s
        self.ctl.shutdown(wait=False, cancel_futures=True)  # um Iniciar ainda na fila não pode rodar depois de fechar
        try:
            self.on_stop()
        except Exception as e:
            print(f"on_stop falhou: {e}", file=sys.stderr)
        if self.job:
            self.root.after_cancel(self.job)
        self.root.destroy()


# ---------------------------------------------------------------- demo e teste de estresse
DIALOGO = [  # (locutor, EN, PT, [locutor chutado nos parciais]); PT vazio = linha descartada
    (0, "Hello everyone, thanks for joining the call today.", "Olá a todos, obrigado por entrarem na chamada hoje."),
    (0, "We have a lot to cover, so let's get started.", "Temos muito a cobrir, então vamos começar."),
    (1, "Sure. First, the new build is live in production.", "Claro. Primeiro, a nova versão já está no ar em produção."),
    (1, "But we noticed some lag on the login page.", "Mas notamos um pouco de lag na página de login."),
    (2, "Yeah.", "É."),
    (2, "I can take a look at it this afternoon, if that works.", "Posso dar uma olhada nisso hoje à tarde, se estiver bom.", 0),
    (1, "you", ""),
    (0, "Perfect, thank you. Any other blockers?", "Perfeito, obrigado. Mais algum impedimento?"),
    (2, "Quick question: did the ping improve after the deploy?", "Uma pergunta rápida: o ping melhorou depois do deploy?"),
    (1, "Yes, it dropped from two hundred to about eighty milliseconds.", "Sim, caiu de duzentos para cerca de oitenta milissegundos."),
    (2, "Nice.", "Legal."),
    (0, "Great work, team. Let's wrap up and send the notes by tonight.", "Ótimo trabalho, pessoal. Vamos encerrar e enviar as anotações até a noite."),
]


def _roteiro(q, run, stop):
    """Thread geradora do demo: parciais crescendo palavra a palavra (~0,6 s) e depois o final."""
    rng, uid = Random(7), 0
    for spk, en, pt, *chute in itertools.cycle(DIALOGO):
        while not run.wait(0.1):  # parado: espera o "Iniciar"
            if stop.is_set():
                return
        ew, pw, t0, k = en.split(), pt.split(), time.monotonic(), 0

        def up(final, e, p, s):
            now = time.monotonic()
            q.put(Update(uid, s, e, p, final, t0, now - (rng.uniform(0.6, 1.0) if final else 0.3), now))
        if not pt:  # ruído: aparece um parcial e o final manda apagar
            up(False, en, "você", None)
            stop.wait(0.8)
        while k < len(ew) - 1:
            k = min(k + rng.choice((1, 2)), len(ew) - 1)
            up(False, " ".join(ew[:k]), " ".join(pw[:round(k * len(pw) / len(ew)) or 1]), None if k < 3 else (chute or [spk])[0])
            stop.wait(0.6)
        stop.wait(0.5)  # silêncio de fim + ASR final
        up(True, en if pt else "", pt, spk)
        uid += 1
        if stop.wait(rng.uniform(0.6, 1.2)):
            return


def _demo():
    q, run, stop = queue.Queue(), threading.Event(), threading.Event()
    nomes = ["Alto-falantes (Realtek High Definition Audio)", "LG HDR WFHD (NVIDIA High Definition Audio)", "NVIDIA HDMI Output"]
    start = lambda d: (q.put(Status("Pronto — escutando o áudio do PC (demo)", "ready")), run.set())
    app = App(q, get_devices=lambda: nomes, on_start=start, on_stop=run.clear)
    th = threading.Thread(target=_roteiro, args=(q, run, stop), name="demo")
    th.start()
    app.root.after(400, app._toggle)  # como se o usuário clicasse em Iniciar
    try:
        app.run()
    finally:
        stop.set()
        th.join(3)


def _stress():
    q = queue.Queue()
    app = App(q, get_devices=lambda: [], on_start=lambda d: None, on_stop=lambda: None)
    app.root.geometry("900x600+60+60")
    U = lambda i, s, en, pt, fin: Update(i, s, en, pt, fin, 0.0, 1.0, 1.8)
    txt = lambda: app.text.get("1.0", "end-1c")

    def feed(*us):  # aplica já, sem esperar o tick
        for u in us:
            q.put(u)
        app._drain()
    # lógica: cabeçalho só quando o locutor muda; None -> N; rótulo corrigido; descarte; parcial atrasada; Limpar
    feed(U(0, 0, "aa", "AA", True), U(1, None, "bb", "BB", False), U(2, 0, "cc", "CC", True))
    assert txt().count("Locutor") == 3 and "Locutor ?" in txt()
    feed(U(1, 0, "bb", "BB", True))  # BB vira Locutor 1: somem os cabeçalhos de BB e de CC
    assert txt().count("Locutor") == 1 and txt().index("AA") < txt().index("BB") < txt().index("CC"), txt()
    feed(U(1, 0, "", "", True), U(0, 0, "aa", "XX", False))  # descarta BB; parcial depois do final é ignorada
    assert "BB" not in txt() and "XX" not in txt() and txt().count("Locutor") == 1
    feed(U(0, 1, "aa", "AA", True))  # AA vira Locutor 2: ganha cabeçalho e CC (Locutor 1) também
    assert txt().count("Locutor") == 2 and app.lag.cget("text") == "atraso ~0,8 s", txt()
    n = len(app.lags)  # final dividido: a 1ª linha acabou 5 s antes (resto da frase), mas só a última (0,8 s) entra no atraso
    feed(Update(8, 0, "dd", "DD", True, 0.0, 0.0, 5.0), Update(EXTRA_ID, 1, "ee", "EE", True, 0.0, 1.0, 1.8))
    assert len(app.lags) == n + 1 and app.lag.cget("text") == "atraso ~0,8 s", list(app.lags)
    app.clear()
    assert not txt() and not app.lines
    # rajadas: 2 x (1500 utterances x parcial + final) = 2 x 3000 updates; a 2ª passa de MAX_LINHAS e poda
    gaps, last = [], [time.perf_counter()]

    def beat():  # batimento de 2 ms: o maior intervalo entre batimentos = pior travada da UI
        now = time.perf_counter()
        gaps.append(now - last[0])
        last[0] = now
        app.root.after(2, beat)
    app.root.update()
    beat()
    app._tick()
    for nome, base in (("rajada 1", 0), ("rajada 2", 1500)):
        d0, b0, t0, g0 = app.done, app.busy, time.perf_counter(), len(gaps)
        for i in range(base, base + 1500):
            en, pt = f"this is test sentence number {i} with a few words", f"esta é a frase de teste número {i} com algumas palavras"
            q.put(U(i, None, en[:24], pt[:26], False))
            q.put(U(i, i // 2 % 3, en, pt, True))
        while not q.empty():
            app.root.update()
        app.root.update()
        print(f"{nome}: {app.done - d0} updates em {time.perf_counter() - t0:.2f} s | {(app.busy - b0) / (app.done - d0) * 1e6:.0f} "
              f"us/update | pior travada da UI {max(gaps[g0:]) * 1e3:.0f} ms | linhas na tela {len(app.lines)}")
    app._check()
    assert len(app.lines) == MAX_LINHAS and len(app.arquivo) == 1000 and app.follow and app.text.yview()[1] > 0.999
    assert not app.text.tag_ranges("ptp")  # nenhum parcial sobrou
    for ln in app.lines.values():  # cabeçalho exatamente onde o locutor muda
        tem = any(re.fullmatch(r"h\d", g) for g in app.text.tag_names(app.text.index(ln.mark)))
        assert tem == (ln.prev is None or ln.prev.spk != ln.spk), ln
    app.text.yview_moveto(0.3)  # usuário rolou para cima: pausa o autoscroll
    app._check()
    feed(U(9999, 0, "late", "TARDE", True))
    assert not app.follow and app.text.yview()[1] < 0.99
    app._end()
    assert app.follow and app.text.yview()[1] > 0.999
    print("ok")
    app.root.destroy()


if __name__ == "__main__":
    if "--stress" in sys.argv:
        _stress()
    elif "--demo" in sys.argv:
        _demo()
    else:
        print("Uso: python -m app.ui --demo | --stress  (a UI real é montada por app.main)")
