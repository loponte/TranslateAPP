"""Backend de captura do macOS para app/audio.py: áudio do sistema (helper Swift com ScreenCaptureKit, macOS 13+) ou uma
entrada de áudio (sounddevice; ex.: BlackHole). Só entrega blocos brutos em `cap._q`; mono/16 kHz/grade de 32 ms é do
`_pump` do audio.py (o mesmo do Windows)."""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

from app.paths import ROOT

SYSTEM = "Áudio do sistema"
RATE, CH = 48_000, 2      # o que o helper escreve: float32 intercalado
BLOCK = RATE // 100 * CH * 4   # 10 ms por leitura
NO_PERM = ("Sem permissão de gravação de tela e áudio — libere o TranslateAPP em Ajustes do Sistema › Privacidade e "
           "Segurança › Gravação de Tela e Áudio do Sistema e reabra o app.")


def _helper() -> Path:
    """Empacotado: na pasta de binários do PyInstaller (Contents/Frameworks). Do código: build/sck_audio, compilado na hora se faltar."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "sck_audio"
    exe = ROOT / "build" / "sck_audio"
    if not exe.exists():
        exe.parent.mkdir(exist_ok=True)
        subprocess.run(["swiftc", "-O", str(ROOT / "native" / "sck_audio.swift"), "-o", str(exe)], check=True)
    return exe


def inputs() -> list[str]:
    import sounddevice as sd
    return [d["name"] for d in sd.query_devices() if d["max_input_channels"] > 0]


def _open_system(cap):
    proc = subprocess.Popen([str(_helper())], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)

    def read():
        while data := proc.stdout.read(BLOCK):
            cap._q.put_nowait((time.monotonic(), RATE, CH, data))

    threading.Thread(target=read, name="audio-sck", daemon=True).start()
    return proc


def _open_input(cap, name: str):
    import sounddevice as sd
    q = cap._q
    dev = next(d for d in sd.query_devices() if d["max_input_channels"] > 0 and d["name"] == name)
    ch, rate = min(2, dev["max_input_channels"]), int(dev["default_samplerate"])
    s = sd.InputStream(device=dev["index"], channels=ch, samplerate=rate, dtype="float32", blocksize=rate // 100,
                       callback=lambda data, n, t, st: q.put_nowait((time.monotonic(), rate, ch, data.tobytes())))
    s.start()
    return s


def manage(cap) -> None:
    """Thread de dispositivo (equivale ao `_manage` do Windows): abre, vigia e reabre a cada 1,5 s se cair."""
    src = last = None
    while not cap._stop.is_set():
        try:
            if src is None:
                want = cap.device
                if want is None or want == SYSTEM:
                    name = SYSTEM
                else:
                    s = want.lower()
                    names = inputs()
                    name = next((n for n in names if n.lower() == s), None) or next((n for n in names if s in n.lower()), None)
                    if name is None:
                        raise LookupError(f"Dispositivo de áudio '{want}' não encontrado — tentando de novo…")
                src = _open_system(cap) if name == SYSTEM else _open_input(cap, name)
                if name == SYSTEM:
                    cap._stop.wait(0.6)   # dá tempo ao helper de falhar (permissão) antes de anunciar "Capturando"
                    if src.poll() is not None:
                        msg = NO_PERM if src.returncode == 3 else f"A captura do sistema parou (código {src.returncode}) — reabrindo…"
                        src = None
                        raise RuntimeError(msg)
                cap.device_name = name
                cap._status(f"Capturando áudio de: {name}")
                last = None
            elif isinstance(src, subprocess.Popen) and src.poll() is not None:
                code = src.returncode
                src = None
                raise RuntimeError(NO_PERM if code == 3 else f"A captura do sistema parou (código {code}) — reabrindo…")
            elif not isinstance(src, subprocess.Popen) and not src.active:
                raise RuntimeError("A captura de áudio parou — reabrindo…")
        except Exception as e:
            _close(src)
            src = None
            msg = str(e) if isinstance(e, (RuntimeError, LookupError)) else f"Falha ao abrir o áudio ({e}) — tentando de novo…"
            if msg != last:
                cap._status(msg)
            last = msg
        cap._stop.wait(1.5 if src is None else 0.5)
    _close(src)


def _close(src) -> None:
    try:
        if isinstance(src, subprocess.Popen):
            src.terminate()
            src.wait(2)
        elif src is not None:
            src.stop()
            src.close()
    except Exception:
        pass
