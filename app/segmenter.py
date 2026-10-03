"""Segmenter: VAD Silero (onnxruntime direto) + máquina de estados de utterances (start/partial/final).

Síncrono e determinístico: o áudio é re-bufferizado em janelas de 512 amostras (32 ms @16 kHz); cada janela vira
fala/silêncio por histerese (entra em `vad_threshold`, sai em `vad_threshold - 0.15`) e os tempos dos eventos
saem do `t_end` do chunk (sem relógio próprio). Todas as durações viram nº de janelas, arredondado para cima.

VAD: o modelo é o `silero_vad_v6.onnx` do faster-whisper (entradas input[seq,576]/h/c), copiado para
`models/silero_vad.onnx`; 1 janela por chamada (seq=1), estado LSTM (h, c) e contexto de 64 amostras mantidos aqui.
"""
from __future__ import annotations

import functools
import importlib.util
import math
import shutil
from pathlib import Path

import numpy as np
import onnxruntime as ort

from app.events import SR, SegEvent
from app.paths import MODELS

W = 512                 # amostras por janela do VAD (32 ms)
_CTX = 64               # contexto do Silero: últimas amostras da janela anterior
_MODEL = MODELS / "silero_vad.onnx"
_IDLE, _CAND, _ACT = range(3)


@functools.cache
def _session() -> ort.InferenceSession:
    path = _MODEL
    if not path.exists():  # o faster-whisper já traz o modelo: copia em vez de baixar
        path = Path(importlib.util.find_spec("faster_whisper").origin).parent / "assets" / "silero_vad_v6.onnx"
        try:
            _MODEL.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, _MODEL)
            path = _MODEL
        except OSError:
            pass  # pasta models/ sem escrita: usa o do próprio pacote
    o = ort.SessionOptions()
    o.intra_op_num_threads = o.inter_op_num_threads = 1  # 1 thread: roda no thread chamador, sem pool/spin
    o.log_severity_level = 4
    s = ort.InferenceSession(str(path), o, providers=["CPUExecutionProvider"])
    assert [i.name for i in s.get_inputs()] == ["input", "h", "c"], "silero_vad.onnx com interface inesperada"
    return s


class Segmenter:
    def __init__(self, *, vad_threshold=0.5, end_silence_ms=450, end_silence_long_ms=250, long_after_s=6.0,
                 max_utt_s=15.0, min_speech_ms=250, preroll_ms=250, tail_ms=150,
                 partial_every_ms=600, min_partial_ms=500):
        def w(ms):  # ms -> janelas (para cima)
            return max(1, math.ceil(ms * SR / 1000 / W))
        self._thr_in, self._thr_out = vad_threshold, max(vad_threshold - 0.15, 0.01)
        self._end_w, self._end_long_w, self._long_w = w(end_silence_ms), w(end_silence_long_ms), w(long_after_s * 1000)
        self._max_w, self._min_w, self._pre_w, self._tail_w = w(max_utt_s * 1000), w(min_speech_ms), w(preroll_ms), w(tail_ms)
        self._every_w, self._minp_w = w(partial_every_ms), w(min_partial_ms)
        self._gap_w = w(150)      # candidato some se ficar > 150 ms sem fala antes de juntar min_speech_ms
        self._pause_w, self._scan_w = w(100), w(4000)   # corte de utterance longa: pausa >= 100 ms nos últimos 4 s
        self._sess = _session()
        self._utt_id = 0          # continua entre reset()s para a UI nunca reusar um id
        self.reset()

    def reset(self) -> None:
        """Descarta tudo (VAD e utterance em curso) sem emitir eventos."""
        self._h = np.zeros((1, 1, 128), np.float32)
        self._c = np.zeros((1, 1, 128), np.float32)
        self._ctx = np.zeros(_CTX, np.float32)
        self._buf = np.empty(0, np.float32)   # sobra < W do último feed
        self._n = 0                           # amostras recebidas (inclui _buf)
        self._k = 0                           # próxima janela a processar
        self._t_end = 0.0
        self._flag = False                    # histerese: a janela anterior foi fala?
        self._rec: list[tuple[np.ndarray, bool]] = []   # (janela, fala) a partir de _r0: preroll em IDLE, utterance toda em CAND/ACT
        self._r0 = 0
        self._st = _IDLE

    def feed(self, chunk: np.ndarray, t_end: float) -> list[SegEvent]:
        x = np.asarray(chunk, np.float32).reshape(-1)
        self._n += len(x)
        self._t_end = t_end
        buf = np.concatenate((self._buf, x)) if len(self._buf) else x
        ev: list[SegEvent] = []
        i = 0
        while len(buf) - i >= W:
            win = buf[i:i + W].copy()          # fica guardada em _rec: não pode apontar para o buffer do chamador
            p, self._h, self._c = self._sess.run(None, {"input": np.concatenate((self._ctx, win))[None],
                                                        "h": self._h, "c": self._c})
            self._ctx = win[-_CTX:]
            self._flag = bool(p.flat[0] >= (self._thr_out if self._flag else self._thr_in))
            self._step(self._k, win, self._flag, ev)
            self._k += 1
            i += W
        self._buf = buf[i:].copy()
        return ev

    def _t(self, sample: int) -> float:
        """monotonic correspondente a uma amostra (1ª = 0), ancorado no t_end do último chunk."""
        return self._t_end - (self._n - sample) / SR

    def _audio(self, a: int, b: int) -> np.ndarray:
        """Janelas a..b (inclusive, índices absolutos) concatenadas."""
        return np.concatenate([w for w, _ in self._rec[a - self._r0:b - self._r0 + 1]])

    def _keep_preroll(self, k: int) -> None:
        del self._rec[:-self._pre_w]
        self._r0 = k + 1 - len(self._rec)

    def _step(self, k: int, w: np.ndarray, f: bool, ev: list) -> None:
        """Avança a máquina com a janela k (áudio w; f = fala)."""
        rec = self._rec
        if not rec:
            self._r0 = k
        rec.append((w, f))
        if self._st == _IDLE:
            if not f:
                if len(rec) > self._pre_w:    # em silêncio só guarda o preroll
                    del rec[0]
                    self._r0 += 1
                return
            self._st, self._k_on, self._sp, self._since, self._sil, self._last = _CAND, k, 1, 1, 0, k
        elif f:
            self._sp += 1
            self._since += 1
            self._sil = 0
            self._last = k
        else:
            self._sil += 1
        te = self._t((k + 1) * W)
        if self._st == _CAND:
            if self._sp >= self._min_w:       # fala suficiente: anuncia a utterance
                self._st, self._id, self._np = _ACT, self._utt_id, 0
                self._utt_id += 1
                self._t0 = self._t(self._k_on * W)
                ev.append(SegEvent("start", self._id, None, self._t0, te))
            elif self._sil > self._gap_w:     # rajada curta = ruído: descarta sem eventos
                self._st = _IDLE
                self._keep_preroll(k)
                return
            else:
                return
        if f and self._since >= (self._every_w if self._np else self._minp_w):
            self._since, self._np = 0, self._np + 1
            ev.append(SegEvent("partial", self._id, self._audio(self._r0, k), self._t0, te))
        dur = k + 1 - self._k_on
        if self._sil >= (self._end_long_w if dur >= self._long_w else self._end_w):
            self._final(min(self._last + self._tail_w, k), ev)   # preroll + fala + tail_ms de silêncio
            self._keep_preroll(k)
        elif dur >= self._max_w:
            self._cut(k, ev)

    def _final(self, e: int, ev: list) -> None:
        ev.append(SegEvent("final", self._id, self._audio(self._r0, e), self._t0, self._t((e + 1) * W)))
        self._st = _IDLE

    def _cut(self, k: int, ev: list) -> None:
        """Utterance >= max_utt_s: corta na última pausa (>= 100 ms) dos últimos ~4 s; sem pausa, corte seco."""
        rec, r0, run, a = self._rec, self._r0, 0, None
        for j in range(k, max(self._k_on, k - self._scan_w) - 1, -1):   # da janela mais nova para a mais antiga
            if not rec[j - r0][1]:
                run += 1
            elif run >= self._pause_w:
                a = j + 1                     # pausa = janelas a .. a+run-1
                break
            else:
                run = 0
        if a is None:
            self._final(k, ev)
            self._rec = []
            return
        left = rec[a - r0:]                   # do início da pausa em diante: vira o começo da próxima utterance
        # o VAD atrasa para reconhecer o início da fala: nunca corta no fim da pausa (no máx. tail_ms, ou a metade dela)
        self._final(a + min(self._tail_w, run // 2) - 1, ev)
        self._rec = []
        for i, (w, f) in enumerate(left):     # reprocessa (sem rodar o VAD de novo): pode gerar start/partial já
            self._step(a + i, w, f, ev)
