"""Faz o CTranslate2 achar cuBLAS/cuDNN (nvidia-*-cu12) no Windows: instaladas via pip (dev) ou baixadas no 1º uso para
DATA_DIR/cuda (.exe; as DLLs, ~1,3 GB, não cabem no pacote). Chamar `setup_cuda_dlls()` ANTES de importar ctranslate2 / faster_whisper."""
import ctypes
import hashlib
import json
import logging
import os
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

from app.paths import DATA_DIR

_done = False
CUDA_DIR = DATA_DIR / "cuda"
# versões baixadas no 1º uso (compatíveis com o requirements.txt: cublas >= 12.9, cudnn >= 9.2)
PKGS = (("nvidia-cublas-cu12", "12.9.2.10"), ("nvidia-cudnn-cu12", "9.27.0.42"))


def setup_cuda_dlls() -> None:
    global _done
    if _done or sys.platform != "win32":
        return
    _done = True
    for base in [*map(Path, sys.path), *([CUDA_DIR] if (CUDA_DIR / ".ok").exists() else [])]:   # só o download completo
        nv = base / "nvidia"
        if not nv.is_dir():
            continue
        for pkg in nv.iterdir():
            bin_dir = pkg / "bin"
            if bin_dir.is_dir():
                os.add_dll_directory(str(bin_dir))
                # o CTranslate2 carrega as DLLs com LoadLibrary puro, que só enxerga o PATH
                os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ["PATH"]


def has_nvidia() -> bool:
    try:
        ctypes.WinDLL("nvcuda.dll")  # driver NVIDIA
        return True
    except OSError:
        return False


def ensure_cuda(on_progress=None) -> bool:
    """Empacotado no Windows com GPU NVIDIA: baixa cuBLAS/cuDNN do PyPI para DATA_DIR/cuda (1ª vez). False = falhou
    (o chamador força CPU); True = ok ou não se aplica."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False) or not has_nvidia():
        return True
    if (CUDA_DIR / ".ok").exists():
        return True
    tmp, part = CUDA_DIR.with_name("cuda.tmp"), DATA_DIR / "cuda.whl.part"
    try:
        shutil.rmtree(tmp, ignore_errors=True)
        for i, (pkg, ver) in enumerate(PKGS):
            with urllib.request.urlopen(f"https://pypi.org/pypi/{pkg}/{ver}/json", timeout=30) as r:
                files = json.load(r)["urls"]
            w = next(f for f in files if f["filename"].endswith("win_amd64.whl"))
            with urllib.request.urlopen(w["url"], timeout=60) as r, open(part, "wb") as f:   # em disco: a wheel tem ~750 MB
                while chunk := r.read(1 << 20):
                    f.write(chunk)
                    if on_progress:
                        on_progress((i + f.tell() / w["size"]) / len(PKGS))
            if hashlib.sha256(part.read_bytes()).hexdigest() != w["digests"]["sha256"]:
                raise OSError(f"checksum de {pkg} não confere")
            with zipfile.ZipFile(part) as z:
                z.extractall(tmp)
        (tmp / ".ok").touch()
        shutil.rmtree(CUDA_DIR, ignore_errors=True)
        tmp.rename(CUDA_DIR)   # só aparece completo (com .ok)
        return True
    except Exception:
        logging.getLogger(__name__).exception("download do CUDA falhou; usando CPU")
        shutil.rmtree(tmp, ignore_errors=True)
        return False
    finally:
        part.unlink(missing_ok=True)
