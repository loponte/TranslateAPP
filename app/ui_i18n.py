"""Textos da UI em português e inglês. `t(lang, chave, **kw)`; o idioma padrão vem do sistema (`system_lang`)."""
from __future__ import annotations

import sys

MAC = sys.platform == "darwin"

STR = {
    "pt": {
        # onboarding e tutorial
        "pick_title": "Escolha seu idioma",
        "lang_en_sub": "Interface em inglês", "lang_pt_sub": "Interface em português",
        "continue": "Continuar", "skip": "Pular", "back": "Voltar", "next": "Próximo", "begin": "Começar",
        "how": "COMO FUNCIONA",
        "tour": [
            ("Escolha o que ouvir.", "Capture todo o som do PC ou só um app, como Discord, Zoom ou o navegador. "
                                     "O resto fica de fora da legenda."),
            ("Escolha os idiomas.", "Diga em que língua está a call (ou deixe no Automático) e escolha a legenda "
                                    "em português ou inglês."),
            ("A legenda flutua.", "Ao iniciar, a janela vira uma legenda de cinema. Arraste para mover, puxe as bordas "
                                  "para redimensionar e passe o mouse para ajustar fonte, opacidade e \"sempre no topo\"."),
            ("Cada voz tem nome e cor.", "Clique no nome para renomear; o app lembra a voz nas próximas calls. Tudo roda "
                                         "no seu computador: o áudio não sai da máquina."),
        ],
        "tour_local": "100 % local",
        # principal
        "tagline": "Legendas ao vivo para qualquer call",
        "tagline_sub": "Transcrição, tradução e locutores rodando no seu computador.",
        "card_audio": "01 · ÁUDIO", "audio_h": "O que ouvir",
        "src_all": "Saída inteira", "src_app": "Um app",
        "device": "Dispositivo de saída", "default_device": "Padrão do sistema" if MAC else "Padrão do Windows",
        "apps": "Apps tocando som", "no_apps": "Nenhum app tocando som agora. Dê play na call e clique em atualizar.",
        "playing": "tocando", "silent": "em silêncio",
        "card_lang": "02 · IDIOMAS", "lang_h": "Idiomas",
        "call_lang": "Idioma da call", "sub_lang": "Legenda em",
        "auto": "Auto", "English": "English", "Português": "Português",
        "detected": "Detectado na call: {lang}", "detected_none": "O idioma detectado aparece aqui durante a call.",
        "lang_name": {"en": "inglês", "pt": "português"},
        "start": "Iniciar legenda", "show_sub": "Voltar à legenda", "apply": "Aplicar e voltar",
        "stop": "Parar", "transcript": "Ver transcrição",
        "pick_app": "Escolha um app na lista (ou use a saída inteira).",
        "use_all": "Usar a saída inteira",
        # status (D13: o pipeline manda pt-BR; a UI traduz pelo nível)
        "st_idle": "Parado. Escolha o áudio e clique em Iniciar legenda.", "st_stopped": "Parado.",
        "st_starting": "Iniciando…", "st_loading": "Carregando modelos…",
        "st_download": "Baixando modelos", "st_ready": "Pronto — escutando {src}",
        "lag": "atraso ~{s} s", "live": "AO VIVO", "waiting": "Ouvindo {src}. As legendas aparecem aqui.",
        "first_use": "Só no primeiro uso: os modelos ficam salvos neste computador.",
        # overlay
        "rename": "Renomear…", "merge": "Juntar com", "forget": "Esquecer voz", "open_main": "Abrir janela principal",
        "stop_sub": "Parar legenda", "save_txt": "Salvar transcrição (.txt)…", "save_srt": "Salvar legenda (.srt)…",
        "quit": "Sair", "show_orig": "Mostrar a fala original", "topmost": "Sempre no topo", "snap": "Encaixar embaixo",
        "speaker": "Locutor {n}", "speaker_unk": "Locutor ?",
        # configurações
        "settings": "Configurações",
        "sec": ["Geral", "Legenda", "Locutores", "Áudio", "Modelos"],
        "app_lang": "Idioma do app", "tutorial": "Tutorial", "tutorial_sub": "Reveja como o app funciona.",
        "replay": "Rever tutorial",
        "font": "Tamanho da fonte", "opacity": "Opacidade",
        "lines": "Linhas na legenda", "hide_capture": "Ocultar ao compartilhar a tela",
        "hide_capture_sub": "Quem vê sua tela no Discord, Zoom ou Meet não vê a legenda.",
        "position": "Posição da legenda", "recenter": "Encaixar embaixo",
        "spk_note": "Renomear um locutor salva a voz dele só neste computador, para reconhecê-la nas próximas calls. "
                    "Nada sai da máquina, e você pode esquecer a voz quando quiser.",
        "no_voices": "Nenhuma voz salva ainda. Clique no nome de um locutor na legenda para renomeá-lo.",
        "forget_all": "Esquecer todas as vozes", "forget_one": "Esquecer", "rename_btn": "Renomear",
        "audio_note": "A saída inteira captura tudo o que toca no computador. \"Um app\" captura só o app escolhido.",
        "app_capture_off": "A captura por app precisa do Windows 10 versão 2004 ou mais novo.",
        "app_capture_on": "Captura por app disponível neste computador.",
        "models_where": "Os modelos ficam em:", "open_folder": "Abrir pasta",
        "models_note": "A aceleração é escolhida sozinha: placa de vídeo NVIDIA se houver, senão o processador.",
        # transcrição
        "tr_title": "Transcrição", "clear": "Limpar", "paused": "Rolagem pausada — clique para ir ao fim",
        "nothing": "Não há nada para salvar.", "saved": "Transcrição salva em {p}", "save_fail": "Não foi possível salvar: {e}",
        "txt": "Texto", "all_files": "Todos os arquivos",
        "dev_fail": "Não foi possível listar os dispositivos: {e}",
    },
    "en": {
        "pick_title": "Choose your language",
        "lang_en_sub": "Interface in English", "lang_pt_sub": "Interface in Portuguese",
        "continue": "Continue", "skip": "Skip", "back": "Back", "next": "Next", "begin": "Get started",
        "how": "HOW IT WORKS",
        "tour": [
            ("Choose what to listen to.", "Capture all of your PC's audio or just one app, like Discord, Zoom or your "
                                          "browser. Everything else stays out of the subtitles."),
            ("Pick your languages.", "Set the call's language (or leave it on Auto) and choose subtitles in English "
                                     "or Portuguese."),
            ("Subtitles float.", "Once you start, the window turns into a movie-style caption. Drag to move, pull the "
                                 "edges to resize, and hover to change font size, opacity and always-on-top."),
            ("Every voice gets a name and color.", "Click a name to rename it; the app remembers that voice next time. "
                                                   "Everything runs on your computer: no audio leaves your machine."),
        ],
        "tour_local": "100% local",
        "tagline": "Live subtitles for any call",
        "tagline_sub": "Transcription, translation and speakers running on your computer.",
        "card_audio": "01 · AUDIO", "audio_h": "What to listen to",
        "src_all": "All audio", "src_app": "One app",
        "device": "Output device", "default_device": "System default" if MAC else "Windows default",
        "apps": "Apps playing sound", "no_apps": "No app is playing sound right now. Start the call and hit refresh.",
        "playing": "playing", "silent": "silent",
        "card_lang": "02 · LANGUAGES", "lang_h": "Languages",
        "call_lang": "Call language", "sub_lang": "Subtitles in",
        "auto": "Auto", "English": "English", "Português": "Portuguese",
        "detected": "Detected in the call: {lang}", "detected_none": "The detected language shows up here during the call.",
        "lang_name": {"en": "English", "pt": "Portuguese"},
        "start": "Start subtitles", "show_sub": "Back to subtitles", "apply": "Apply and go back",
        "stop": "Stop", "transcript": "View transcript",
        "pick_app": "Pick an app from the list (or use all audio).",
        "use_all": "Use all audio",
        "st_idle": "Stopped. Choose the audio and click Start subtitles.", "st_stopped": "Stopped.",
        "st_starting": "Starting…", "st_loading": "Loading models…",
        "st_download": "Downloading models", "st_ready": "Ready — listening to {src}",
        "lag": "delay ~{s} s", "live": "LIVE", "waiting": "Listening to {src}. Subtitles show up here.",
        "first_use": "First run only: the models stay saved on this computer.",
        "rename": "Rename…", "merge": "Merge with", "forget": "Forget voice", "open_main": "Open main window",
        "stop_sub": "Stop subtitles", "save_txt": "Save transcript (.txt)…", "save_srt": "Save subtitles (.srt)…",
        "quit": "Quit", "show_orig": "Show original speech", "topmost": "Always on top", "snap": "Snap to bottom",
        "speaker": "Speaker {n}", "speaker_unk": "Speaker ?",
        "settings": "Settings",
        "sec": ["General", "Subtitles", "Speakers", "Audio", "Models"],
        "app_lang": "App language", "tutorial": "Tutorial", "tutorial_sub": "See how the app works again.",
        "replay": "Replay tutorial",
        "font": "Font size", "opacity": "Opacity",
        "lines": "Subtitle lines", "hide_capture": "Hide when sharing the screen",
        "hide_capture_sub": "People watching your screen on Discord, Zoom or Meet won't see the subtitles.",
        "position": "Subtitle position", "recenter": "Snap to bottom",
        "spk_note": "Renaming a speaker saves their voice on this computer only, so it's recognized in your next calls. "
                    "Nothing leaves your machine, and you can forget a voice any time.",
        "no_voices": "No saved voices yet. Click a speaker's name on the subtitles to rename them.",
        "forget_all": "Forget all voices", "forget_one": "Forget", "rename_btn": "Rename",
        "audio_note": "All audio captures everything playing on the computer. \"One app\" captures only the chosen app.",
        "app_capture_off": "Per-app capture needs Windows 10 version 2004 or newer.",
        "app_capture_on": "Per-app capture is available on this computer.",
        "models_where": "Models are stored in:", "open_folder": "Open folder",
        "models_note": "Acceleration is picked automatically: an NVIDIA graphics card if there is one, otherwise the CPU.",
        "tr_title": "Transcript", "clear": "Clear", "paused": "Scrolling paused — click to jump to the end",
        "nothing": "There is nothing to save.", "saved": "Transcript saved to {p}", "save_fail": "Could not save: {e}",
        "txt": "Text", "all_files": "All files",
        "dev_fail": "Could not list the devices: {e}",
    },
}


def t(lang: str, key: str, /, **kw):
    s = STR.get(lang, STR["en"]).get(key, key)
    return s.format(**kw) if kw else s


def system_lang() -> str:
    """'pt' se o idioma da interface do sistema é português; senão 'en'."""
    try:
        if sys.platform == "win32":
            import ctypes
            return "pt" if ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == 0x16 else "en"
        import locale
        return "pt" if (locale.getlocale()[0] or "").lower().startswith("pt") else "en"
    except Exception:
        return "en"


if __name__ == "__main__":  # check: as duas línguas têm as mesmas chaves
    assert STR["pt"].keys() == STR["en"].keys(), STR["pt"].keys() ^ STR["en"].keys()
    assert len(STR["pt"]["tour"]) == len(STR["en"]["tour"]) == 4
    print("ok", system_lang())
