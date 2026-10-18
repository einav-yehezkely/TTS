"""Checks with known answers: layout heuristics, text cleanup mapping, scanned PDFs,
GPU->CPU fallback during inference, cancellation latency and offline operation.

Usage: python tests/unit_checks.py
"""

import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pymupdf  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'} {name} {detail}", flush=True)


TMP = Path(tempfile.mkdtemp())

# ---- block the network for everything below: the app must work offline after setup ----------------------
_real_connect = socket.socket.connect
attempts = []


def _blocked(self, address):
    attempts.append(address)
    raise OSError("network blocked by test")


socket.socket.connect = _blocked
socket.socket.connect_ex = lambda self, a: (attempts.append(a), 1)[1]

from kokoro_reader.docstate import DocState, Options  # noqa: E402
from kokoro_reader.layout import extract  # noqa: E402
from kokoro_reader.segments import build_segments  # noqa: E402

# ---- synthetic book: header, page number, footnote, hyphenation ----------------------------------------------
BODY = [
    "The river ran quietly past the old mill, carrying leaves and the occa-",
    "sional branch toward the sea. Nobody in the village remembered when the",
    "wheel had last turned, but everyone agreed it had been a long time ago.",
    "Margaret walked along the bank every morning before the sun was fully up.",
]
book = pymupdf.open()
for n in range(6):
    p = book.new_page(width=420, height=600)
    p.insert_text((60, 40), "THE OLD MILL", fontsize=8)
    p.insert_text((205, 575), str(n + 1), fontsize=9)
    y = 90
    for t in BODY:
        p.insert_text((60, y), t.replace("Margaret", f"Margaret{n}"), fontsize=10)
        y += 14
    if n == 2:
        p.insert_text((60, 520), "1 This footnote explains where the mill was built.", fontsize=7)
book.save(TMP / "book.pdf")
lay = extract(TMP / "book.pdf")
roles = {(l.page, l.text): l.role for l in lay.lines}
check("running header detected", all(roles.get((i, "THE OLD MILL")) == "header" for i in range(6)))
check("page numbers detected", all(roles.get((i, str(i + 1))) == "pagenum" for i in range(6)))
fn = [l for l in lay.lines if "footnote explains" in l.text]
check("footnote detected", fn and fn[0].role == "footnote", fn and fn[0].role)
segs = build_segments(lay, DocState("t"), Options(), range(0, 1))
text = " ".join(s.text for s in segs)
check("hyphenated word joined", "occasional branch" in text and "occa-" not in text, text[:110])
check("header/page number not narrated", "OLD MILL" not in text and not text.rstrip().endswith("1"))
first_lines = [lid for s in segs for lid, _ in s.fragments]
body_ids = [l.id for l in lay.lines if l.page == 0 and l.role == "body"]
check("narration maps to the right PDF lines in order", sorted(set(first_lines)) == body_ids
      and first_lines == sorted(first_lines), f"{first_lines}")
seg_hy = next(s for s in segs if "occasional" in s.text)
frag = dict(seg_hy.fragments)
check("de-hyphenated word stays on the line where it starts",
      frag.get(body_ids[0], "").endswith("occasional") and frag.get(body_ids[1], "").startswith("branch"), frag)
opts = Options(skip_footnotes=False)
segs3 = build_segments(lay, DocState("t"), opts, range(2, 3))
check("footnote narrated when option off", any("footnote explains" in s.text for s in segs3))

# ---- synthetic two-column article ---------------------------------------------------------------------------
art = pymupdf.open()
p = art.new_page(width=612, height=792)
p.insert_text((150, 70), "A Study of Columns in Documents", fontsize=16)
left = [f"Left column sentence number {i} continues here." for i in range(1, 16)]
right = [f"Right column sentence number {i} continues here." for i in range(1, 16)]
for i, t in enumerate(left):
    p.insert_text((50, 130 + 14 * i), t, fontsize=10)
for i, t in enumerate(right):
    p.insert_text((320, 130 + 14 * i), t, fontsize=10)
p.insert_text((50, 400), "References", fontsize=12)
p.insert_text((50, 420), "[1] A. Author. A paper about things. Journal of Stuff, 2020.", fontsize=9)
art.save(TMP / "article.pdf")
lay2 = extract(TMP / "article.pdf")
order = [lay2.lines[i].text for i in lay2.pages[0].order]
li = [order.index(t) for t in left]
ri = [order.index(t) for t in right]
check("two columns detected", lay2.pages[0].columns == 2)
check("two-column reading order: title, all left, then all right",
      order.index("A Study of Columns in Documents") < li[0] and li == sorted(li) and ri == sorted(ri)
      and max(li) < min(ri))
refs = [l for l in lay2.lines if "Journal of Stuff" in l.text]
check("references section detected", refs and refs[0].role == "references")
segs2 = build_segments(lay2, DocState("t"), Options())
check("references skipped by default", not any("Journal of Stuff" in s.text for s in segs2))

# ---- scanned (image-only) PDF ---------------------------------------------------------------------------------
src = pymupdf.open(ROOT / "samples" / "resnet_two_column.pdf")
scan = pymupdf.open()
for i in range(3):
    pix = src[i].get_pixmap(dpi=100)
    pg = scan.new_page(width=src[i].rect.width, height=src[i].rect.height)
    pg.insert_image(pg.rect, pixmap=pix)
scan.save(TMP / "scanned.pdf")
lay3 = extract(TMP / "scanned.pdf")
check("scanned PDF detected as OCR required", lay3.scanned and lay3.text_pages == 0)
check("normal PDF not flagged as scanned", not lay.scanned and not lay2.scanned)

# ---- user edits keep the mapping ------------------------------------------------------------------------------
st = DocState("t")
st.excluded.add(body_ids[1])
segs_e = build_segments(lay, st, Options(), range(0, 1))
check("excluded line is not narrated", all(lid != body_ids[1] for s in segs_e for lid, _ in s.fragments)
      and "sional branch" not in " ".join(s.text for s in segs_e))
st2 = DocState("t2")
st2.page_mode[0] = "single"
order_single = [lay2.lines[i].text for i in __import__("kokoro_reader.segments", fromlist=["x"]).page_order(lay2, st2, 0)]
check("forcing one column changes the order", order_single.index(right[0]) < order_single.index(left[1]))

# ---- engine: runtime GPU failure falls back to CPU; cancellation is quick --------------------------------------
from kokoro_reader.assets import MODEL_FILE  # noqa: E402
from kokoro_reader.engine import Cancelled, CancelToken, KokoroEngine  # noqa: E402
from kokoro_reader.paths import VOICES_DIR  # noqa: E402

eng = KokoroEngine(MODEL_FILE, VOICES_DIR)
check("engine on GPU (offline)", eng.device == "cuda", eng.device_label())
real_sess = eng.sess


class Broken:
    def run(self, *a, **k):
        raise RuntimeError("CUDA error: an illegal memory access was encountered (simulated)")


eng.sess = Broken()
switched = []
eng.on_device_change = lambda e: switched.append(e.device)
s = eng.synthesize([(1, "The graphics card failed, but reading continues.")], "af_heart")
check("GPU failure during inference falls back to CPU", eng.device == "cpu" and switched == ["cpu"]
      and len(s.audio) > 24000, eng.fallback_reason)
long_text = [(i, "This is a fairly long sentence that keeps going so the processor has real work to do") for i in range(5)]
tok = CancelToken()
t0 = time.perf_counter()
threading.Timer(0.4, tok.cancel).start()
try:
    eng.synthesize(long_text, "af_heart", token=tok)
    cancelled_after = None
except Cancelled:
    cancelled_after = time.perf_counter() - t0
check("cancellation stops a running CPU synthesis quickly", cancelled_after is not None and cancelled_after < 1.2,
      f"stopped after {cancelled_after and round(cancelled_after, 2)}s (cancel requested at 0.4s)")
check("no network access attempted", not attempts, f"{attempts}")

failed = [n for n, ok in results if not ok]
print("RESULT:", "PASS" if not failed else f"FAIL {failed}")
sys.exit(1 if failed else 0)
