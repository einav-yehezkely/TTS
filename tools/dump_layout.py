"""Developer tool: print the extracted reading order and roles of PDF pages.

Usage: python tools/dump_layout.py file.pdf [first_page [last_page]]   (1-based pages)
"""

import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kokoro_reader.layout import extract  # noqa: E402

path = sys.argv[1]
t = time.time()
lay = extract(path)
print(f"{lay.page_count} pages, {len(lay.lines)} lines, body size {lay.body_size}, "
      f"scanned={lay.scanned}, extraction {time.time() - t:.1f}s")
print("roles:", dict(Counter(l.role for l in lay.lines)))
first = int(sys.argv[2]) if len(sys.argv) > 2 else 1
last = int(sys.argv[3]) if len(sys.argv) > 3 else first
for pno in range(first - 1, last):
    p = lay.pages[pno]
    print(f"\n=== page {pno + 1}: columns={p.columns} gutter={p.gutter and round(p.gutter)} chars={p.chars}")
    for k, lid in enumerate(p.order):
        l = lay.lines[lid]
        print(f"{k:3d} [{l.role:10s}] b{l.block:<3d} x{l.x0:5.0f} y{l.y0:5.0f} s{l.size:4.1f} | {l.text[:90]}")
