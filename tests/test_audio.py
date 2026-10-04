"""Teste MANUAL da captura loopback: TOCA ~8 s de som na saída padrão do Windows (e captura ~25 s no total).

    python tests\\test_audio.py                                   roda tudo e imprime os números
    set TRANSLATEAPP_AUDIO_MANUAL=1 && pytest tests\\test_audio.py  (sem a variável o pytest pula estes testes)
    set TRANSLATEAPP_AUDIO_DEVICE=LG                              (opcional) captura/zeros num dispositivo ocioso

Captura por app (Windows build 19041+): dois processos tocam tons diferentes (440 e 1000 Hz) por cópias renomeadas do
python.exe do venv (tone_a.exe/tone_b.exe; o som sai do python.exe filho, então também testa a árvore de processos) e
cada um é capturado pelo nome do exe. Mede isolamento, ritmo dos blocos, app fechando/reabrindo e app ausente.

Sem som tocando, o loopback de alguns dispositivos não entrega nada (a linha do tempo é preenchida com zeros pelo
relógio); outros entregam o que estiver tocando no PC, então a checagem de "tudo zero" só vale para um dispositivo
ocioso escolhido por TRANSLATEAPP_AUDIO_DEVICE.
"""
from __future__ import annotations

import os
import shutil
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
from app.audio import APP_CAPTURE, LoopbackCapture, list_audio_apps, list_loopback_devices  # noqa: E402
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


# ---- captura por app ----
def _tone_player(exe_name: str, freq: int, secs: float = 12.0) -> subprocess.Popen:
    """Toca um tom de `freq` Hz (amplitude 0,2; 48 kHz estéreo) por uma cópia renomeada do launcher do venv."""
    assert sys.prefix != sys.base_prefix, "rode com o python do .venv (o launcher renomeado precisa do pyvenv.cfg)"
    d = Path(tempfile.gettempdir()) / "translateapp_fv"
    (d / "Scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy(Path(sys.prefix) / "pyvenv.cfg", d)
    exe, wav = d / "Scripts" / exe_name, d / f"tone{freq}.wav"
    if not exe.exists():
        shutil.copy(sys.executable, exe)
    y = (0.2 * np.sin(2 * np.pi * freq * np.arange(int(48000 * secs)) / 48000) * 32767).astype("<i2")
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(2), w.setsampwidth(2), w.setframerate(48000)
        w.writeframes(np.repeat(y[:, None], 2, 1).tobytes())
    return subprocess.Popen([str(exe), "-c", f"import winsound; winsound.PlaySound(r'{wav}', winsound.SND_FILENAME)"])


def _kill(p: subprocess.Popen) -> None:
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)


def _band(x: np.ndarray, f0: float) -> float:
    """Fração da energia em f0 ± 20 Hz."""
    S = np.abs(np.fft.rfft(x.astype(np.float64))) ** 2
    f = np.fft.rfftfreq(len(x), 1 / SR)
    return float(S[(f > f0 - 20) & (f < f0 + 20)].sum() / max(S.sum(), 1e-20))


class _Rec:
    """LoopbackCapture(app=...) que guarda chunks, t_end e (instante, status)."""
    def __init__(self, app: str):
        self.chunks, self.ends, self.status = [], [], []
        self.cap = LoopbackCapture(lambda c, t: (self.chunks.append(c), self.ends.append(t)), app=app,
                                   on_status=lambda m: self.status.append((time.monotonic(), m)))

    def msgs(self) -> list[str]:
        return [m for _, m in self.status]


@manual
def test_lista_apps():
    if not APP_CAPTURE:
        return print("  captura por app indisponível neste Windows")
    pa = _tone_player("tone_a.exe", 440)
    try:
        time.sleep(1.0)
        ms = []
        for _ in range(10):
            t = time.perf_counter()
            apps = list_audio_apps()
            ms.append((time.perf_counter() - t) * 1e3)
    finally:
        _kill(pa)
    print(f"  {apps} | {np.median(ms):.1f} ms (mín {min(ms):.1f}, máx {max(ms):.1f})")
    # o som sai do python.exe filho do tone_a.exe (outro exe): a sessão aparece como python.exe
    assert any(a.id.lower() == "python.exe" and a.active for a in apps) and apps[0].active, apps
    assert np.median(ms) <= 30, ms


@manual
def test_app_isolamento_e_ritmo():
    if not APP_CAPTURE:
        return
    pa, pb = _tone_player("tone_a.exe", 440), _tone_player("tone_b.exe", 1000)
    ra, rb = _Rec("tone_a.exe"), _Rec("TONE_B.EXE")   # o nome do exe não diferencia maiúsculas
    try:
        time.sleep(0.5)
        ra.cap.start(), rb.cap.start()
        time.sleep(6.0)
        ra.cap.stop(), rb.cap.stop()
    finally:
        _kill(pa), _kill(pb)
    for r, f, g in ((ra, 440, 1000), (rb, 1000, 440)):
        x = np.concatenate(r.chunks)
        x = x[np.argmax(np.abs(x) > 0.01):][SR // 2:]   # do início do tom (+0,5 s) em diante
        sp = np.diff(r.ends) * 1e3
        rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
        print(f"  {r.cap.app}: {_band(x, f) * 100:.2f} % em {f} Hz, {_band(x, g) * 100:.4f} % em {g} Hz (o outro), rms {rms:.4f}"
              f" | {len(r.chunks)} blocos de {sorted({len(c) for c in r.chunks})}, passo mediano {np.median(sp):.2f} ms"
              f" (mín {sp.min():.1f}, máx {sp.max():.1f}) | {r.msgs()}")
        assert _band(x, f) >= 0.99 and _band(x, g) < 0.001 and 0.12 < rms < 0.16, (_band(x, f), _band(x, g), rms)
        assert {len(c) for c in r.chunks} == {512} and 31.7 <= np.median(sp) <= 33.3 and np.all(sp > 0), np.median(sp)
        assert r.msgs() == [f"Capturando áudio de: {r.cap.app[:-4]}"], r.status   # o launcher não tem FileDescription


@manual
def test_app_fecha_e_reabre():
    if not APP_CAPTURE:
        return
    r = _Rec("tone_a.exe")
    pa = _tone_player("tone_a.exe", 440)
    try:
        time.sleep(0.5)
        r.cap.start()
        time.sleep(2.0)
        _kill(pa)
        t_kill = time.monotonic()
        time.sleep(3.0)
        pa = _tone_player("tone_a.exe", 440)
        t_back = time.monotonic()
        time.sleep(3.5)
        r.cap.stop()
    finally:
        _kill(pa)
    ends = np.repeat(np.array(r.ends), 512)   # t_end de cada amostra (aprox.: o do seu chunk)
    x = np.concatenate(r.chunks)
    gone, back = x[(ends > t_kill + 0.8) & (ends < t_back)], x[ends > t_back + 1.5]
    reopen = next(t for t, m in r.status if t > t_back and m.startswith("Capturando")) - t_back
    sp = np.diff(r.ends)
    print(f"  {r.msgs()} | fechado: pico {np.abs(gone).max():.4f} | reabriu {reopen:.2f} s depois: {_band(back, 440) * 100:.1f} %"
          f" em 440 Hz | passo máx {sp.max() * 1e3:.1f} ms")
    assert r.msgs() == ["Capturando áudio de: tone_a", "tone_a não está aberto — esperando…", "Capturando áudio de: tone_a"], r.msgs()
    assert np.abs(gone).max() == 0 and _band(back, 440) >= 0.99 and reopen < 2.0
    assert np.all(sp > 0) and sp.max() < 0.1   # a linha do tempo não para


@manual
def test_app_ausente_avisa_e_zera():
    if not APP_CAPTURE:
        return
    r = _Rec("__nao_existe__.exe")
    r.cap.start()
    time.sleep(3.5)
    r.cap.stop()
    assert r.msgs() == ["__nao_existe__ não está aberto — esperando…"], r.msgs()   # avisa uma vez, não repete
    assert len(r.chunks) > 90 and max(np.abs(c).max() for c in r.chunks) == 0 and r.cap.device_name == ""
    assert not [t for t in threading.enumerate() if t.name.startswith("audio-")]


if __name__ == "__main__":
    for f in (test_list_devices, test_5s_sem_tocar_nada, test_toca_8s_e_segmenta, test_dispositivo_inexistente_avisa_e_para,
              test_troca_da_saida_padrao_reabre, test_lista_apps, test_app_isolamento_e_ritmo, test_app_fecha_e_reabre,
              test_app_ausente_avisa_e_zera):
        print(f.__name__)
        f()
        print("  ok")
    print("TODOS OK")
