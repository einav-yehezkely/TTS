"""MP4 video export: the original PDF pages with the line being read highlighted, plus the narration.

The application prepares each sentence exactly as audio export does (from the
audio cache, or synthesized and cached) and streams its audio and line timing
to a worker process (videoworker.py) that renders the pages and encodes H.264
video and AAC audio. Only one sentence is in flight: the pipe blocks this side
while the encoder is busy, so memory stays flat however long the document is.

The highlight times are the same per-line sample marks that drive the live
highlight, so the video is in sync to the millisecond timestamps it carries.
"""

import json
import logging
import os
import statistics
import struct
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from importlib.util import find_spec
from math import sqrt
from pathlib import Path

import numpy as np

from .audiocache import AudioCache
from .engine import SAMPLE_RATE, Cancelled, CancelToken, KokoroEngine
from .export import segment_audio
from .layout import DocLayout
from .paths import LOG_DIR, ROOT
from .segments import Segment

log = logging.getLogger(__name__)

VIEWS = {
    "zoom_portrait": "Zoomed on the line being read – phone, upright",
    "zoom_landscape": "Zoomed on the line being read – phone, sideways",
    "page": "Full page",
}
QUALITIES = {1080: "1080p – sharpest text", 720: "720p – smaller file"}
ENCODER_LABELS = {"h264_nvenc": "NVIDIA GPU encoder (NVENC)", "libx264": "CPU encoder (x264)"}
_LEVELS = {1080: "4.0", 720: "3.1"}  # H.264 levels every current phone decodes
WORKER_LOG = LOG_DIR / "video_export.log"


class VideoExportError(Exception):
    def __init__(self, text: str, retry_software: bool = False):
        super().__init__(text)
        self.retry_software = retry_software


@dataclass
class VideoResult:
    duration: float      # seconds
    encoder: str         # FFmpeg codec name
    fallback_reason: str
    frames: int
    worker_peak_mb: float | None


def available() -> bool:
    return find_spec("av") is not None


def frame_size(view: str, quality: int, page_sizes: list[tuple[float, float]]) -> tuple[int, int]:
    """Video width and height. Full-page video takes the shape of the commonest page, within the H.264 level."""
    long_side, short_side = (1920, 1080) if quality == 1080 else (1280, 720)
    if view == "zoom_portrait":
        return short_side, long_side
    if view == "zoom_landscape":
        return long_side, short_side
    pw, ph = Counter((round(w), round(h)) for w, h in page_sizes).most_common(1)[0][0]
    aspect = pw / ph
    area = long_side * short_side  # the level's frame-size limit
    h = min(long_side, sqrt(area / aspect))
    w = h * aspect
    if w > long_side:
        w, h = long_side, long_side / aspect
    return int(w) // 16 * 16, int(h) // 16 * 16


def _column_spans(layout: DocLayout, line_ids: set[int]) -> dict[int, tuple[float, float]]:
    """For the zoomed view: the horizontal stretch of page (points) to fit to the frame width for each line.

    That is the line's text column (or the whole text width for lines that cross
    a two-column gutter), with a margin, and never narrower than the document's
    usual column, so the zoom stays steady from page to page.
    """
    by_page: dict[int, list] = defaultdict(list)
    for lid in line_ids:
        by_page[layout.lines[lid].page].append(layout.lines[lid])
    raw: dict[int, tuple[float, float]] = {}
    for page, lines in by_page.items():
        info = layout.pages[page]
        gutter = info.gutter if info.columns == 2 else None

        def side(line, gutter=gutter) -> str:
            if gutter is None:
                return "all"
            return "left" if line.x1 <= gutter + 2 else "right" if line.x0 >= gutter - 2 else "all"
        groups: dict[str, list] = defaultdict(list)
        for line in lines:
            groups[side(line)].append(line)
        groups["all"] = lines
        ext = {k: (min(l.x0 for l in v), max(l.x1 for l in v)) for k, v in groups.items()}
        if "left" in ext and "right" in ext:  # both columns at the same zoom
            w = max(ext["left"][1] - ext["left"][0], ext["right"][1] - ext["right"][0])
            for k in ("left", "right"):
                c = (ext[k][0] + ext[k][1]) / 2
                ext[k] = (c - w / 2, c + w / 2)
        for line in lines:
            raw[line.id] = ext[side(line)]
    usual = statistics.median(b - a for a, b in raw.values()) if raw else 0
    spans = {}
    for lid, (a, b) in raw.items():
        pw = layout.pages[layout.lines[lid].page].width
        w = max(b - a, usual, 0.35 * pw)
        w = min(w + 2 * max(6.0, 0.04 * w), pw)
        c = (a + b) / 2
        x0 = min(max(c - w / 2, 0.0), pw - w)
        spans[lid] = (x0, x0 + w)
    return spans


def build_job(layout: DocLayout, segments: list[Segment], out: str, view: str, quality: int,
              software: bool = False, timeline: str | None = None) -> dict:
    line_ids = {lid for s in segments for lid, _ in s.fragments}
    pages = sorted({layout.lines[lid].page for lid in line_ids})
    width, height = frame_size(view, quality, [(layout.pages[p].width, layout.pages[p].height) for p in pages])
    spans = _column_spans(layout, line_ids) if view != "page" else {}
    lines = {}
    for lid in line_ids:
        l = layout.lines[lid]
        cx0, cx1 = spans.get(lid, (0.0, layout.pages[l.page].width))
        lines[lid] = [l.page, *l.bbox, cx0, cx1]
    return {"pdf": layout.path, "out": out, "mode": "page" if view == "page" else "zoom",
            "width": width, "height": height, "level": _LEVELS[quality], "sample_rate": SAMPLE_RATE,
            "software": software, "timeline": timeline,
            "pages": {p: [layout.pages[p].width, layout.pages[p].height] for p in pages}, "lines": lines}


def _send(pipe, header: dict, payload: bytes = b"") -> None:
    data = json.dumps(header).encode("utf-8")
    pipe.write(struct.pack("<I", len(data)) + data)
    if payload:
        pipe.write(payload)
    pipe.flush()


def _fmt_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _run_worker(job: dict, engine: KokoroEngine, cache: AudioCache, segments: list[Segment], voice: str,
                speed: float, progress: Callable[[int, int, str], None] | None, token: CancelToken) -> dict:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    total = len(segments)
    with open(WORKER_LOG, "ab") as errlog:
        proc = subprocess.Popen([sys.executable, "-m", "kokoro_reader.videoworker"], cwd=str(ROOT),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errlog,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    status: dict = {"encoder": None, "reason": "", "done": None, "error": None, "retry": False}

    def read() -> None:
        for raw in proc.stdout:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if "encoder" in msg:
                status["encoder"], status["reason"] = msg["encoder"], msg["reason"]
                if msg["reason"]:
                    log.info("video export: %s", msg["reason"])
            elif "seg" in msg and progress:
                label = ENCODER_LABELS.get(status["encoder"], status["encoder"])
                progress(msg["seg"], total, f"Sentence {msg['seg']} of {total} · {_fmt_time(msg['t'])} of video "
                                            f"made · {label}")
            elif "done" in msg:
                status["done"] = msg["done"]
            elif "error" in msg:
                status["error"], status["retry"] = msg["error"], msg.get("retry_software", False)

    def watch() -> None:  # a cancel stops the worker at once, even in the middle of a page
        while proc.poll() is None:
            if token.wait(0.1):
                proc.kill()
                return

    reader = threading.Thread(target=read, name="video-worker-reader", daemon=True)
    watcher = threading.Thread(target=watch, name="video-worker-cancel", daemon=True)
    reader.start()
    watcher.start()
    try:
        _send(proc.stdin, {"job": job})
        for seg in segments:
            if token.cancelled:
                raise Cancelled()
            synth = segment_audio(engine, cache, seg, voice, speed, token)
            pcm = (np.clip(synth.audio, -1.0, 1.0) * 32767).astype("<i2")
            _send(proc.stdin, {"n": int(pcm.size), "marks": synth.marks}, pcm.tobytes())
        _send(proc.stdin, {"end": True})
        proc.stdin.close()
    except OSError:
        pass  # the worker stopped reading; its message (or exit code) says why
    except BaseException:
        proc.kill()
        raise
    finally:
        proc.wait()
        reader.join(timeout=5)
    if token.cancelled:
        raise Cancelled()
    if proc.returncode == 0 and status["done"]:
        return status["done"]
    raise VideoExportError(status["error"] or f"The video encoder stopped unexpectedly (code {proc.returncode}). "
                                              f"Details: {WORKER_LOG}", status["retry"])


def export_video(engine: KokoroEngine, cache: AudioCache, layout: DocLayout, segments: list[Segment],
                 out_path: str | Path, voice: str, speed: float, view: str = "zoom_portrait", quality: int = 1080,
                 progress: Callable[[int, int, str], None] | None = None, token: CancelToken | None = None,
                 software: bool = False, timeline: str | None = None) -> VideoResult:
    """Write the segments as an MP4 video. `timeline` (tests): a path for a JSON log of every frame."""
    out_path = Path(out_path)
    if out_path.suffix.lower() != ".mp4":
        raise ValueError("Choose a .mp4 file name")
    if view not in VIEWS or quality not in QUALITIES:
        raise ValueError(f"Unknown video settings: {view}, {quality}")
    if not available():
        raise VideoExportError("The video library (PyAV) is not installed. Close the application and double-click "
                               "the launcher: it installs what is missing.")
    if not segments:
        raise ValueError("There is no narrated text to put in the video.")
    token = token or CancelToken()
    tmp = out_path.with_name(out_path.name + ".part")
    first_reason = ""
    try:
        while True:
            job = build_job(layout, segments, str(tmp), view, quality, software, timeline)
            try:
                done = _run_worker(job, engine, cache, segments, voice, speed, progress, token)
                break
            except VideoExportError as e:
                if not e.retry_software or software:
                    raise
                # the GPU encoder failed part-way: start again on the CPU (the narration is cached by now)
                log.warning("video export: %s; retrying with the CPU encoder", e)
                first_reason, software = str(e), True
                if progress:
                    progress(0, len(segments), "The NVIDIA encoder failed; starting again with the CPU encoder…")
        os.replace(tmp, out_path)
    except BaseException:
        for _ in range(20):  # a killed worker can hold the file for a moment
            try:
                os.remove(tmp)
                break
            except FileNotFoundError:
                break
            except OSError:
                time.sleep(0.05)
        raise
    return VideoResult(done["duration"], done["encoder"], first_reason or done["reason"], done["frames"],
                       done.get("peak_mb"))
