"""Turn the ordered PDF lines into narration segments (sentences).

A segment is a list of fragments, each tied to the PDF line it came from, so
cleanup never loses the mapping: words moved by de-hyphenation, removed
citation markers and expanded abbreviations all stay attached to their line.
"""

import re
from dataclasses import dataclass

from .docstate import DocState, Options
from .layout import DocLayout, Line, order_page

MAX_CHARS = 280

_EXPAND = [(re.compile(p), r) for p, r in [
    (r"\be\.\s?g\.,?", "for example,"), (r"\bi\.\s?e\.,?", "that is,"), (r"\bet al\.", "et al"),
    (r"\bFigs\.", "Figures"), (r"\bFig\.", "Figure"), (r"\bEqs\.", "Equations"), (r"\bEq\.", "Equation"),
    (r"\bSecs?\.", "Section"), (r"\bSect\.", "Section"), (r"\bvs\.", "versus"), (r"\bcf\.", "compare"),
    (r"\bDr\.", "Doctor"), (r"\bMr\.", "Mister"), (r"\bMrs\.", "Missus"), (r"\bProf\.", "Professor"),
    (r"\bNo\.(?=\s*\d)", "number"), (r"\bpp\.(?=\s*\d)", "pages"), (r"\bp\.(?=\s*\d)", "page"),
    (r"\bapprox\.", "approximately"), (r"\bresp\.", "respectively"), (r"\bChap\.", "Chapter"),
    (r"\bCh\.(?=\s*\d)", "Chapter"), (r"\bVol\.", "Volume"), (r"\bRefs\.", "References"),
    (r"\bRef\.", "Reference"), (r"\bTab\.(?=\s*\d)", "Table"), (r"\bSt\.(?=\s+[A-Z])", "Saint"),
    (r"\bJr\.", "Junior"), (r"\bSr\.", "Senior"), (r"\bInc\.", "Incorporated"), (r"\bLtd\.", "Limited"),
]]
_SYMBOLS = [("—", ", "), ("―", ", "), ("×", " times "), ("≈", " approximately "), ("±", " plus or minus "),
            ("≤", " less than or equal to "), ("≥", " greater than or equal to "), ("→", " to "), ("&", " and "),
            ("…", "..."), ("­", ""), ("�", " ")]
_CITATION = re.compile(r"\s*\[\d+(?:\s*[,–\-]\s*\d+)*\]")
_URL = re.compile(r"https?://\S+|www\.\S+")
_BULLET = re.compile(r"^[•◦▪‣∙·●○■□➢►]\s*")
_RANGE_DASH = re.compile(r"(?<=\d)\s?–\s?(?=\d)")
_NO_SPLIT = {"al.", "etc.", "approx.", "ca.", "viz.", "Mt.", "Gen.", "Col.", "Lt.", "Sgt.", "Ave.", "Blvd."}
_SENT_END = re.compile(r"[.!?]+[\"”’')\]]*(?=\s|$)")
_CLAUSE = re.compile(r"[,;:]\s|\s[–-]\s|\s\(")
_TERMINAL = re.compile(r"[.!?:;][\"”’')\]]*$")


@dataclass
class Segment:
    index: int
    fragments: list[tuple[int, str]]  # (line id, text to speak)
    pause: float = 0.35               # silence after the segment, seconds at speed 1

    @property
    def line_ids(self) -> list[int]:
        out: list[int] = []
        for lid, _ in self.fragments:
            if not out or out[-1] != lid:
                out.append(lid)
        return out

    @property
    def first_line(self) -> int:
        return self.fragments[0][0]

    @property
    def text(self) -> str:
        return " ".join(t for _, t in self.fragments)


def clean_text(text: str, opts: Options) -> str:
    t = _BULLET.sub("", text.strip())
    t = _URL.sub("link", t)
    t = _RANGE_DASH.sub(" to ", t)
    for a, b in _SYMBOLS:
        t = t.replace(a, b)
    for pat, rep in _EXPAND:
        t = pat.sub(rep, t)
    return re.sub(r"\s+", " ", t).strip()


def line_included(line: Line, state: DocState, opts: Options) -> bool:
    if line.id in state.excluded:
        return False
    if line.id in state.included:
        return True
    role = line.role
    if role in ("body", "heading"):
        return True
    if role in ("header", "footer", "pagenum"):
        return opts.read_headers
    return {"footnote": not opts.skip_footnotes, "references": not opts.skip_references,
            "symbolic": not opts.skip_symbolic, "figure": not opts.skip_figure_text,
            "caption": not opts.skip_captions}.get(role, False)


def page_order(layout: DocLayout, state: DocState, page: int) -> list[int]:
    """Reading order of a page after the user's corrections."""
    p = layout.pages[page]
    mode = state.page_mode.get(page, "auto")
    order = p.order if mode == "auto" else order_page([layout.lines[i] for i in p.line_ids], mode)[0]
    custom = state.block_order.get(page)
    if custom:
        rank = {b: k for k, b in enumerate(custom)}
        first_seen: dict[int, int] = {}
        pos = {lid: k for k, lid in enumerate(order)}
        for k, lid in enumerate(order):
            first_seen.setdefault(layout.lines[lid].block, k)

        def key(lid: int) -> tuple:
            b = layout.lines[lid].block
            return rank.get(b, len(rank) + first_seen[b] / 1e6), pos[lid]
        order = sorted(order, key=key)
    return order


def page_blocks(layout: DocLayout, state: DocState, page: int) -> list[int]:
    """Block numbers of a page in reading order."""
    seen: list[int] = []
    for lid in page_order(layout, state, page):
        b = layout.lines[lid].block
        if b not in seen:
            seen.append(b)
    return seen


def _forced_break(a: Line, b: Line, a_text: str, block_sizes: dict) -> bool:
    if a.role != b.role and "heading" in (a.role, b.role):
        return True
    if a.role != b.role and {a.role, b.role} & {"caption", "footnote", "references", "figure", "symbolic"}:
        return True
    same_block = a.page == b.page and a.block == b.block
    if a.role == "heading" and not same_block:
        return True
    if same_block:
        return False
    if _TERMINAL.search(a_text):
        return True
    starts_upper = bool(b.text[:1].isupper())
    return starts_upper and block_sizes.get((a.page, a.block), 0) <= 2


def _split_long(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Split [start, end) of text into pieces of at most MAX_CHARS, preferring clause boundaries."""
    out = []
    while end - start > MAX_CHARS:
        window = text[start:start + MAX_CHARS]
        cut = None
        for m in _CLAUSE.finditer(window):
            if m.start() > 80:
                cut = m.start() + 1
        if cut is None:
            sp = window.rfind(" ", 60)
            cut = sp if sp > 0 else MAX_CHARS
        out.append((start, start + cut))
        start += cut
        while start < end and text[start] == " ":
            start += 1
    if start < end:
        out.append((start, end))
    return out


def _sentences(stream: str) -> list[tuple[int, int, bool]]:
    """(start, end, is_sentence_end) spans of the stream."""
    spans, start = [], 0
    for m in _SENT_END.finditer(stream):
        end = m.end()
        word_start = stream.rfind(" ", 0, m.start()) + 1
        word = stream[word_start:m.start() + 1]
        nxt = stream[end:].lstrip()[:1]
        if word in _NO_SPLIT or re.fullmatch(r"[A-Z]\.", word) or re.fullmatch(r"(?:[A-Za-z]\.){2,}", word):
            continue
        if not stream[start:word_start].strip() and re.fullmatch(r"(\d+|[IVXLC]+|[A-Z])(\.\d+)*\.", word):
            continue  # section number such as "1." or "2.3." opening a heading
        if nxt and not (nxt.isupper() or nxt.isdigit() or nxt in "\"“‘'([" ):
            continue
        if stream[start:end].strip():
            spans.append((start, end, True))
        start = end
    if stream[start:].strip():
        spans.append((start, len(stream), False))
    out = []
    for s, e, final in spans:
        while s < e and stream[s] == " ":
            s += 1
        pieces = _split_long(stream, s, e)
        for k, (ps, pe) in enumerate(pieces):
            out.append((ps, pe, final and k == len(pieces) - 1))
    return out


def build_segments(layout: DocLayout, state: DocState, opts: Options,
                   pages: range | None = None) -> list[Segment]:
    pages = pages if pages is not None else range(layout.page_count)
    block_sizes: dict[tuple[int, int], int] = {}
    for l in layout.lines:
        block_sizes[(l.page, l.block)] = block_sizes.get((l.page, l.block), 0) + 1
    seq: list[tuple[Line, str]] = []
    for p in pages:
        for lid in page_order(layout, state, p):
            line = layout.lines[lid]
            if line_included(line, state, opts):
                t = clean_text(line.text, opts)
                if t:
                    seq.append((line, t))
    # de-hyphenate: move the continuation of a split word onto the line where the word starts
    texts = [t for _, t in seq]
    for i in range(len(seq) - 1):
        a, b = texts[i], texts[i + 1]
        if re.search(r"[A-Za-z]-$", a) and re.match(r"[a-z]", b):
            head, _, rest = b.partition(" ")
            trail = re.match(r"([a-z]+)(.*)", head)
            texts[i] = a[:-1] + trail.group(1) + trail.group(2)
            texts[i + 1] = rest.strip()
    # group into runs separated by forced breaks, then split runs into sentences
    segments: list[Segment] = []
    run: list[tuple[int, str]] = []

    def flush(paragraph_end: bool) -> None:
        if not run:
            return
        stream, spans = "", []
        for lid, t in run:
            if not t:
                continue
            if stream:
                stream += " "
            spans.append((lid, len(stream), len(stream) + len(t)))
            stream += t
        if opts.skip_citations:  # on the joined text, so markers broken over lines go too
            stream, spans = _remove(stream, spans, [m.span() for m in _CITATION.finditer(stream)])
        for s, e, final in _sentences(stream):
            frags = []
            for lid, ls, le in spans:
                if le <= s or ls >= e:
                    continue
                piece = stream[max(s, ls):min(e, le)].strip()
                if piece and frags and not any(c.isalnum() for c in piece):
                    frags[-1] = (frags[-1][0], frags[-1][1] + piece)  # stray punctuation left by cleanup
                elif piece:
                    frags.append((lid, piece))
            if not frags:
                continue
            is_last = e >= len(stream.rstrip())
            if is_last and frags[-1][1][-1:].isalnum():
                frags[-1] = (frags[-1][0], frags[-1][1] + ".")  # headings and list items end with a fall
            pause = 0.5 if (is_last and paragraph_end) else (0.35 if final or is_last else 0.12)
            segments.append(Segment(len(segments), frags, pause))
        run.clear()

    # captions and footnotes met in the middle of a sentence are read once the sentence ends
    items = list(zip([l for l, _ in seq], texts))
    ordered: list[tuple[Line, str]] = []
    deferred: list[tuple[Line, str]] = []
    unfinished = False
    for k, (line, t) in enumerate(items):
        if line.role in ("caption", "footnote") and unfinished:
            deferred.append((line, t))
            continue
        ordered.append((line, t))
        if line.role in ("body", "heading") and t:
            unfinished = not _TERMINAL.search(t)
            if not unfinished and deferred:
                ordered += deferred
                deferred = []
    ordered += deferred

    prev: Line | None = None
    prev_text = ""
    for line, t in ordered:
        if prev is not None and _forced_break(prev, line, prev_text, block_sizes):
            flush(True)
        if t:
            run.append((line.id, t))
            prev_text = t
        prev = line
    flush(True)
    return segments


def _remove(stream: str, spans: list[tuple[int, int, int]], ranges: list[tuple[int, int]]):
    """Delete character ranges from the stream, keeping every line's span aligned."""
    if not ranges:
        return stream, spans

    def shift(x: int) -> int:
        return x - sum(min(e, x) - s for s, e in ranges if s < x)
    out = "".join(stream[a:b] for a, b in zip([0] + [e for _, e in ranges], [s for s, _ in ranges] + [len(stream)]))
    return out, [(lid, shift(s), shift(e)) for lid, s, e in spans]


def segment_for_line(segments: list[Segment], line_id: int) -> int | None:
    for s in segments:
        if any(lid == line_id for lid, _ in s.fragments):
            return s.index
    return None
