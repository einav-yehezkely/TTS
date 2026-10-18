"""End-to-end check of narration without the GUI: real synthesis, real audio output.

Plays the start of a PDF, then exercises pause, resume, next/previous sentence,
speed change and stop, logging every highlighted-line change with its time.
Checks that line changes happen at the times the model's timing predicts.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PySide6.QtCore import QCoreApplication, QTimer  # noqa: E402

from kokoro_reader.assets import MODEL_FILE  # noqa: E402
from kokoro_reader.audiocache import AudioCache  # noqa: E402
from kokoro_reader.docstate import DocState, Options  # noqa: E402
from kokoro_reader.engine import KokoroEngine  # noqa: E402
from kokoro_reader.layout import extract  # noqa: E402
from kokoro_reader.narrator import Narrator  # noqa: E402
from kokoro_reader.paths import VOICES_DIR  # noqa: E402
from kokoro_reader.segments import build_segments, page_order  # noqa: E402

pdf = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent / "samples/resnet_two_column.pdf")
app = QCoreApplication([])
lay = extract(pdf)
state = DocState(lay.doc_id)
segs = build_segments(lay, state, Options())
order = [lid for p in range(lay.page_count) for lid in page_order(lay, state, p)]
engine = KokoroEngine(MODEL_FILE, VOICES_DIR)
print("device:", engine.device_label())
import tempfile  # noqa: E402
cache = AudioCache(Path(tempfile.mkdtemp()), 500)
nar = Narrator(engine, cache)
nar.set_segments(segs, order)
t0 = time.perf_counter()
events = []


def stamp(kind, value):
    events.append((round(time.perf_counter() - t0, 3), kind, value))


nar.lineChanged.connect(lambda l: stamp("line", l))
nar.segmentChanged.connect(lambda s: stamp("seg", s))
nar.stateChanged.connect(lambda s: stamp("state", s))
nar.error.connect(lambda e: stamp("error", e))

script = [
    (0.0, "play", lambda: nar.play(3)),
    (6.0, "pause", nar.pause),
    (8.0, "resume", nar.resume),
    (11.0, "next", nar.next_sentence),
    (14.0, "prev", nar.prev_sentence),
    (17.0, "speed 1.5", lambda: nar.set_speed(1.5)),
    (21.0, "stop", nar.stop),
    (22.0, "quit", app.quit),
]
for at, name, fn in script:
    QTimer.singleShot(int(at * 1000), lambda n=name, f=fn: (stamp("cmd", n), f()))
app.exec()
nar.shutdown()

for t, kind, value in events:
    if kind == "line":
        l = lay.lines[value]
        print(f"{t:7.3f} line {value:4d} p{l.page + 1} | {l.text[:70]}")
    else:
        print(f"{t:7.3f} {kind} {value}")
print("synthesis (segment, seconds to make, audio seconds):",
      [(i, round(a, 2), round(b, 2)) for i, a, b in nar.synth_times])
print("underruns:", nar.player.underruns)

# ---- checks ----------------------------------------------------------------------------------------
failures = []
segs_heard = [(t, v) for t, k, v in events if k == "seg"]


def seg_after(t_cmd, window=1.5):
    later = [v for t, v in segs_heard if t_cmd <= t < t_cmd + window]
    return later


def cmd_time(name):
    return next(t for t, k, v in events if k == "cmd" and v == name)


def seg_before(t_cmd):
    before = [v for t, v in segs_heard if t < t_cmd]
    return before[-1] if before else None


for name, delta in (("next", +1), ("prev", -1)):
    t = cmd_time(name)
    expect = seg_before(t) + delta
    got = seg_after(t)
    ok = bool(got) and all(g in (expect, expect + 1) for g in got) and got[0] == expect
    print(f"CHECK {name}: expected segment {expect}, heard {got} -> {'OK' if ok else 'FAIL'}")
    if not ok:
        failures.append(name)
# while paused, no line changes may happen
tp, tr = cmd_time("pause"), cmd_time("resume")
moved = [e for e in events if e[1] == "line" and tp + 0.05 < e[0] < tr]
print(f"CHECK pause: line changes while paused = {len(moved)} -> {'OK' if not moved else 'FAIL'}")
if moved:
    failures.append("pause")
# line change times inside one segment must match the synthesized timing (heard via the player clock)
t_play = cmd_time("play")
first_playing = next(t for t, k, v in events if k == "state" and v == "playing" and t >= t_play)
seg3 = nar.cache.get(nar.cache.key(nar.voice, 1.0, segs[3].pause, segs[3].fragments))
seg4 = nar.cache.get(nar.cache.key(nar.voice, 1.0, segs[4].pause, segs[4].fragments))
if seg3 and seg4:
    predicted = first_playing + len(seg3.audio) / 24000 + seg4.marks[1][1] / 24000
    actual = next(t for t, k, v in events if k == "line" and v == seg4.marks[1][0] and t > first_playing)
    err = actual - predicted
    print(f"CHECK timing: line {seg4.marks[1][0]} predicted at {predicted:.3f}s, highlighted at {actual:.3f}s "
          f"(difference {err * 1000:+.0f} ms) -> {'OK' if abs(err) < 0.25 else 'FAIL'}")
    if abs(err) >= 0.25:
        failures.append("timing")
print("RESULT:", "PASS" if not failures else f"FAIL {failures}")
sys.exit(1 if failures else 0)
