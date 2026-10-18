"""Developer tool: print narration segments with their PDF line ids.

Usage: python tools/dump_segments.py file.pdf first_page last_page   (1-based)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kokoro_reader.docstate import DocState, Options  # noqa: E402
from kokoro_reader.layout import extract  # noqa: E402
from kokoro_reader.segments import build_segments  # noqa: E402

lay = extract(sys.argv[1])
first, last = int(sys.argv[2]), int(sys.argv[3])
segs = build_segments(lay, DocState("x"), Options(), range(first - 1, last))
for s in segs:
    print(f"#{s.index} pause={s.pause} lines={s.line_ids}")
    for lid, t in s.fragments:
        print(f"    {lid:5d} p{lay.lines[lid].page + 1} | {t}")
