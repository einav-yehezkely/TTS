"""Model and voice files: what is needed, whether it is present, and downloading it.

This module is the only part of the application that uses the network, and only
when the user starts the one-time setup download.
"""

import hashlib
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .paths import MODELS_DIR, VOICES_DIR

_HF = "https://huggingface.co/onnx-community/Kokoro-82M-v1.0-ONNX-timestamped/resolve/dd4401a9add81ac692d20e240d22ec9dda82cc29"

MODEL_FILE = MODELS_DIR / "kokoro-v1.0-timestamped.onnx"
MODEL_SHA256 = "651ea8291843a92276a4a003581a215cb07d15e47dde6fcfb1b768f9a1682054"
MODEL_SIZE = 325_532_171
VOICE_SIZE = 522_240  # 510 x 256 float32 style vectors

# English voices of Kokoro-82M v1.0 (a = American English, b = British English)
VOICES = {
    "af_heart": "Heart (US, female)", "af_bella": "Bella (US, female)", "af_nicole": "Nicole (US, female)",
    "af_aoede": "Aoede (US, female)", "af_kore": "Kore (US, female)", "af_sarah": "Sarah (US, female)",
    "af_nova": "Nova (US, female)", "af_sky": "Sky (US, female)", "af_alloy": "Alloy (US, female)",
    "af_jessica": "Jessica (US, female)", "af_river": "River (US, female)",
    "am_michael": "Michael (US, male)", "am_fenrir": "Fenrir (US, male)", "am_puck": "Puck (US, male)",
    "am_echo": "Echo (US, male)", "am_eric": "Eric (US, male)", "am_liam": "Liam (US, male)",
    "am_onyx": "Onyx (US, male)", "am_adam": "Adam (US, male)",
    "bf_emma": "Emma (UK, female)", "bf_isabella": "Isabella (UK, female)", "bf_alice": "Alice (UK, female)",
    "bf_lily": "Lily (UK, female)",
    "bm_george": "George (UK, male)", "bm_fable": "Fable (UK, male)", "bm_lewis": "Lewis (UK, male)",
    "bm_daniel": "Daniel (UK, male)",
}
DEFAULT_VOICE = "af_heart"


@dataclass
class Asset:
    name: str
    url: str
    path: Path
    size: int
    sha256: str | None = None

    def present(self) -> bool:
        return self.path.exists() and self.path.stat().st_size == self.size


def required_assets() -> list[Asset]:
    items = [Asset("Kokoro-82M speech model", f"{_HF}/onnx/model.onnx", MODEL_FILE, MODEL_SIZE, MODEL_SHA256)]
    for v in VOICES:
        items.append(Asset(f"voice {v}", f"{_HF}/voices/{v}.bin", VOICES_DIR / f"{v}.bin", VOICE_SIZE))
    return items


def missing_assets() -> list[Asset]:
    return [a for a in required_assets() if not a.present()]


def installed_voices() -> list[str]:
    return [v for v in VOICES if (VOICES_DIR / f"{v}.bin").exists()]


class DownloadCancelled(Exception):
    pass


Progress = Callable[[int, int, str], None]  # (bytes done overall, bytes total overall, current item)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_all(progress: Progress, cancel: threading.Event, retries: int = 4) -> None:
    """Download every missing asset, resuming partial files. Raises on failure."""
    todo = missing_assets()
    total = sum(a.size for a in todo)
    done_before = 0
    for asset in todo:
        asset.path.parent.mkdir(parents=True, exist_ok=True)
        part = asset.path.with_suffix(asset.path.suffix + ".part")
        attempt = 0
        while True:
            if cancel.is_set():
                raise DownloadCancelled()
            have = part.stat().st_size if part.exists() else 0
            if have > asset.size:
                part.unlink()
                have = 0
            try:
                req = urllib.request.Request(asset.url, headers={"User-Agent": "kokoro-pdf-reader"})
                if have:
                    req.add_header("Range", f"bytes={have}-")
                with urllib.request.urlopen(req, timeout=30) as resp:
                    if have and resp.status != 206:  # server ignored the range, start over
                        have = 0
                    with open(part, "ab" if have else "wb") as out:
                        while True:
                            if cancel.is_set():
                                raise DownloadCancelled()
                            chunk = resp.read(1 << 16)
                            if not chunk:
                                break
                            out.write(chunk)
                            have += len(chunk)
                            progress(done_before + have, total, asset.name)
                if have != asset.size:
                    raise OSError(f"incomplete download of {asset.name} ({have} of {asset.size} bytes)")
                if asset.sha256 and _sha256(part) != asset.sha256:
                    part.unlink()
                    raise OSError(f"{asset.name} failed its integrity check, it will be downloaded again")
                part.replace(asset.path)
                break
            except DownloadCancelled:
                raise
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                attempt += 1
                if attempt > retries:
                    raise OSError(f"Could not download {asset.name}: {e}") from e
                time.sleep(min(2 ** attempt, 10))
        done_before += asset.size
        progress(done_before, total, asset.name)
