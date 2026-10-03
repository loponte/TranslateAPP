"""Captura do áudio que sai do PC (WASAPI loopback) -> chunks float32 mono 16 kHz de `block_ms`, linha do tempo contínua.

Threads: callback do PortAudio (só enfileira) -> `_pump` (mono, 16 kHz, chunks, `on_audio`, preenche silêncio pelo
relógio) e `_manage` (abre/reabre o dispositivo, acompanha a saída padrão do Windows).
Reamostragem: PyAV/swr (o mesmo caminho do faster-whisper) em vez de soxr: ~1 ms de atraso fixo contra 11-43 ms do soxr
(medido com blocos de 10 ms), resposta plana até ~6,5 kHz e > 60 dB de rejeição acima de 9 kHz.
"""
from __future__ import annotations

import ctypes
import queue
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from typing import Callable

import av
import numpy as np
import pyaudiowpatch as pa

from app.events import SR

# ponytail: GRACE fixo; um dispositivo que entregue em rajadas > 32 ms geraria chunks de zeros falsos -> adaptar ao intervalo medido
GRACE = 0.032   # s: atraso tolerado na entrega do dado real antes de preencher o chunk com zeros
TOL = 0.064     # s: erro (relógio x dado recebido) acima do qual a linha do tempo salta em vez de seguir suavemente


@dataclass
class LoopbackDevice:
    index: int
    name: str
    is_default: bool


def _loopbacks(p: pa.PyAudio) -> list[dict]:
    """Dispositivos loopback WASAPI (dict do PortAudio) com o padrão do Windows primeiro."""
    devs = list(p.get_loopback_device_info_generator())
    try:
        dflt = p.get_default_wasapi_loopback()["index"]
    except Exception:
        dflt = None
    for d in devs:
        d["is_default"] = d["index"] == dflt
    return sorted(devs, key=lambda d: not d["is_default"])  # sort estável


def list_loopback_devices() -> list[LoopbackDevice]:
    p = pa.PyAudio()
    try:
        return [LoopbackDevice(d["index"], d["name"].removesuffix(" [Loopback]"), d["is_default"]) for d in _loopbacks(p)]
    finally:
        p.terminate()


_ole = ctypes.OleDLL("ole32")


def _default_render_id() -> str | None:
    """ID do endpoint de saída padrão (Core Audio via ctypes; a thread precisa ter COM inicializado). O PortAudio só
    reenumera ao reiniciar, então é isto que detecta a troca da saída padrão com a captura rodando."""
    vt = lambda o, i: ctypes.cast(ctypes.cast(o, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))[i]
    guid = lambda s: ctypes.create_string_buffer(uuid.UUID(s).bytes_le)
    enum, dev, wid = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    try:
        _ole.CoCreateInstance(guid("BCDE0395-E52F-467C-8E3D-C4579291692E"), None, 23,
                              guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), ctypes.byref(enum))  # MMDeviceEnumerator
        ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(
            vt(enum, 4))(enum, 0, 0, ctypes.byref(dev))   # GetDefaultAudioEndpoint(eRender, eConsole)
        ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
            vt(dev, 5))(dev, ctypes.byref(wid))           # IMMDevice::GetId
        s = ctypes.wstring_at(wid.value)
        _ole.CoTaskMemFree(ctypes.c_void_p(wid.value))
        return s
    except Exception:
        return None
    finally:
        for o in (dev, enum):
            if o.value:
                ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vt(o, 2))(o)   # Release


class LoopbackCapture:
    def __init__(self, on_audio: Callable[[np.ndarray, float], None], device: str | None = None,
                 on_status: Callable[[str], None] | None = None, block_ms: int = 32):
        self.on_audio, self.device, self.on_status = on_audio, device, on_status
        self.device_name = ""            # preenchido ao abrir (~0,1 s após start) e a cada reabertura
        self._n = block_ms * SR // 1000  # amostras por chunk
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        self._q = queue.Queue()
        self._threads = [threading.Thread(target=f, name=n, daemon=True)
                         for n, f in (("audio-pump", self._pump), ("audio-dev", self._manage))]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        if not self._threads:
            return
        self._stop.set()
        self._q.put(None)
        for t in self._threads:
            if t is not threading.current_thread():   # stop() chamado de dentro de on_audio/on_status
                t.join(5)
        self._threads = []

    def _status(self, msg: str) -> None:
        if self.on_status:
            try:
                self.on_status(msg)
            except Exception:
                traceback.print_exc()

    # ---- dispositivo ----
    def _open(self, p: pa.PyAudio):
        devs = _loopbacks(p)
        if self.device is None:
            d = devs[0] if devs else None
        else:
            s = self.device.lower()   # nome exato (o que a UI devolve) ganha da busca por substring
            d = next((x for x in devs if x["name"].removesuffix(" [Loopback]").lower() == s), None) \
                or next((x for x in devs if s in x["name"].lower()), None)
        if d is None:
            raise LookupError("Nenhuma saída de áudio encontrada — tentando de novo…" if self.device is None
                              else f"Dispositivo de áudio '{self.device}' não encontrado — tentando de novo…")
        ch, rate, q = int(d["maxInputChannels"]), int(d["defaultSampleRate"]), self._q

        def cb(data, frames, tinfo, flags):   # só enfileira; o resto é na thread _pump
            q.put_nowait((time.monotonic(), rate, ch, data))
            return None, pa.paContinue

        stream = p.open(format=pa.paFloat32, channels=ch, rate=rate, input=True, input_device_index=d["index"],
                        frames_per_buffer=rate // 100, stream_callback=cb)   # 10 ms = período do motor de áudio
        self.device_name = d["name"].removesuffix(" [Loopback]")
        return stream

    def _manage(self) -> None:
        try:   # o PortAudio/WASAPI exige COM nesta thread (ele só o inicializa na thread do 1º PyAudio() do processo)
            _ole.CoInitializeEx(None, 0)
            com = True
        except OSError:
            com = False   # já inicializado em outro modelo: serve igual
        p = stream = None
        dflt_id, last_msg = None, None

        def close():
            for obj, fn in ((stream, "stop_stream"), (stream, "close"), (p, "terminate")):
                try:
                    getattr(obj, fn)()
                except Exception:
                    pass

        while not self._stop.is_set():
            try:
                if stream is None:
                    p = pa.PyAudio()   # nova instância = dispositivos reenumerados (novo padrão incluso)
                    dflt_id = _default_render_id()
                    stream = self._open(p)
                    self._status(f"Capturando áudio de: {self.device_name}")
                    last_msg = None
                elif not stream.is_active():
                    raise RuntimeError("A captura de áudio parou — reabrindo…")
                elif self.device is None and dflt_id and _default_render_id() not in (None, dflt_id):
                    raise RuntimeError("A saída padrão do Windows mudou — reabrindo a captura…")
            except Exception as e:
                close()
                stream = p = None
                msg = str(e) if isinstance(e, (RuntimeError, LookupError)) else f"Falha ao abrir o áudio ({e}) — tentando de novo…"
                if msg != last_msg:   # não repete a mesma mensagem a cada tentativa
                    self._status(msg)
                last_msg = msg
            self._stop.wait(1.5 if stream is None else 0.5)
        close()
        if com:
            _ole.CoUninitialize()

    # ---- linha do tempo ----
    def _pump(self) -> None:
        """mono -> 16 kHz -> chunks. A linha do tempo é uma grade de `n` amostras ancorada no relógio: dado real entra
        nela; quando o chunk vence sem dado (loopback não entrega nada com tudo mudo) completa com zeros."""
        n, q = self._n, self._q
        anchor = time.monotonic()      # monotonic da amostra 0 da grade
        done, last = 0, 0.0            # amostras já entregues; último t_end entregue
        pend = np.empty(0, np.float32)
        fmt = rs = None
        realign = True                 # o próximo dado real realinha a grade (início ou depois de preencher zeros)

        def emit():
            nonlocal pend, done, last
            while len(pend) >= n:
                done += n
                last = max(anchor + done / SR, last + 1e-3)   # monotônico mesmo se a âncora recuar
                chunk, pend = pend[:n].copy(), pend[n:]
                try:
                    self.on_audio(chunk, last)
                except Exception:
                    traceback.print_exc()

        def due():   # quando o próximo chunk vence; já ocioso (realign) não há o que esperar: sem tolerância
            return anchor + (done + n) / SR + (0.0 if realign else GRACE)

        while True:
            items = []
            try:   # espera dado até o próximo chunk vencer (+ tolerância); depois drena o que já chegou
                items.append(q.get(timeout=max(0.0, due() - time.monotonic())))
                while True:
                    items.append(q.get_nowait())
            except queue.Empty:
                pass
            for it in items:
                if it is None:
                    return
                t_cb, rate, ch, data = it
                try:
                    if (rate, ch) != fmt:   # novo dispositivo/formato: resampler novo (o swr guarda estado)
                        fmt = (rate, ch)
                        rs = av.AudioResampler(format="flt", layout="mono", rate=SR) if rate != SR else None
                    d = np.frombuffer(data, np.float32).reshape(-1, ch)
                    # 5.1/7.1: o áudio de call costuma ir só em FL/FR (+ centro); a média simples atenuaria vários dB
                    x = d.mean(axis=1) if ch < 6 else d[:, :2].mean(axis=1) + 0.7 * d[:, 2]
                    if rs is not None:
                        f = av.AudioFrame.from_ndarray(x.reshape(1, -1), format="flt", layout="mono")
                        f.sample_rate = rate
                        x = np.concatenate([o.to_ndarray().reshape(-1) for o in rs.resample(f)] or [x[:0]])
                except Exception:
                    traceback.print_exc()
                    fmt = None
                    continue
                pend = np.concatenate((pend, x))
                d = t_cb - (anchor + (done + len(pend)) / SR)   # o fim deste dado aconteceu em ~t_cb
                if realign or abs(d) > TOL:
                    anchor, realign = anchor + d, False
                else:
                    anchor += 0.02 * d                            # segue a deriva entre o relógio do dispositivo e o do PC
                emit()
            if (lag := time.monotonic() - due()) > 1.0:   # salto do relógio (suspensão/hibernação): não despeja horas de zeros
                anchor, realign = anchor + lag, True
            while time.monotonic() >= due():   # chunk vencido sem dado: zeros
                pend = np.concatenate((pend, np.zeros(n - len(pend), np.float32)))
                realign = True
                emit()
