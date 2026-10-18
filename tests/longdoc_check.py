"""Long-document streaming: start deep in a book, play for a while, check bounded prefetch,
memory, underruns and that Stop really stops generation.

Usage: python tests/longdoc_check.py [pdf] [seconds]
Set KOKORO_FORCE_CPU=1 to run the same check on the CPU.
"""

import ctypes
import ctypes.wintypes
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from PySide6.QtCore import QCoreApplication, QTimer  # noqa: E402

from kokoro_reader.assets import MODEL_FILE  # noqa: E402
from kokoro_reader.audiocache import AudioCache  # noqa: E402
from kokoro_reader.docstate import DocState, Options  # noqa: E402
from kokoro_reader.engine import KokoroEngine  # noqa: E402
from kokoro_reader.layout import extract  # noqa: E402
from kokoro_reader.narrator import PREFETCH, Narrator  # noqa: E402
from kokoro_reader.paths import VOICES_DIR  # noqa: E402
from kokoro_reader.segments import build_segments, page_order  # noqa: E402


class PMC(ctypes.Structure):
    _fields_ = [("cb", ctypes.wintypes.DWORD), ("PageFaultCount", ctypes.wintypes.DWORD)] + \
               [(n, ctypes.c_size_t) for n in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                               "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                                               "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]


_k32 = ctypes.WinDLL("kernel32")
_k32.GetCurrentProcess.restype = ctypes.wintypes.HANDLE
_k32.K32GetProcessMemoryInfo.argtypes = [ctypes.wintypes.HANDLE, ctypes.POINTER(PMC), ctypes.wintypes.DWORD]


def rss_mb() -> float:
    c = PMC()
    c.cb = ctypes.sizeof(c)
    if not _k32.K32GetProcessMemoryInfo(_k32.GetCurrentProcess(), ctypes.byref(c), c.cb):
        raise OSError("GetProcessMemoryInfo failed")
    return c.WorkingSetSize / 1e6


pdf = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "samples/think_python_book.pdf")
seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 60
app = QCoreApplication([])
t = time.perf_counter()
lay = extract(pdf)
st = DocState(lay.doc_id)
segs = build_segments(lay, st, Options())
order = [lid for p in range(lay.page_count) for lid in page_order(lay, st, p)]
print(f"{lay.page_count} pages -> {len(segs)} sentences, prepared in {time.perf_counter() - t:.1f}s")
eng = KokoroEngine(MODEL_FILE, VOICES_DIR)
print("device:", eng.device_label(), eng.fallback_reason)
nar = Narrator(eng, AudioCache(Path(tempfile.mkdtemp()), 500))
nar.set_segments(segs, order)
start = next(s.index for s in segs if lay.lines[s.first_line].page >= int(lay.page_count * 0.8))
mem0 = rss_mb()
samples = []
first_audio = []
t0 = time.perf_counter()
nar.stateChanged.connect(lambda s: s == "playing" and not first_audio and first_audio.append(time.perf_counter() - t0))
nar.play(start)


def sample():
    ahead = nar._next - nar.current_seg
    samples.append((ahead, rss_mb(), nar.state))


timer = QTimer(interval=500, timeout=sample)
timer.start()
stop_info = {}


def do_stop():
    stop_info["generated_before"] = len(nar.synth_times)
    t = time.perf_counter()
    nar.stop()
    stop_info["stop_call"] = time.perf_counter() - t
    stop_info["seg_at_stop"] = nar.current_seg


QTimer.singleShot(int(seconds * 1000), do_stop)
QTimer.singleShot(int(seconds * 1000) + 3000, app.quit)
app.exec()
generated_after = len(nar.synth_times) - stop_info["generated_before"]
nar.shutdown()

played = stop_info["seg_at_stop"] - start
max_ahead = max(a for a, _, _ in samples)
max_mem = max(m for _, m, _ in samples)
buffering = sum(1 for *_, s in samples[4:] if s == "buffering")
times = nar.synth_times
rtf = sum(a for _, a, _ in times) / max(1e-6, sum(b for _, _, b in times))
print(f"started at sentence {start} (page {lay.lines[segs[start].first_line].page + 1}); "
      f"first audio after {first_audio[0] if first_audio else None:.2f}s")
print(f"played {played} sentences in {seconds:.0f}s; synthesis real-time factor {rtf:.3f}")
print(f"max sentences generated ahead: {max_ahead} (limit {PREFETCH + 1}); memory {mem0:.0f} -> max {max_mem:.0f} MB")
print(f"underruns: {nar.player.underruns}; 'buffering' samples after start: {buffering}")
step = max(1, len(samples) // 8)
print("memory over time (MB):", [round(m) for _, m, _ in samples[::step]])
print(f"stop() took {stop_info['stop_call'] * 1000:.0f} ms; sentences generated after stop: {generated_after}")
ok = (first_audio and first_audio[0] < 5 and max_ahead <= PREFETCH + 1 and generated_after <= 1
      and max_mem - mem0 < 400 and played > 3)
print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
