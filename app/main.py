"""Ponto de entrada: `python -m app.main` (ou run.bat). Monta UI + Pipeline; os modelos carregam em background."""
from __future__ import annotations

import logging
import os
import queue
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Calibração (ver README): vão direto para os construtores; vazio = padrão do módulo.
SEG_KW: dict = {}   # ex.: {"end_silence_ms": 350, "end_silence_long_ms": 200}: final mais rápido, mas corta frases nas pausas
DIAR_KW: dict = {}  # ex.: {"threshold": 0.45}: menor junta vozes parecidas; maior separa mais
ASR_KW: dict = {}   # ex.: {"model": "small.en"}: Whisper menor = mais rápido (e mais erros)
AUTOSTART = True    # já começa a escutar ao abrir (com o último dispositivo usado); False = esperar o clique em Iniciar


def _setup_logging() -> None:
    (ROOT / "logs").mkdir(exist_ok=True)
    f = open(ROOT / "logs" / "app.log", "a", encoding="utf-8", buffering=1)  # ponytail: sem rotação do log
    if sys.stderr is None:  # pythonw (sem console): bibliotecas que escrevem em stderr/stdout (tqdm...) quebrariam
        sys.stdout = sys.stderr = f
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=f)
    for noisy in ("faster_whisper", "httpx", "httpcore"):  # uma linha INFO por inferência: encheria o log
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if sys.stderr is not f:  # com console (python.exe): espelha o log na tela
        logging.getLogger().addHandler(logging.StreamHandler())


def main() -> None:
    os.chdir(ROOT)  # os módulos usam caminhos relativos (models/, logs/)
    _setup_logging()
    from app.audio import list_loopback_devices
    from app.pipeline import Pipeline
    from app.ui import App

    out: queue.Queue = queue.Queue()
    pipe = Pipeline(out, seg_kw=SEG_KW, asr_kw=ASR_KW, diar_kw=DIAR_KW)
    app = App(out, get_devices=lambda: [d.name for d in list_loopback_devices()],
              on_start=pipe.start, on_stop=pipe.stop)
    if AUTOSTART:
        app.root.after(300, app._toggle)  # como se o usuário clicasse em Iniciar
    try:
        app.run()
    finally:
        pipe.stop()


if __name__ == "__main__":
    main()
    logging.shutdown()
    os._exit(0)  # sem teardown do interpretador: um job de GPU em andamento (thread daemon) pode travar o fim
