"""Where the application keeps its files. Everything lives inside the project folder."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"
VOICES_DIR = MODELS_DIR / "voices"
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "audio_cache"
DOCS_DIR = DATA_DIR / "documents"
EXTRACT_CACHE_DIR = DATA_DIR / "extraction_cache"
LOG_DIR = DATA_DIR / "logs"
SETTINGS_FILE = DATA_DIR / "settings.json"
README_HE = ROOT / "README_HE.md"


def ensure_dirs() -> None:
    for d in (MODELS_DIR, VOICES_DIR, DATA_DIR, CACHE_DIR, DOCS_DIR, EXTRACT_CACHE_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
