"""Ponto de entrada: `python -m app.main` (ou run.bat). Monta UI + Pipeline; os modelos carregam em background."""
from __future__ import annotations

import logging
import multiprocessing
import os
import queue
import sys

from app.paths import DATA_DIR, LOGS

# Calibração (ver README): vão direto para os construtores; vazio = padrão do módulo.
SEG_KW: dict = {}   # ex.: {"end_silence_ms": 350, "end_silence_long_ms": 200}: final mais rápido, mas corta frases nas pausas
DIAR_KW: dict = {}  # ex.: {"threshold": 0.45}: menor junta vozes parecidas; maior separa mais
ASR_KW: dict = {}   # ex.: {"model": "small.en"}: Whisper menor = mais rápido (e mais erros)
AUTOSTART = True    # já começa a escutar ao abrir (com o último dispositivo usado); False = esperar o clique em Iniciar


def _setup_logging() -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    f = open(LOGS / "app.log", "a", encoding="utf-8", buffering=1)  # ponytail: sem rotação do log
    if sys.stderr is None:  # pythonw (sem console): bibliotecas que escrevem em stderr/stdout (tqdm...) quebrariam
        sys.stdout = sys.stderr = f
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=f)
    for noisy in ("faster_whisper", "httpx", "httpcore"):  # uma linha INFO por inferência: encheria o log
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if sys.stderr is not f:  # com console (python.exe): espelha o log na tela
        logging.getLogger().addHandler(logging.StreamHandler())


def selftest() -> None:
    """Smoke test do pacote (`--selftest`): importa tudo, cria o VAD, lista dispositivos (sem áudio não quebra), Tk oculto."""
    import tkinter

    import app.asr, app.diar, app.mt, app.pipeline, app.ui  # noqa: E401,F401
    from app.audio import list_loopback_devices
    from app.segmenter import Segmenter

    Segmenter()
    print("dispositivos:", [d.name for d in list_loopback_devices()])
    r = tkinter.Tk()
    r.withdraw()
    r.update()
    r.destroy()


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    os.chdir(DATA_DIR)  # os módulos usam caminhos relativos (models/, logs/)
    _setup_logging()
    if "--selftest" in sys.argv:
        try:   # resultado também em arquivo: no .exe windowed o stdout não chega ao CI
            selftest()
            msg, code = f"selftest ok {DATA_DIR}", 0
        except BaseException:
            import traceback
            msg, code = "selftest FALHOU\n" + traceback.format_exc(), 1
        (DATA_DIR / "selftest.txt").write_text(msg, encoding="utf-8")
        print(msg, flush=True)
        os._exit(code)
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
    multiprocessing.freeze_support()  # .app/.exe: sem isso o resource_tracker do multiprocessing reabre o app em cascata
    main()
    logging.shutdown()
    sys.stdout.flush()
    os._exit(0)  # sem teardown do interpretador: um job de GPU em andamento (thread daemon) pode travar o fim
