"""Pipeline de tempo real: captura -> fila -> segmenter -> worker único (ASR -> locutor -> MT) -> `out`.

Threads por sessão (start..stop): "pipe-main" (carrega modelos, abre a captura e roda o segmenter sobre a
fila de áudio; fecha tudo ao sair) e "pipe-work" (único consumidor de GPU). Tudo bloqueante: sem polling.
Final: tracker.segments() acha as trocas de locutor. 1 trecho = 1 linha (ASR do áudio todo); mais de 1 = cada trecho é
transcrito e vira uma linha (Update final) própria, todas no mesmo job (saem juntas, em ordem). Ver _final().
Imports reais só dentro de funções, então a lógica roda com stubs (tests/test_pipeline.py)."""
from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from app.events import SR, SegEvent, Status, Update

log = logging.getLogger("pipeline")
ROOT = Path(__file__).resolve().parents[1]
STOP_JOIN_S = 1.0  # teto do stop(): espera a captura fechar; job de GPU em andamento não é interrompível
EXTRA_ID = 1_000_000  # utt_id das linhas extras de um final dividido por locutor (o Segmenter conta de 0: não colide)


class _Fail(RuntimeError):
    """Falha de arranque com mensagem pronta (pt-BR) para o Status."""


def _why(e: BaseException) -> str:
    hint = " (rode setup.ps1)" if isinstance(e, (ImportError, FileNotFoundError)) else ""
    return f"{type(e).__name__}: {e}{hint}"


@dataclass
class _Utt:
    """Cache por utterance: rótulo de locutor e último EN/PT (reaproveitar e pular a MT)."""
    spk: int | None = None
    en: str = ""
    pt: str = ""
    tried: int = 0  # amostras do áudio na última tentativa de rótulo num parcial


class _Inbox:
    """Fila do worker. Finais: prioridade, FIFO, nunca descartados. Parciais: só o mais recente por
    utt_id, e morrem se o final da mesma utterance já está na fila."""

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._finals: deque[SegEvent] = deque()
        self._partials: dict[int, SegEvent] = {}  # a última chave inserida é a mais nova
        self._closed = False

    def put(self, ev: SegEvent) -> None:
        with self._cv:
            if ev.kind == "final":
                self._partials.pop(ev.utt_id, None)
                self._finals.append(ev)
            elif any(f.utt_id == ev.utt_id for f in self._finals):
                return
            else:
                self._partials.pop(ev.utt_id, None)
                self._partials[ev.utt_id] = ev
            self._cv.notify()

    def get(self) -> SegEvent | None:
        """Bloqueia até haver job; None depois de close() (o que sobrou na fila é descartado)."""
        with self._cv:
            while not self._closed:
                if self._finals:
                    return self._finals.popleft()
                if self._partials:
                    return self._partials.popitem()[1]
                self._cv.wait()
            return None

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    def __len__(self) -> int:
        return len(self._finals) + len(self._partials)


class _Session:
    """Tudo que vive de start() a stop(). As threads recebem a sessão (não leem self), então uma sessão
    antiga ainda terminando um job não interfere na nova."""

    def __init__(self) -> None:
        self.stop = threading.Event()
        self.live = threading.Event()      # captura aberta (stop() só espera por ela se estiver)
        self.ready = False                 # "Pronto — escutando X" já foi avisado
        self.inbox = _Inbox()
        self.audio: queue.SimpleQueue = queue.SimpleQueue()  # (chunk, t_end): captura -> segmenter
        self.ids = itertools.count(EXTRA_ID)  # utt_id inéditos para as linhas extras de um final dividido
        self.thread: threading.Thread | None = None

    def close(self) -> None:
        self.stop.set()
        self.inbox.close()
        self.audio.put(None)


class Pipeline:
    def __init__(self, out: queue.Queue, *, segmenter=None, transcriber=None, tracker=None, translator=None,
                 capture_factory=None, seg_kw=None, asr_kw=None, diar_kw=None, piece_pad_s: float = 0.0,
                 log_path: str | Path = ROOT / "logs" / "latency.csv"):
        """`out` recebe Update | Status. Instâncias injetadas (testes) substituem as reais;
        `*_kw` (calibração) vão para os construtores reais. capture_factory(on_audio, device=, on_status=) -> captura.
        piece_pad_s: folga de áudio de cada lado do trecho ao re-transcrever um final dividido por locutor."""
        self.out = out
        self.segmenter, self.asr, self.tracker, self.mt = segmenter, transcriber, tracker, translator
        self._capture_factory = capture_factory
        self._kw = {"seg": seg_kw or {}, "asr": asr_kw or {}, "diar": diar_kw or {}}
        self._pad = piece_pad_s
        self._log_path = Path(log_path)
        self._sess: _Session | None = None
        self._ctl = threading.RLock()       # start/stop chegam de threads diferentes (executor da UI, fechar a janela)
        self._load_lock = threading.Lock()  # Parar+Iniciar durante a carga não carrega os modelos 2x
        self._infer = threading.Lock()      # 1 job de inferência por vez, mesmo com sessão antiga terminando
        self._min_samples = SR
        self._err_t: dict[str, float] = {}

    # ---- API ----
    def start(self, device: str | None = None) -> None:
        """Volta já: tudo (carga de modelos, captura) acontece em thread. Reinicia se já estava rodando."""
        with self._ctl:
            self.stop()
            s = self._sess = _Session()
            s.thread = threading.Thread(target=self._run, args=(s, device), name="pipe-main", daemon=True)
            s.thread.start()

    def stop(self) -> None:
        """Idempotente. Volta em <= STOP_JOIN_S mesmo com job de GPU em andamento (a thread é daemon e o
        resultado é descartado). O que mais pode demorar é o stop() da captura, por isso o join tem teto."""
        with self._ctl:
            s, self._sess = self._sess, None
            if s is None:
                return
            s.close()
            if s.live.is_set():
                s.thread.join(STOP_JOIN_S)

    # ---- thread principal da sessão ----
    def _run(self, s: _Session, device: str | None) -> None:
        cap = None
        try:
            self._emit(s, Status("Carregando modelos…"))
            with self._load_lock:
                if s.stop.is_set():
                    return
                self._load()
            if s.stop.is_set():
                return
            with self._infer:  # um job da sessão anterior ainda pode estar usando o tracker
                self.segmenter.reset()
                self.tracker.reset()  # sessão nova = conversa nova: locutores recomeçam do 0
            self._min_samples = int(getattr(self.tracker, "min_audio_s", 1.0) * SR)
            threading.Thread(target=self._work, args=(s,), name="pipe-work", daemon=True).start()

            def on_status(text: str) -> None:
                if text.startswith("Capturando"):  # audio.py avisa assim quando (re)abre o dispositivo; o resto são problemas
                    self._ready(s, cap.device_name)
                else:
                    self._emit(s, Status(text, "warn"))
            try:
                factory = self._capture_factory
                if factory is None:
                    from app.audio import LoopbackCapture as factory
                cap = factory(lambda chunk, t_end: s.audio.put((chunk, t_end)), device=device, on_status=on_status)
                cap.start()
            except Exception as e:
                raise _Fail(f"Falha ao abrir a captura de áudio: {_why(e)}") from e
            s.live.set()
            while True:
                if not s.ready and cap.device_name:  # a captura real só preenche o nome ~0,1 s depois de start()
                    self._ready(s, cap.device_name)
                item = s.audio.get()
                if item is None or s.stop.is_set():
                    break
                try:
                    for ev in self.segmenter.feed(*item):
                        if ev.kind != "start":  # "start" não tem uso aqui: o 1º parcial já abre a linha na UI
                            s.inbox.put(ev)
                except Exception as e:  # um chunk ruim não pode matar a captura
                    self._error(s, "segmentação", e)
        except Exception as e:
            log.exception("falha na sessão")
            self._emit(s, Status(str(e) if isinstance(e, _Fail) else f"Erro ao iniciar: {_why(e)}", "error"))
        finally:
            s.close()
            if cap is not None:
                try:
                    cap.stop()
                except Exception:
                    log.exception("erro ao parar a captura")

    def _load(self) -> None:
        """Cria o que não foi injetado. Fica no self: Parar+Iniciar não recarrega nada."""
        from app.cuda_dlls import setup_cuda_dlls
        setup_cuda_dlls()  # antes de importar ctranslate2/faster_whisper

        def asr():
            from app.asr import Transcriber
            m = Transcriber(**self._kw["asr"])
            m.warmup()
            return m

        def mt():
            from app.mt import Translator
            m = Translator()
            m.translate("Hello.")  # esquenta kernels: a 1ª tradução real não paga a inicialização
            return m

        def tracker():
            from app.diar import SpeakerTracker
            return SpeakerTracker(**self._kw["diar"])

        def segmenter():
            from app.segmenter import Segmenter
            return Segmenter(**self._kw["seg"])

        # ponytail: carga sequencial; paralelizar (ThreadPool) se o tempo de abertura incomodar
        for name, attr, make in (("Whisper", "asr", asr), ("tradutor", "mt", mt),
                                 ("locutores", "tracker", tracker), ("VAD", "segmenter", segmenter)):
            if getattr(self, attr) is None:
                try:
                    setattr(self, attr, make())
                except Exception as e:
                    raise _Fail(f"Falha ao carregar {name}: {_why(e)}") from e

    # ---- worker de inferência ----
    def _work(self, s: _Session) -> None:
        utts: dict[int, _Utt] = {}  # só esta thread mexe
        f = self._open_log()
        try:
            while (ev := s.inbox.get()) is not None:
                with self._infer:
                    if s.stop.is_set():
                        break
                    try:
                        self._job(s, ev, utts, f)
                    except Exception as e:  # rede de segurança: o worker nunca morre calado
                        self._error(s, "job", e)
                        if ev.kind == "final":  # não deixa a linha parcial presa na UI
                            self._discard(s, ev)
        finally:
            if f:
                f.close()

    def _job(self, s: _Session, ev: SegEvent, utts: dict[int, _Utt], log_f) -> None:
        st = utts.pop(ev.utt_id, None) or _Utt()
        if ev.kind != "final":
            utts[ev.utt_id] = st
            return self._partial(s, ev, st)
        # durações com perf_counter: time.monotonic() no Windows/Py3.12 tem resolução de 15,6 ms (GetTickCount64);
        # t_ready/lag usam monotonic porque t_end vem dele (contrato), então o lag_ms tem ±15 ms de granularidade
        tm = [0.0, 0.0, 0.0]  # s gastos em ASR, locutor e MT neste job (latency.csv)
        t_ready = self._final(s, ev, st, tm)
        if log_f:
            row = (ev.utt_id, len(ev.audio) / SR, *(x * 1e3 for x in tm), (t_ready - ev.t_end) * 1e3)
            try:
                log_f.write("%d,%.2f,%.0f,%.0f,%.0f,%.0f\n" % row)
                log_f.flush()
            except OSError:
                log.exception("sem espaço/acesso para latency.csv")

    def _partial(self, s: _Session, ev: SegEvent, st: _Utt) -> None:
        try:
            en = (self.asr.transcribe(ev.audio, False) or "").strip()
        except Exception as e:
            self._error(s, "ASR", e)
            return
        if not en:
            return  # parcial vazio: mantém o que já está na tela
        spk, n = st.spk, len(ev.audio)
        # palpite de locutor só com áudio suficiente e sem rótulo ainda (senão reaproveita o do cache). Voz ainda
        # desconhecida devolve None em todo parcial: só tenta de novo quando o áudio cresceu 50%
        if spk is None and n >= max(self._min_samples, st.tried * 3 // 2):
            st.tried = n
            spk = self._try(s, "locutor", lambda: self.tracker.identify(ev.audio, False))
        pt = st.pt
        if en != st.en or not pt:  # EN igual ao último: reaproveita o PT e poupa a MT
            pt = self._try(s, "tradução", lambda: self.mt.translate(en), pt)
        st.spk, st.en, st.pt = spk, en, pt
        self._emit(s, Update(ev.utt_id, spk, en, pt, False, ev.t0, ev.t_end, time.monotonic()))

    def _final(self, s: _Session, ev: SegEvent, st: _Utt, tm: list[float]) -> float:
        """Emite as linhas do final e devolve o t_ready da última. Com texto no último parcial a fala já está confirmada:
        os locutores vêm primeiro e, se a utterance for dividida, o ASR do áudio todo nem roda (poupa ~1 ASR por divisão).
        Sem texto no parcial (curta, ou o parcial foi pulado) o Whisper decide antes: vazio = ruído, descarta sem consultar
        os locutores (senão cada ruído criaria um locutor fantasma)."""
        d, memo = len(ev.audio) / SR, []

        def whole() -> str:  # EN do áudio todo (no máximo 1 ASR); ASR que quebra: fecha com o último parcial
            if not memo:
                got = self._asr(s, ev.audio, tm)
                memo.append(st.en if got is None else got)
            return memo[0]

        if not st.en and not whole():
            return self._discard(s, ev)
        t = time.perf_counter()
        segs = self._try(s, "locutor", lambda: self.tracker.segments(ev.audio))  # [(início_s, fim_s, locutor)]
        if segs is None:  # segments() falhou: a utterance inteira, rotulada como antes
            segs = [(0.0, d, self._try(s, "locutor", lambda: self.tracker.identify(ev.audio, True)))]
        tm[1] = time.perf_counter() - t
        rows = self._split(s, ev, segs, tm) if len(segs) > 1 else []
        split = bool(rows)
        if split:
            log.info("final %d dividido: %s", ev.utt_id, [(round(a, 2), round(b, 2), spk) for a, b, spk, _ in rows])
        elif whole():  # 1 trecho (ou não deu para dividir): o EN do áudio todo, com o rótulo do trecho mais longo
            spk = max(segs, key=lambda g: g[1] - g[0])[2]
            rows = [(0.0, d, st.spk if spk is None else spk, whole())]  # sem voz nova: mantém o palpite do parcial
        else:  # o ASR final veio vazio
            return self._discard(s, ev)
        t = time.perf_counter()
        pts = [st.pt if k == 0 and text == st.en and st.pt  # EN igual ao do último parcial: reaproveita o PT
               else self._try(s, "tradução", lambda: self.mt.translate(text), "" if split else st.pt)  # falha: mantém o EN
               for k, (_, _, _, text) in enumerate(rows)]
        tm[2] += time.perf_counter() - t
        base, t_ready = ev.t_end - d, 0.0
        for k, ((a, b, spk, text), pt) in enumerate(zip(rows, pts)):  # as linhas do final saem juntas, em sequência
            t_ready = time.monotonic()
            # o 1º trecho com texto troca a linha parcial (utt_id do evento); os outros são linhas novas
            self._emit(s, Update(ev.utt_id if k == 0 else next(s.ids), spk, text, pt, True,
                                 max(ev.t0, base + a), base + b, t_ready))
        return t_ready

    def _split(self, s: _Session, ev: SegEvent, segs: list, tm: list[float]) -> list[tuple]:
        """ASR de cada trecho de um final com troca de locutor: [(início_s, fim_s, locutor, EN)] dos que têm texto.
        [] (= não divide) se o ASR quebrar num trecho ou todos vierem vazios: segue a linha única do áudio todo."""
        n, pad, rows = len(ev.audio), self._pad, []
        for a, b, spk in segs:
            text = self._asr(s, ev.audio[max(0, round((a - pad) * SR)):min(n, round((b + pad) * SR))], tm)
            if text is None:
                return []
            if text:
                rows.append((a, b, spk, text))
        return rows

    def _asr(self, s: _Session, audio, tm: list[float]) -> str | None:
        """ASR final: o texto ('' se vazio) ou None se quebrou (avisado)."""
        t = time.perf_counter()
        try:
            return (self.asr.transcribe(audio, True) or "").strip()
        except Exception as e:
            self._error(s, "ASR", e)
            return None
        finally:
            tm[0] += time.perf_counter() - t

    def _discard(self, s: _Session, ev: SegEvent) -> float:
        """Alucinação/ruído: a UI apaga a linha parcial. Devolve o t_ready."""
        upd = Update(ev.utt_id, None, "", "", True, ev.t0, ev.t_end, time.monotonic())
        self._emit(s, upd)
        return upd.t_ready

    # ---- utilitários ----
    def _ready(self, s: _Session, device_name: str) -> None:
        s.ready = True
        log.info("pronto: whisper=%s em %s; escutando %s", getattr(self.asr, "model_name", "?"),
                 getattr(self.asr, "device", "?"), device_name)
        self._emit(s, Status(f"Pronto — escutando {device_name}", "ready"))

    def _try(self, s: _Session, where: str, fn, default=None):
        """Estágio auxiliar (locutor/MT): falhar não pode custar a legenda."""
        try:
            return fn()
        except Exception as e:
            self._error(s, where, e)
            return default

    def _error(self, s: _Session, where: str, exc: BaseException) -> None:
        now = time.monotonic()
        if now - self._err_t.get(where, -60.0) < 5.0:
            return  # falha repetida (a cada parcial/chunk): só o 1º aviso a cada 5 s, no log e na UI
        self._err_t[where] = now
        log.error("%s: %s", where, exc, exc_info=exc)
        self._emit(s, Status(f"Erro em {where}: {type(exc).__name__}: {exc}", "error"))

    def _emit(self, s: _Session, item: Update | Status) -> None:
        if not s.stop.is_set():  # sessão parada não escreve mais na UI
            self.out.put(item)

    def _open_log(self):
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            new = not self._log_path.exists() or self._log_path.stat().st_size == 0
            f = open(self._log_path, "a", encoding="utf-8")
            if new:
                f.write("utt_id,dur_s,asr_ms,spk_ms,mt_ms,lag_ms\n")
            return f
        except OSError:  # ex.: CSV aberto no Excel: roda sem log
            log.exception("não foi possível abrir %s", self._log_path)
            return None
