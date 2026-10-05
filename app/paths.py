"""Pastas de dados. Empacotado (PyInstaller): pasta do usuário; rodando do código: raiz do repo (os testes não mudam)."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FROZEN = getattr(sys, "frozen", False)


def _data_dir() -> Path:
    if not FROZEN:
        return ROOT
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "TranslateAPP"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "TranslateAPP"


DATA_DIR = _data_dir()
MODELS = DATA_DIR / "models"
LOGS = DATA_DIR / "logs"
SETTINGS = DATA_DIR / "settings.json"


def download(url: str, sha256: str, dst, on_progress=None) -> None:
    """Baixa `url` para `dst` conferindo o SHA-256. on_progress(frac | None) a cada MB (None = tamanho desconhecido).
    Levanta se a rede falhar ou o hash não bater (o arquivo fica pela metade: use dst numa pasta temporária)."""
    import hashlib
    import urllib.request
    h = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=60) as r, open(dst, "wb") as f:
        total, got = int(r.headers.get("Content-Length") or 0), 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            h.update(chunk)
            got += len(chunk)
            if on_progress:
                on_progress(got / total if total else None)
    if h.hexdigest() != sha256:
        raise ValueError(f"checksum do download não confere: {url}")
