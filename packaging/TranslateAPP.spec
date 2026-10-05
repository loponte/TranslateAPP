# PyInstaller: `pyinstaller packaging/TranslateAPP.spec` (use scripts/build_win.ps1). onedir, sem console.
# Os modelos NÃO entram: baixados no 1º uso em DATA_DIR (app/paths.py). As DLLs CUDA (nvidia-*) também não (~1,3 GB).
# app/assets (fonte Outfit + OFL.txt + PNGs da UI) vai junto, em app/assets dentro do pacote.
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).parent

datas, binaries, hidden = [], [], []
for pkg in ("faster_whisper", "ctranslate2", "onnxruntime", "sherpa_onnx", "av", "sentencepiece", "tokenizers", "yaml"):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hidden += h
datas.append((str(ROOT / "app" / "assets"), "app/assets"))  # ui_overlay acha por Path(__file__).with_name("assets")
a = Analysis([str(ROOT / "app" / "main.py")], pathex=[str(ROOT)], binaries=binaries, datas=datas,
             hiddenimports=hidden + ["app.asr", "app.audio", "app.diar", "app.mt", "app.pipeline",
                                     "app.segmenter", "app.ui", "app.ui_overlay", "app.ui_i18n", "app.cuda_dlls", "app.paths"],
             excludes=["nvidia", "torch", "matplotlib", "pytest"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="TranslateAPP", console=False, upx=False,
          disable_windowed_traceback=True)   # sem janela de erro do bootloader (trava o CI)
coll = COLLECT(exe, a.binaries, a.datas, name="TranslateAPP", upx=False)

