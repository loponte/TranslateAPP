"""Faz o CTranslate2 achar cuBLAS/cuDNN instalados via pip (nvidia-*-cu12) no Windows.
Chamar `setup_cuda_dlls()` ANTES de importar ctranslate2 / faster_whisper."""
import os
import sys
from pathlib import Path

_done = False


def setup_cuda_dlls() -> None:
    global _done
    if _done or sys.platform != "win32":
        return
    _done = True
    for base in map(Path, sys.path):
        nv = base / "nvidia"
        if not nv.is_dir():
            continue
        for pkg in nv.iterdir():
            bin_dir = pkg / "bin"
            if bin_dir.is_dir():
                os.add_dll_directory(str(bin_dir))
                # o CTranslate2 carrega as DLLs com LoadLibrary puro, que só enxerga o PATH
                os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ["PATH"]
