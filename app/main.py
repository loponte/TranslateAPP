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
ASR_KW: dict = {}   # ex.: {"model": "parakeet-v3"}: Parakeet na CPU (sem GPU; mais rápido que o Whisper na CPU, erra mais)
AUTOSTART = True    # já começa a escutar ao abrir (depois do onboarding, com a última fonte); False = esperar o clique


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
    """Smoke test do pacote (`--selftest`): importa tudo, cria o VAD, lista dispositivos e apps (sem áudio não quebra), Tk oculto."""
    import tkinter

    import app.asr, app.diar, app.mt, app.pipeline, app.ui  # noqa: E401,F401
    from app.audio import APP_CAPTURE, list_audio_apps, list_loopback_devices
    from app.segmenter import Segmenter

    Segmenter()
    print("dispositivos:", [d.name for d in list_loopback_devices()])
    print("apps:", [a.id for a in list_audio_apps()] if APP_CAPTURE else "captura por app indisponível")
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
    from app.audio import APP_CAPTURE, list_audio_apps, list_loopback_devices
    from app.pipeline import Pipeline
    from app.ui import App

    out: queue.Queue = queue.Queue()
    pipe = Pipeline(out, seg_kw=SEG_KW, asr_kw=ASR_KW, diar_kw=DIAR_KW)
    # a UI é dona do settings.json e passa fonte e idiomas no on_start(device=, app=, call_lang=, sub_lang=)
    app = App(out, get_devices=lambda: [d.name for d in list_loopback_devices()],
              get_apps=list_audio_apps if APP_CAPTURE else None, get_speakers=pipe.saved_speakers,
              on_start=pipe.start, on_stop=pipe.stop, on_speaker=pipe.speaker, autostart=AUTOSTART)
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
