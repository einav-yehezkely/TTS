"""PDF text extraction with page coordinates, reading order and line roles.

Every visual text line keeps its page number and bounding box (PDF points,
origin top-left of the displayed page), which is what the viewer highlights.

Reading order: lines are split into columns at a detected vertical gutter;
full-width lines (titles, wide figures, page-wide paragraphs) cut the page
into horizontal bands, and each band is read left column then right column.

Roles are heuristics and are reported as such: repeated page headers/footers,
page numbers, footnotes, references, captions, headings, symbol-heavy lines
(likely equations or table cells) and rotated margin text.
"""

import gzip
import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pymupdf

EXTRACTOR_VERSION = 5

ROLES_NEVER_LAYOUT = {"margin", "header", "footer", "pagenum"}
ROLE_LABELS = {
    "body": "text", "heading": "heading", "caption": "caption", "footnote": "footnote",
    "references": "references", "symbolic": "symbols/equation/table", "header": "page header",
    "footer": "page footer", "pagenum": "page number", "margin": "margin text", "figure": "text inside image",
}

_PAGENUM_RE = re.compile(
    r"^\s*(page\s*)?[\(\[\-–—]?\s*(\d{1,4}|[ivxlcdm]{1,7})\s*[\)\]\-–—]?\s*((of|/)\s*\d{1,4})?\s*$", re.I)
_CAPTION_RE = re.compile(r"^\s*(fig\.?|figure|table|tab\.|algorithm|listing|chart|plate)\s*[0-9IVXA-Z]+[\.:\s]", re.I)
_FOOTNOTE_START_RE = re.compile(r"^\s*([0-9]{1,3}|[*†‡§¶∗⋆])\s*\S")
_REFS_TITLES = {"references", "bibliography", "works cited", "literature cited", "reference list",
                "references and notes", "cited literature", "literature"}
_MATH_CHARS = set("=+−×÷±∓∑∏∫∮√∞∂∇∈∉⊂⊆⊃⊇∪∩∀∃≤≥≈≠≡∝→←↔⇒⇔⟨⟩‖∥⊥∠°′″αβγδεζηθικλμνξπρστυφχψωΓΔΘΛΞΠΣΦΨΩ^_{}|<>")
_TERMINAL_PUNCT = re.compile(r"[.!?:;,]\W*$")
_SHORT_WORDS = {"a", "i", "an", "as", "at", "be", "by", "do", "go", "he", "if", "in", "is", "it", "me", "my",
                "no", "of", "on", "or", "so", "to", "up", "us", "we", "ok", "oh"}


@dataclass
class Line:
    id: int
    page: int
    bbox: tuple[float, float, float, float]
    text: str
    size: float
    bold: bool
    block: int
    role: str = "body"
    sup_start: bool = False

    @property
    def x0(self): return self.bbox[0]
    @property
    def y0(self): return self.bbox[1]
    @property
    def x1(self): return self.bbox[2]
    @property
    def y1(self): return self.bbox[3]
    @property
    def h(self): return max(0.1, self.bbox[3] - self.bbox[1])
    @property
    def w(self): return max(0.1, self.bbox[2] - self.bbox[0])
    @property
    def cy(self): return (self.bbox[1] + self.bbox[3]) / 2
    @property
    def cx(self): return (self.bbox[0] + self.bbox[2]) / 2


@dataclass
class PageInfo:
    index: int
    width: float
    height: float
    line_ids: list[int] = field(default_factory=list)  # every line on the page, extraction order
    order: list[int] = field(default_factory=list)     # automatic reading order
    columns: int = 1
    gutter: float | None = None
    chars: int = 0
    image_cover: float = 0.0
    label: str = ""

    @property
    def no_text(self) -> bool:
        return self.chars < 20


@dataclass
class DocLayout:
    path: str
    doc_id: str
    pages: list[PageInfo]
    lines: list[Line]
    body_size: float = 10.0
    garbled: bool = False

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def text_pages(self) -> int:
        return sum(1 for p in self.pages if not p.no_text)

    @property
    def scanned(self) -> bool:
        """No usable text layer: almost every page lacks text."""
        n = len(self.pages)
        return n > 0 and self.text_pages <= max(0, int(n * 0.1)) and self.text_pages < n

    def to_json(self) -> dict:
        return {"v": EXTRACTOR_VERSION, "path": self.path, "doc_id": self.doc_id, "body_size": self.body_size,
                "garbled": self.garbled, "pages": [asdict(p) for p in self.pages],
                "lines": [[l.id, l.page, list(l.bbox), l.text, l.size, l.bold, l.block, l.role, l.sup_start]
                          for l in self.lines]}

    @classmethod
    def from_json(cls, d: dict) -> "DocLayout":
        lines = [Line(i, p, tuple(b), t, s, bo, bl, r, su) for i, p, b, t, s, bo, bl, r, su in d["lines"]]
        pages = [PageInfo(**p) for p in d["pages"]]
        return cls(d["path"], d["doc_id"], pages, lines, d["body_size"], d["garbled"])


def document_id(path: str | Path) -> str:
    """Identify a PDF by content, so renamed or moved files keep their saved state."""
    p = Path(path)
    size = p.stat().st_size
    h = hashlib.sha1(str(size).encode())
    with open(p, "rb") as f:
        h.update(f.read(1 << 20))
        if size > 2 << 20:
            f.seek(-(1 << 20), 2)
            h.update(f.read())
    return h.hexdigest()[:20]


# ---- per-page raw extraction ----------------------------------------------------------------------

def _span_is_super(span: dict, line_size: float) -> bool:
    return bool(span.get("flags", 0) & 1) or span.get("size", line_size) < line_size * 0.72


def _raw_page_lines(page: pymupdf.Page, page_no: int, first_id: int) -> list[Line]:
    flags = pymupdf.TEXT_MEDIABOX_CLIP | pymupdf.TEXT_PRESERVE_WHITESPACE
    d = page.get_text("dict", flags=flags)
    rot = page.rotation_matrix if page.rotation else None
    pieces: list[Line] = []
    for b in d.get("blocks", []):
        if b.get("type") != 0:
            continue
        for ln in b.get("lines", []):
            spans = [s for s in ln.get("spans", []) if s.get("text")]
            if not spans:
                continue
            weights = Counter()
            for s in spans:
                weights[round(s["size"] * 2) / 2] += len(s["text"].strip())
            size = weights.most_common(1)[0][0] if weights else spans[0]["size"]
            text_parts = []
            sup_start = False
            for i, s in enumerate(spans):
                t = s["text"]
                if _span_is_super(s, size) and re.fullmatch(r"[\s\d,*†‡§¶∗⋆\-–a-z]{1,8}", t):
                    if i == 0 and t.strip():
                        sup_start = True
                    continue  # footnote markers and other superscripts are not spoken
                text_parts.append(t)
            text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", "".join(text_parts))
            text = re.sub(r"\s+", " ", text).strip()
            raw = "".join(s["text"] for s in spans).strip()
            if not raw:
                continue
            bold = sum(len(s["text"]) for s in spans if (s.get("flags", 0) & 16) or "bold" in s.get("font", "").lower()) \
                > 0.6 * sum(len(s["text"]) for s in spans)
            dx, dy = ln.get("dir", (1, 0))
            bbox = pymupdf.Rect(ln["bbox"])
            if rot is not None:
                bbox = (bbox * rot).normalize()
            role = "margin" if (abs(dy) > 0.2 or dx < 0.8) else "body"
            pieces.append(Line(0, page_no, (bbox.x0, bbox.y0, bbox.x1, bbox.y1), text or raw, size, bold,
                               b.get("number", 0), role, sup_start))
    # merge pieces of one visual line within a block (split spans, superscripts)
    merged: list[Line] = []
    by_block: dict[int, list[Line]] = defaultdict(list)
    for p in pieces:
        by_block[p.block].append(p)
    for blk in by_block.values():
        blk.sort(key=lambda l: (l.y0, l.x0))
        rows: list[list[Line]] = []
        for l in blk:
            for row in rows:
                r = row[0]
                ov = min(r.y1, l.y1) - max(r.y0, l.y0)
                side_by_side = all(min(q.x1, l.x1) - max(q.x0, l.x0) < 0.3 * l.size for q in row)
                if l.role == r.role and ov >= 0.5 * min(r.h, l.h) and abs(r.cy - l.cy) < 0.6 * max(r.h, l.h) \
                        and side_by_side:
                    row.append(l)
                    break
            else:
                rows.append([l])
        for row in rows:
            row.sort(key=lambda l: l.x0)
            cur = row[0]
            for nxt in row[1:]:
                gap = nxt.x0 - cur.x1
                if gap < 3 * max(cur.size, nxt.size):
                    joiner = "" if gap < 0.12 * cur.size or not nxt.text or not cur.text else " "
                    cur = Line(0, page_no, (min(cur.x0, nxt.x0), min(cur.y0, nxt.y0), max(cur.x1, nxt.x1),
                                            max(cur.y1, nxt.y1)), (cur.text + joiner + nxt.text).strip(),
                               max(cur.size, nxt.size) if cur.size < nxt.size * 0.8 else cur.size,
                               cur.bold and nxt.bold, cur.block, cur.role, cur.sup_start)
                else:
                    merged.append(cur)
                    cur = nxt
            merged.append(cur)
    merged.sort(key=lambda l: (l.block, l.y0, l.x0))
    for i, l in enumerate(merged):
        l.id = first_id + i
    return merged


def _figure_regions(page: pymupdf.Page) -> list[pymupdf.Rect]:
    """Areas covered by vector drawings (diagrams, charts), merged into clusters."""
    try:
        paths = page.get_drawings()
    except Exception:  # noqa: BLE001
        return []
    rects = [pymupdf.Rect(p["rect"]) for p in paths[:1500] if p["rect"].width > 2 and p["rect"].height > 2]
    clusters: list[list] = []  # [rect, count]
    for r in rects:
        grown = r + (-6, -6, 6, 6)
        hit = [c for c in clusters if c[0].intersects(grown)]
        for c in hit:
            r = r | c[0]
            clusters.remove(c)
        clusters.append([r, 1 + sum(c[1] for c in hit)])
    area = page.rect.width * page.rect.height
    return [c[0] for c in clusters if c[1] >= 2 and 0.005 * area < c[0].width * c[0].height < 0.6 * area]


# ---- reading order --------------------------------------------------------------------------------

def _rows(lines: list[Line]) -> list[Line]:
    """Top-to-bottom, and left-to-right for lines sharing a row."""
    out: list[Line] = []
    rows: list[list[Line]] = []
    for l in sorted(lines, key=lambda l: (l.y0, l.x0)):
        if rows:
            r = rows[-1]
            ref = max(r, key=lambda q: q.y1)
            side_by_side = all(min(q.x1, l.x1) - max(q.x0, l.x0) < 0.3 * l.size for q in r)
            if side_by_side and min(ref.y1, l.y1) - max(ref.y0, l.y0) >= 0.5 * min(ref.h, l.h):
                r.append(l)
                continue
        rows.append([l])
    for r in rows:
        out.extend(sorted(r, key=lambda l: l.x0))
    return out


def _find_gutter(body: list[Line], force: bool) -> float | None:
    x0 = min(l.x0 for l in body)
    x1 = max(l.x1 for l in body)
    W = x1 - x0
    if W < 50:
        return None
    mid = x0 + W / 2
    # centered titles and author lines span the gutter by design, they are not column text
    narrow = [l for l in body if l.w < 0.6 * W and not (l.x0 < mid < l.x1 and abs(l.cx - mid) < 0.1 * W)]
    if len(narrow) < 6 and not force:
        return None
    n = int(W) + 1
    cover = [0] * n
    for l in narrow:
        for i in range(max(0, int(l.x0 - x0)), min(n, int(l.x1 - x0) + 1)):
            cover[i] += 1
    lo, hi = int(0.3 * W), int(0.7 * W)
    limit = max(0, len(narrow) // 50)
    best, run_start = (0, None), None
    for i in range(lo, hi + 1):
        if cover[i] <= limit:
            if run_start is None:
                run_start = i
            length = i - run_start + 1
            if length > best[0]:
                best = (length, run_start)
        else:
            run_start = None
    gutter = None
    if best[1] is not None and best[0] >= 5:
        gutter = x0 + best[1] + best[0] / 2
    elif force:
        i = min(range(lo, hi + 1), key=lambda k: (cover[k], abs(k - W / 2)))
        gutter = x0 + i
    if gutter is None:
        return None
    left = [l for l in narrow if l.x1 <= gutter + 1]
    right = [l for l in narrow if l.x0 >= gutter - 1]
    if force:
        return gutter
    if len(left) < 3 or len(right) < 3 or min(len(left), len(right)) < 0.2 * len(narrow):
        return None
    ly = (min(l.y0 for l in left), max(l.y1 for l in left))
    ry = (min(l.y0 for l in right), max(l.y1 for l in right))
    overlap = min(ly[1], ry[1]) - max(ly[0], ry[0])
    if overlap < 0.3 * min(ly[1] - ly[0], ry[1] - ry[0]):
        return None
    return gutter


def order_page(lines: list[Line], mode: str = "auto") -> tuple[list[int], int, float | None]:
    """Reading order of one page's lines. mode: auto, single or double (two columns)."""
    top = [l for l in lines if l.role == "header"]
    bottom = [l for l in lines if l.role in ("footer", "pagenum")]
    margin = [l for l in lines if l.role == "margin"]
    body = [l for l in lines if l.role not in ROLES_NEVER_LAYOUT]
    if not body:
        return [l.id for l in _rows(top) + _rows(bottom) + margin], 1, None
    gutter = None if mode == "single" else _find_gutter(body, force=(mode == "double"))
    if gutter is None:
        ordered = _rows(body)
        cols = 1
    else:
        x0 = min(l.x0 for l in body)
        W = max(l.x1 for l in body) - x0
        spanning, left, right = [], [], []
        for l in body:
            crosses = l.x0 < gutter - 1 and l.x1 > gutter + 1
            if crosses and (l.w >= 0.55 * W or abs(l.cx - gutter) < 0.08 * W):
                spanning.append(l)
            elif l.cx < gutter:
                left.append(l)
            else:
                right.append(l)
        spanning = _rows(spanning)
        bands_l: dict[int, list[Line]] = defaultdict(list)
        bands_r: dict[int, list[Line]] = defaultdict(list)
        for col, bands in ((left, bands_l), (right, bands_r)):
            for l in col:
                k = sum(1 for s in spanning if s.cy < l.cy)
                bands[k].append(l)
        ordered = []
        for k in range(len(spanning) + 1):
            ordered += _rows(bands_l[k]) + _rows(bands_r[k])
            if k < len(spanning):
                ordered.append(spanning[k])
        cols = 2
    return [l.id for l in _rows(top) + ordered + _rows(bottom) + margin], cols, gutter


# ---- document-level roles -------------------------------------------------------------------------

def _norm_key(text: str) -> str:
    return re.sub(r"\d+", "#", re.sub(r"\s+", " ", text.lower())).strip()


def _mark_headers_footers(pages: list[PageInfo], lines: list[Line]) -> None:
    n = len(pages)
    zone_hits: dict[tuple[str, str], set[int]] = defaultdict(set)
    cands: list[tuple[Line, str]] = []
    for p in pages:
        for lid in p.line_ids:
            l = lines[lid]
            if l.role != "body":
                continue
            if l.y1 <= p.height * 0.1:
                zone = "top"
            elif l.y0 >= p.height * 0.9:
                zone = "bottom"
            else:
                continue
            cands.append((l, zone))
            zone_hits[(zone, _norm_key(l.text))].add(p.index)
    need = 2 if n <= 3 else max(3, int(0.15 * n))
    for l, zone in cands:
        if _PAGENUM_RE.match(l.text):
            l.role = "pagenum"
        elif n > 1 and len(zone_hits[(zone, _norm_key(l.text))]) >= need:
            l.role = "header" if zone == "top" else "footer"
    # running heads printed on the same row as a page number ("1.2 Running Python    3")
    for l, zone in cands:
        if l.role != "body":
            continue
        for other, z in cands:
            if other.page == l.page and z == zone and other.role == "pagenum" and \
                    min(other.y1, l.y1) - max(other.y0, l.y0) > 0.4 * min(other.h, l.h):
                l.role = "header" if zone == "top" else "footer"
                break


def _is_symbolic(text: str) -> bool:
    s = text.replace(" ", "")
    if not s:
        return False
    if len(s) <= 2:
        return not (s.isalpha() and (len(s) == 1 or s.lower() in _SHORT_WORDS)) and not s.isalpha()
    letters = sum(1 for c in s if c.isalpha() and c not in _MATH_CHARS)
    math = sum(1 for c in s if c in _MATH_CHARS)
    words = re.findall(r"[A-Za-z]{3,}", text)
    if math >= 3 and len(words) < 3:
        return True
    return letters / len(s) < 0.45 and len(words) < 3


def _assign_roles(layout: DocLayout) -> None:
    lines, pages, body_size = layout.lines, layout.pages, layout.body_size
    _mark_headers_footers(pages, lines)
    blocks: dict[tuple[int, int], list[Line]] = defaultdict(list)
    for l in lines:
        blocks[(l.page, l.block)].append(l)
    for blk in blocks.values():
        blk.sort(key=lambda l: (l.y0, l.x0))
        first = next((l for l in blk if l.role == "body"), None)
        if first is None:
            continue
        if _CAPTION_RE.match(first.text):
            for l in blk:
                if l.role == "body":
                    l.role = "caption"
            continue
        if len(blk) <= 3 and all(l.role == "body" for l in blk) and not blk[-1].text.rstrip().endswith(('.', ',', ';')):
            if first.size >= body_size + 0.9 or all(l.bold for l in blk):
                for l in blk:
                    l.role = "heading"
    for l in lines:
        if l.role == "body" and _is_symbolic(l.text):
            l.role = "symbolic"
    # tiny labels inside charts and diagrams, and text drawn over images
    images: dict[int, list[pymupdf.Rect]] = layout._image_rects if hasattr(layout, "_image_rects") else {}
    for blk in blocks.values():
        tiny = all(l.size < 0.72 * body_size and len(l.text) < 40 for l in blk)
        # short unpunctuated fragments set smaller than the body text: diagram labels such as "F(x)", "identity"
        label = all(l.size < body_size - 0.4 and len(l.text) < 30 and not _TERMINAL_PUNCT.search(l.text)
                    and l.role in ("body", "symbolic", "heading") for l in blk)
        if len(blk) <= 2 and (tiny or label):
            for l in blk:
                if l.role in ("body", "symbolic", "heading"):
                    l.role = "figure"
    drawings: dict[int, list[pymupdf.Rect]] = getattr(layout, "_drawing_rects", {})
    for l in lines:
        if l.role not in ("body", "symbolic", "heading"):
            continue
        r = pymupdf.Rect(l.bbox)
        for im in images.get(l.page, ()):
            inter = r & im
            if not inter.is_empty and inter.width * inter.height > 0.6 * r.width * r.height:
                l.role = "figure"
                break
        else:
            # labels inside vector diagrams; paragraph text in a framed box stays readable
            if len(l.text) < 40 or l.size < body_size * 0.9:
                for fr in drawings.get(l.page, ()):
                    if fr.contains(pymupdf.Point(l.cx, l.cy)):
                        l.role = "figure"
                        break
    # footnotes: small text at the bottom, below all body-size text it sits under
    for p in pages:
        plines = [lines[i] for i in p.line_ids]
        main = [l for l in plines if l.role in ("body", "heading") and l.size >= body_size - 0.6]
        cand_blocks = sorted({l.block for l in plines if l.role in ("body", "symbolic") and l.size <= body_size * 0.9
                              and l.y0 > p.height * 0.5}, key=lambda b: min(x.y0 for x in blocks[(p.index, b)]))
        started = False
        for b in cand_blocks:
            blk = blocks[(p.index, b)]
            below_main = all(not (m.y0 > blk[0].y0 and min(m.x1, blk[0].x1) > max(m.x0, blk[0].x0)) for m in main)
            if not below_main:
                continue
            if started or _FOOTNOTE_START_RE.match(blk[0].text) or blk[0].sup_start:
                started = True
                for l in blk:
                    if l.role in ("body", "symbolic"):
                        l.role = "footnote"
    # references: from a "References" heading to the end or to an appendix
    in_refs = False
    ref_size = 0.0
    total = len(lines)
    for p in pages:
        for lid in p.order:
            l = lines[lid]
            if l.role in ROLES_NEVER_LAYOUT:
                continue
            title = re.sub(r"^[\dIVX]+[\.\s)]*\s*", "", l.text).strip().lower().rstrip(":")
            if not in_refs and title in _REFS_TITLES and len(l.text) < 40 and (lid > total * 0.2 or len(pages) <= 2):
                in_refs, ref_size = True, l.size
                l.role = "references"
                continue
            if in_refs:
                if l.size >= ref_size - 0.1 and (l.role == "heading" or l.bold) and \
                        re.match(r"^\s*([A-Z]\.?\s+)?(appendix|supplementary|supplemental)", l.text, re.I):
                    in_refs = False
                    continue
                if l.role in ("body", "symbolic", "heading", "caption"):
                    l.role = "references"


# ---- entry point ------------------------------------------------------------------------------------

def extract(path: str | Path, progress: Callable[[int, int], None] | None = None,
            cancelled: Callable[[], bool] | None = None) -> DocLayout:
    doc = pymupdf.open(str(path))
    if doc.needs_pass:
        raise ValueError("This PDF is password protected.")
    pages: list[PageInfo] = []
    lines: list[Line] = []
    image_rects: dict[int, list[pymupdf.Rect]] = defaultdict(list)
    drawing_rects: dict[int, list[pymupdf.Rect]] = {}
    labels_ok = True
    for i, page in enumerate(doc):
        if cancelled and cancelled():
            raise InterruptedError()
        rect = page.rect
        pl = _raw_page_lines(page, i, len(lines))
        chars = sum(len(l.text.replace(" ", "")) for l in pl)
        cover = 0.0
        try:
            area = rect.width * rect.height
            for info in page.get_image_info():
                r = pymupdf.Rect(info["bbox"]) & rect
                share = max(0.0, r.width * r.height) / area
                cover += share
                if 0.002 < share < 0.5:  # figure-sized, not a page background or OCR scan
                    image_rects[i].append(r)
        except Exception:  # noqa: BLE001
            pass
        drawing_rects[i] = _figure_regions(page) if pl else []
        label = ""
        if labels_ok:
            try:
                label = page.get_label() or ""
            except Exception:  # noqa: BLE001
                labels_ok = False
        pages.append(PageInfo(i, rect.width, rect.height, [l.id for l in pl], [], 1, None, chars, min(1.0, cover),
                              label))
        lines.extend(pl)
        if progress:
            progress(i + 1, doc.page_count)
    weights = Counter()
    for l in lines:
        if l.role == "body":
            weights[round(l.size * 2) / 2] += len(l.text)
    body_size = weights.most_common(1)[0][0] if weights else 10.0
    all_text = "".join(l.text for l in lines)
    garbled = bool(all_text) and (all_text.count("�") + all_text.count("(cid:") * 5) > 0.05 * len(all_text)
    layout = DocLayout(str(path), document_id(path), pages, lines, body_size, garbled)
    layout._image_rects = image_rects
    layout._drawing_rects = drawing_rects
    _mark_headers_footers(pages, lines)
    for p in pages:  # preliminary order for the references scan
        p.order, p.columns, p.gutter = order_page([lines[i] for i in p.line_ids])
    for l in lines:  # header marking is redone inside _assign_roles
        if l.role in ("header", "footer", "pagenum"):
            l.role = "body"
    _assign_roles(layout)
    for p in pages:
        p.order, p.columns, p.gutter = order_page([lines[i] for i in p.line_ids])
    return layout


def load_or_extract(path: str | Path, cache_dir: Path, progress=None, cancelled=None) -> DocLayout:
    doc_id = document_id(path)
    cache = cache_dir / f"{doc_id}.v{EXTRACTOR_VERSION}.json.gz"
    if cache.exists():
        try:
            with gzip.open(cache, "rt", encoding="utf-8") as f:
                layout = DocLayout.from_json(json.load(f))
            layout.path = str(path)
            return layout
        except Exception:  # noqa: BLE001 - re-extract on any cache problem
            pass
    layout = extract(path, progress, cancelled)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        with gzip.open(cache, "wt", encoding="utf-8") as f:
            json.dump(layout.to_json(), f)
    except OSError:
        pass
    return layout
