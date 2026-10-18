"""Download missing assets, interrupting once midway to check that resuming works."""

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kokoro_reader import assets  # noqa: E402

missing = assets.missing_assets()
print("missing before:", len(missing), [a.name for a in missing][:5], "...")
cancel = threading.Event()
seen = []


def progress(done, total, name):
    seen.append(done)
    if len(seen) == 40 and not cancel.is_set():
        cancel.set()  # simulate the user pressing Cancel / a lost connection


try:
    assets.download_all(progress, cancel)
except assets.DownloadCancelled:
    print("cancelled midway at", seen[-1], "bytes; partial files:",
          [p.name for p in assets.VOICES_DIR.glob("*.part")])
cancel.clear()
assets.download_all(progress, cancel)
left = assets.missing_assets()
print("missing after:", len(left))
print("voices installed:", len(assets.installed_voices()))
sys.exit(1 if left else 0)
