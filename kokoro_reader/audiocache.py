"""On-disk cache of synthesized segments (FLAC audio + line timing), with a size limit."""

import hashlib
import json
import os
import threading
from pathlib import Path

import numpy as np
import soundfile as sf

from .assets import MODEL_SHA256
from .engine import SAMPLE_RATE, Synth


class AudioCache:
    def __init__(self, directory: Path, limit_mb: int = 2048):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.limit = limit_mb * 1024 * 1024
        self._lock = threading.Lock()
        self._size = sum(f.stat().st_size for f in self.dir.glob("*") if f.is_file())

    @staticmethod
    def key(voice: str, speed: float, pause: float, fragments: list[tuple[int, str]]) -> str:
        blob = json.dumps([MODEL_SHA256[:16], 2, voice, round(speed, 2), round(pause, 2), fragments],
                          ensure_ascii=False)
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()

    def get(self, key: str) -> Synth | None:
        audio_path = self.dir / f"{key}.flac"
        meta_path = self.dir / f"{key}.json"
        try:
            with open(meta_path, encoding="utf-8") as f:
                marks = [tuple(m) for m in json.load(f)["marks"]]
            audio, sr = sf.read(audio_path, dtype="float32")
            if sr != SAMPLE_RATE:
                return None
            os.utime(audio_path)  # most recently used survives eviction
            return Synth(np.ascontiguousarray(audio), marks)
        except (OSError, ValueError, KeyError, RuntimeError, sf.LibsndfileError):
            return None

    def put(self, key: str, synth: Synth) -> None:
        audio_path = self.dir / f"{key}.flac"
        meta_path = self.dir / f"{key}.json"
        try:
            sf.write(audio_path, np.clip(synth.audio, -1, 1), SAMPLE_RATE, format="FLAC", subtype="PCM_16")
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump({"marks": synth.marks}, f)
            added = audio_path.stat().st_size + meta_path.stat().st_size
        except OSError:
            return
        with self._lock:
            self._size += added
            if self._size > self.limit:
                self._evict()

    def _evict(self) -> None:
        files = sorted(self.dir.glob("*.flac"), key=lambda f: f.stat().st_mtime)
        target = self.limit * 0.8
        for f in files:
            if self._size <= target:
                break
            for p in (f, f.with_suffix(".json")):
                try:
                    self._size -= p.stat().st_size
                    p.unlink()
                except OSError:
                    pass

    def set_limit(self, limit_mb: int) -> None:
        with self._lock:
            self.limit = limit_mb * 1024 * 1024
            if self._size > self.limit:
                self._evict()

    @property
    def size_mb(self) -> float:
        return self._size / (1024 * 1024)

    def clear(self) -> None:
        with self._lock:
            for p in self.dir.glob("*"):
                try:
                    p.unlink()
                except OSError:
                    pass
            self._size = sum(f.stat().st_size for f in self.dir.glob("*") if f.is_file())
