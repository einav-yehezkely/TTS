"""Saved settings and per-document state (reading position, exclusions, order fixes)."""

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .paths import DOCS_DIR, SETTINGS_FILE


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _load(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


@dataclass
class Options:
    """What to leave out of narration. Detection of these is heuristic."""
    skip_footnotes: bool = True
    skip_references: bool = True
    skip_symbolic: bool = True     # symbol-heavy lines: equations, table cells, code prompts
    skip_figure_text: bool = True  # labels inside diagrams and charts
    skip_captions: bool = False
    skip_citations: bool = True    # numeric citation markers like [12] or [3, 4]
    read_headers: bool = False     # repeated page headers/footers and page numbers

    def key(self) -> tuple:
        return tuple(getattr(self, f.name) for f in fields(self))


@dataclass
class Settings:
    voice: str = "af_heart"
    speed: float = 1.0
    follow: bool = True
    zoom: float = 0.0  # 0 = fit width
    cache_limit_mb: int = 2048
    prefer_gpu: bool = True
    last_dir: str = ""
    recent: list[str] = field(default_factory=list)
    options: Options = field(default_factory=Options)

    @classmethod
    def load(cls) -> "Settings":
        d = _load(SETTINGS_FILE)
        s = cls()
        for f in fields(cls):
            if f.name in d and f.name != "options":
                setattr(s, f.name, d[f.name])
        for f in fields(Options):
            if f.name in d.get("options", {}):
                setattr(s.options, f.name, bool(d["options"][f.name]))
        return s

    def save(self) -> None:
        _atomic_write(SETTINGS_FILE, asdict(self))


@dataclass
class DocState:
    doc_id: str
    path: str = ""
    last_line: int | None = None
    excluded: set[int] = field(default_factory=set)   # lines the user removed from narration
    included: set[int] = field(default_factory=set)   # lines the user forced into narration
    page_mode: dict[int, str] = field(default_factory=dict)          # page -> auto/single/double
    block_order: dict[int, list[int]] = field(default_factory=dict)  # page -> block numbers in order

    @property
    def file(self) -> Path:
        return DOCS_DIR / f"{self.doc_id}.json"

    @classmethod
    def load(cls, doc_id: str) -> "DocState":
        d = _load(DOCS_DIR / f"{doc_id}.json")
        return cls(
            doc_id=doc_id,
            path=d.get("path", ""),
            last_line=d.get("last_line"),
            excluded=set(d.get("excluded", [])),
            included=set(d.get("included", [])),
            page_mode={int(k): v for k, v in d.get("page_mode", {}).items()},
            block_order={int(k): list(v) for k, v in d.get("block_order", {}).items()},
        )

    def save(self) -> None:
        _atomic_write(self.file, {
            "path": self.path, "last_line": self.last_line,
            "excluded": sorted(self.excluded), "included": sorted(self.included),
            "page_mode": {str(k): v for k, v in self.page_mode.items()},
            "block_order": {str(k): v for k, v in self.block_order.items()},
        })

    def edits_key(self) -> tuple:
        return (tuple(sorted(self.excluded)), tuple(sorted(self.included)),
                tuple(sorted(self.page_mode.items())), tuple(sorted((k, tuple(v)) for k, v in self.block_order.items())))
