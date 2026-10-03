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
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "TranslateAPP"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "TranslateAPP"


DATA_DIR = _data_dir()
MODELS = DATA_DIR / "models"
LOGS = DATA_DIR / "logs"
SETTINGS = DATA_DIR / "settings.json"
