"""Teste MANUAL da captura loopback: TOCA ~8 s de som na saída padrão do Windows (e captura ~25 s no total).

    python tests\\test_audio.py                                   roda tudo e imprime os números
    set TRANSLATEAPP_AUDIO_MANUAL=1 && pytest tests\\test_audio.py  (sem a variável o pytest pula estes testes)
    set TRANSLATEAPP_AUDIO_DEVICE=LG                              (opcional) captura/zeros num dispositivo ocioso

Sem som tocando, o loopback de alguns dispositivos não entrega nada (a linha do tempo é preenchida com zeros pelo
relógio); outros entregam o que estiver tocando no PC, então a checagem de "tudo zero" só vale para um dispositivo
ocioso escolhido por TRANSLATEAPP_AUDIO_DEVICE.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import subprocess
import wave
from pathlib import Path

import av
import numpy as np

if sys.platform == "win32":
    import winsound

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app.audio as audio  # noqa: E402
from app.audio import LoopbackCapture, list_loopback_devices  # noqa: E402
from app.events import SR  # noqa: E402
from app.segmenter import Segmenter  # noqa: E402

DEVICE = os.environ.get("TRANSLATEAPP_AUDIO_DEVICE") or None
FIXTURE = Path(__file__).parent / "data" / "conv_2spk_48k_stereo.wav"

try:
    import pytest
    manual = pytest.mark.skipif(not os.environ.get("TRANSLATEAPP_AUDIO_MANUAL"), reason="toca som; rode manualmente")
except ImportError:  # venv do projeto não tem pytest: roda como script
    manual = lambda f: f  # noqa: E731


def _capture(seconds: float, play=None, device=DEVICE, seg=None):
    """Captura `seconds` s (play() dispara o som 1,5 s depois do início). -> (chunks, t_end, eventos, status)."""
    chunks, ends, ev, status = [], [], [], []

    def on_audio(c, t):
        chunks.append(c)
        ends.append(t)
        if seg:
            ev.extend(seg.feed(c, t))

    cap = LoopbackCapture(on_audio, device=device, on_status=status.append)
    cap.start()
    if play:
        time.sleep(1.5)
        play()
        time.sleep(seconds - 1.5)
    else:
        time.sleep(seconds)
    cap.stop()
    return chunks, np.array(ends), ev, status


@manual
def test_list_devices():
    devs = list_loopback_devices()
    for d in devs:
        print(" ", d)
    assert devs and devs[0].is_default and sum(d.is_default for d in devs) == 1
    assert all(d.name and not d.name.endswith("[Loopback]") for d in devs)


@manual
def test_5s_sem_tocar_nada():
    chunks, ends, _, status = _capture(5.0)
    assert 150 <= len(chunks) <= 160, len(chunks)                 # 5 s / 32 ms = 156
    assert all(c.shape == (512,) and c.dtype == np.float32 for c in chunks)
    sp = np.diff(ends) * 1e3
    assert np.all(sp > 0) and abs(sp.mean() - 32) < 0.5 and sp.max() < 100, (sp.mean(), sp.max())
    peak = max(float(np.abs(c).max()) for c in chunks)
    if DEVICE:
        assert peak == 0.0, f"dispositivo {DEVICE!r} não estava ocioso (pico {peak})"
    print(f"  {len(chunks)} chunks de 512 | t_end monotônico, espaço médio {sp.mean():.2f} ms (mín {sp.min():.2f}, máx {sp.max():.2f})"
          f" | pico {peak:.4f}{' (havia áudio tocando no PC)' if peak else ' (tudo zero)'} | status: {status}")


def _trecho_8s(tmp: str) -> np.ndarray:
    """Grava os 8 primeiros segundos do fixture a 50% do nível em `tmp`; devolve a referência mono 16 kHz."""
    with wave.open(str(FIXTURE)) as w:
        ch, fr = w.getnchannels(), w.getframerate()
        pcm = np.frombuffer(w.readframes(fr * 8), np.int16)
    with wave.open(tmp, "wb") as w:
        w.setnchannels(ch), w.setframerate(fr), w.setsampwidth(2)
        w.writeframes((pcm * 0.5).astype(np.int16).tobytes())
    mono = (pcm.reshape(-1, ch).astype(np.float32) / 32768 * 0.5).mean(axis=1)
    f = av.AudioFrame.from_ndarray(mono.reshape(1, -1), format="flt", layout="mono")
    f.sample_rate = fr
    return np.concatenate([o.to_ndarray().reshape(-1) for o in av.AudioResampler(format="flt", layout="mono", rate=SR).resample(f)])


_proc = None


def _play(path):   # assíncrono: winsound no Windows, afplay no macOS
    global _proc
    if sys.platform == "win32":
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
    else:
        _proc = subprocess.Popen(["afplay", path])


def _stop_play():
    if sys.platform == "win32":
        winsound.PlaySound(None, winsound.SND_PURGE)
    elif _proc:
        _proc.terminate()


@manual
def test_toca_8s_e_segmenta():
    tmp = os.path.join(tempfile.gettempdir(), "translateapp_audio_test_8s.wav")
    ref = _trecho_8s(tmp)
    play = lambda: _play(tmp)
    if DEVICE:
        print("  (som tocado na saída PADRÃO; com TRANSLATEAPP_AUDIO_DEVICE só vale se for a mesma)")
    seg = Segmenter()
    chunks, ends, ev, _ = _capture(11.0, play=play, seg=seg)
    _stop_play()
    x = np.concatenate(chunks)
    env = lambda a: np.sqrt((a[:len(a) // 1600 * 1600].reshape(-1, 1600).astype(np.float64) ** 2).mean(axis=1))   # RMS de 100 ms
    ex, er = env(x), env(ref)
    corr, lag = max((np.corrcoef(ex[k:k + min(len(er), len(ex) - k)], er[:min(len(er), len(ex) - k)])[0, 1], k) for k in range(20))
    rms_cap = np.sqrt(np.mean(x[int(1.5 * SR):int(9.5 * SR)].astype(np.float64) ** 2))
    rms_ref = np.sqrt(np.mean(ref.astype(np.float64) ** 2))
    fin = [e for e in ev if e.kind == "final"]
    print(f"  correlação do envelope capturado x referência: {corr:.3f} (início em {lag * 100} ms) | RMS capturado {rms_cap:.4f} x referência {rms_ref:.4f}"
          f" | eventos: {[(e.kind, e.utt_id) for e in ev if e.kind != 'partial']}")
    assert corr >= 0.7 and 0.5 <= rms_cap / rms_ref <= 3, (corr, rms_cap, rms_ref)
    assert len(fin) >= 1 and fin[0].t_end > fin[0].t0


@manual
def test_dispositivo_inexistente_avisa_e_para():
    chunks, _, _, status = _capture(3.5, device="__nao_existe__")
    assert len(status) == 1 and "não encontrado" in status[0], status      # avisa uma vez, não repete a cada tentativa
    assert len(chunks) > 90                                                  # a linha do tempo não para
    assert not [t for t in threading.enumerate() if t.name.startswith("audio-")]


@manual
def test_troca_da_saida_padrao_reabre():
    real, state = audio._default_render_id, {"id": None}
    audio._default_render_id = lambda: state["id"] or real()                # simula o Windows trocando o padrão
    try:
        seen = []
        cap = LoopbackCapture(lambda c, t: seen.append(t), on_status=lambda s: seen.append(s))
        cap.start()
        time.sleep(1.0)
        state["id"] = "{outro-endpoint}"
        time.sleep(2.2)
        state["id"] = None
        cap.stop()
    finally:
        audio._default_render_id = real
    msgs = [s for s in seen if isinstance(s, str)]
    ts = np.array([s for s in seen if not isinstance(s, str)])
    assert any("mudou" in m for m in msgs) and sum("Capturando" in m for m in msgs) >= 2, msgs
    assert np.all(np.diff(ts) > 0) and np.diff(ts).max() < 0.5              # reabriu sozinho, sem buraco longo na linha do tempo


if __name__ == "__main__":
    for f in (test_list_devices, test_5s_sem_tocar_nada, test_toca_8s_e_segmenta, test_dispositivo_inexistente_avisa_e_para,
              test_troca_da_saida_padrao_reabre):
        print(f.__name__)
        f()
        print("  ok")
    print("TODOS OK")
