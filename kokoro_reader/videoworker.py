"""Video export worker: renders the PDF pages with the line highlight and encodes MP4 (H.264 + AAC).

Runs as its own process (python -m kokoro_reader.videoworker), so page
rendering and encoding never hold the GUI's interpreter lock, a codec crash
cannot take the application down, and cancelling is a process kill.

Protocol. stdin carries length-prefixed messages (4-byte little-endian length,
then a JSON header; an audio message is followed by its raw PCM):
  {"job": {...}}                                   first message
  {"n": samples, "marks": [[line, sample], ...]}   one sentence: n int16 samples follow
  {"end": true}
stdout answers with one JSON object per line: {"encoder": ...}, {"seg": i, "t": seconds},
{"done": {...}} or {"error": text, "retry_software": bool}.

Frames carry millisecond timestamps and are only emitted when the picture
changes (a new line, a camera glide), plus a repeat at least once a second for
seeking and player compatibility. The highlight therefore switches at the audio
sample the model's timing gives, without encoding hours of identical frames.
"""

import io
import json
import math
import os
import struct
import sys
import traceback
from collections import OrderedDict
from dataclasses import dataclass, replace
from fractions import Fraction

import av
import av.logging
import numpy as np
import pymupdf
from av.video.frame import PictureType
from av.video.reformatter import Colorspace

TB = Fraction(1, 1000)
KEEPALIVE = 1.0                         # longest a frame is shown before it is repeated, seconds
GOP = 10                                # frames between keyframes (also forced at every page change)
GLIDE = 0.32                            # zoomed view: camera scroll duration, seconds
GLIDE_FPS = 30
BG = (38, 40, 46)                       # outside the page
TINT = np.array([255, 222, 110], np.float32) / 255  # multiplied in: the paper turns yellow, ink stays dark
EDGE = (230, 120, 0)
AUDIO_RATE = 48000
AUDIO_BITRATE = 96000

# No B-frames: with frames seconds apart, reordering would need composition offsets of seconds, which some
# players apply wrongly (picture out of step with the sound).
ENCODER_OPTIONS = {
    "h264_nvenc": {"preset": "p5", "tune": "hq", "rc": "vbr", "cq": "23", "profile": "high", "bf": "0",
                   "forced-idr": "1"},
    "libx264": {"preset": "medium", "crf": "22", "tune": "stillimage", "profile": "high", "bf": "0"},
}


@dataclass(frozen=True)
class View:
    """What the frame shows: the page area whose top-left corner is (x0, y0), in PDF points, at `scale` px/pt."""
    page: int
    scale: float
    x0: float
    y0: float

    def same_place(self, other: "View | None") -> bool:
        return (other is not None and other.page == self.page and math.isclose(other.scale, self.scale)
                and math.isclose(other.x0, self.x0))


class Renderer:
    def __init__(self, job: dict):
        self.doc = pymupdf.open(job["pdf"])
        self.W, self.H = job["width"], job["height"]
        self.zoom = job["mode"] != "page"
        self.pages = {int(k): v for k, v in job["pages"].items()}   # page -> [width, height]
        self.lines = {int(k): v for k, v in job["lines"].items()}   # line -> [page, x0, y0, x1, y1, cx0, cx1]
        self._images: OrderedDict = OrderedDict()
        self.goal: View | None = None
        self._glide: tuple[float, float, float, float] | None = None  # start, end, y from, y to

    # ---- camera ----------------------------------------------------------------------------------------
    def _target(self, lid: int) -> View:
        page, x0, y0, x1, y1, cx0, cx1 = self.lines[lid]
        pw, ph = self.pages[page]
        if not self.zoom:
            s = min(self.W / pw, self.H / ph)
            return View(page, s, -(self.W / s - pw) / 2, -(self.H / s - ph) / 2)
        s = self.W / (cx1 - cx0)
        cam_h = self.H / s
        goal = View(page, s, cx0, 0.0)
        if goal.same_place(self.goal):
            g = self.goal
            if y0 >= g.y0 + 0.1 * cam_h and y1 <= g.y0 + 0.85 * cam_h:
                return g  # still comfortably in view: keep the camera still
        if cam_h >= ph:
            y = (ph - cam_h) / 2
        else:
            y = min(max(y0 - 0.25 * cam_h, 0.0), ph - cam_h)
        return replace(goal, y0=y)

    def set_line(self, lid: int, t: float) -> bool:
        """Point the camera at a line. Returns True when the picture cuts to another page."""
        new = self._target(lid)
        old = self.goal
        if new.same_place(old):
            if not math.isclose(new.y0, old.y0):
                self._glide = (t, t + GLIDE, self.view_at(t).y0, new.y0)
            self.goal = new
            return False
        self.goal, self._glide = new, None
        return old is None or new.page != old.page

    def view_at(self, t: float) -> View:
        if self._glide:
            t0, t1, a, b = self._glide
            if t < t1:
                u = 1 - (1 - (t - t0) / (t1 - t0)) ** 3  # ease out
                return replace(self.goal, y0=a + (b - a) * u)
            self._glide = None
        return self.goal

    def next_glide_frame(self, after: float) -> float | None:
        if not self._glide:
            return None
        return min(after + 1 / GLIDE_FPS, self._glide[1])

    # ---- pixels ----------------------------------------------------------------------------------------
    def _page_image(self, page: int, scale: float, x0: float, x1: float) -> tuple[np.ndarray, int, int]:
        """Page strip x0..x1 (points, full height) at the scale, with its top-left pixel in page pixel space."""
        key = (page, round(scale, 5), round(x0, 2), round(x1, 2))
        hit = self._images.get(key)
        if hit is None:
            ph = self.pages[page][1]
            pix = self.doc[page].get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=pymupdf.Rect(x0, 0, x1, ph),
                                            alpha=False)
            arr = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.stride)[:, :pix.width * 3]
            hit = (arr.reshape(pix.height, pix.width, 3), pix.x, pix.y)
            self._images[key] = hit
            while len(self._images) > 3:  # a page or two of strips: memory stays flat on long documents
                self._images.popitem(last=False)
        else:
            self._images.move_to_end(key)
        return hit

    def render(self, view: View, lid: int | None) -> tuple[np.ndarray, list[int] | None]:
        W, H, s = self.W, self.H, view.scale
        frame = np.empty((H, W, 3), np.uint8)
        frame[:] = BG
        pw, ph = self.pages[view.page]
        x0, x1 = max(0.0, view.x0), min(pw, view.x0 + W / s)
        if x1 > x0:
            img, ix, iy = self._page_image(view.page, s, x0, x1)
            fx, fy = ix - round(view.x0 * s), iy - round(view.y0 * s)
            sx, sy = max(0, -fx), max(0, -fy)
            dx, dy = max(0, fx), max(0, fy)
            w = min(img.shape[1] - sx, W - dx)
            h = min(img.shape[0] - sy, H - dy)
            if w > 0 and h > 0:
                frame[dy:dy + h, dx:dx + w] = img[sy:sy + h, sx:sx + w]
        rect = None
        if lid is not None and self.lines[lid][0] == view.page:
            _, lx0, ly0, lx1, ly1, _, _ = self.lines[lid]
            # the viewer's padding around the line box, in points
            X0 = max(0, math.floor((lx0 - 2 - view.x0) * s))
            Y0 = max(0, math.floor((ly0 - 1.5 - view.y0) * s))
            X1 = min(W, math.ceil((lx1 + 2 - view.x0) * s))
            Y1 = min(H, math.ceil((ly1 + 1.5 - view.y0) * s))
            if X1 > X0 and Y1 > Y0:
                region = frame[Y0:Y1, X0:X1]
                region[:] = (region * TINT).astype(np.uint8)
                e = max(2, round(0.8 * s))
                for sl in (np.s_[Y0:Y0 + e, X0:X1], np.s_[Y1 - e:Y1, X0:X1],
                           np.s_[Y0:Y1, X0:X0 + e], np.s_[Y0:Y1, X1 - e:X1]):
                    frame[sl] = EDGE
                rect = [X0, Y0, X1, Y1]
        return frame, rect


class Encoder:
    def __init__(self, target, width: int, height: int, level: str, codec: str, faststart: bool = True):
        # faststart puts the index first, so phones and browsers can start playing before the whole file arrives
        self.out = av.open(target, "w", format="mp4", options={"movflags": "+faststart"} if faststart else {})
        try:
            self.v = self.out.add_stream(codec, rate=30, options=dict(ENCODER_OPTIONS[codec], level=level))
            self.v.width, self.v.height, self.v.pix_fmt = width, height, "yuv420p"
            cc = self.v.codec_context
            cc.time_base = TB
            self.v.time_base = TB
            cc.gop_size = GOP
            cc.max_b_frames = 0
            cc.color_primaries = cc.color_trc = cc.colorspace = 1  # BT.709, as the frames are converted
            cc.color_range = 1                                     # limited ("TV") range
            self.a = self.out.add_stream("aac", rate=AUDIO_RATE, layout="mono")
            self.a.bit_rate = AUDIO_BITRATE
        except BaseException:
            self.out.close()
            raise
        self.resampler = av.AudioResampler(format="fltp", layout="mono", rate=AUDIO_RATE)
        self.frame: av.VideoFrame | None = None
        self.last_pts = -1

    def video(self, rgb: np.ndarray | None, t: float, key: bool) -> int:
        """Encode a new picture (or repeat the last one when rgb is None) shown from time t. Returns its pts."""
        if rgb is not None:
            self.frame = av.VideoFrame.from_ndarray(rgb, format="rgb24").reformat(
                format="yuv420p", src_colorspace=Colorspace.ITU709, dst_colorspace=Colorspace.ITU709)
        f = self.frame
        f.pts = max(round(t * 1000), self.last_pts + 1)
        f.time_base = TB
        f.pict_type = PictureType.I if key else PictureType.NONE
        for p in self.v.encode(f):
            self.out.mux(p)
        self.last_pts = f.pts
        return f.pts

    def audio(self, pcm: np.ndarray, start: int, rate: int) -> None:
        af = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="s16", layout="mono")
        af.sample_rate = rate
        af.pts = start
        af.time_base = Fraction(1, rate)
        for rf in self.resampler.resample(af):
            for p in self.a.encode(rf):
                self.out.mux(p)

    def close(self) -> None:
        for rf in self.resampler.resample(None):
            for p in self.a.encode(rf):
                self.out.mux(p)
        for stream in (self.a, self.v):
            for p in stream.encode(None):
                self.out.mux(p)
        self.out.close()


def _encoder_error(codec: str, width: int, height: int, level: str) -> str | None:
    """Open the encoder and encode one frame in memory. None if it works, else the reason."""
    if codec == "h264_nvenc" and os.environ.get("KOKORO_SIMULATE_NVENC_FAILURE"):
        return "Simulated NVENC failure (test mode, KOKORO_SIMULATE_NVENC_FAILURE is set)"
    try:
        enc = Encoder(io.BytesIO(), width, height, level, codec, faststart=False)
        enc.video(np.zeros((height, width, 3), np.uint8), 0.0, True)
        enc.close()
        return None
    except Exception as e:  # noqa: BLE001 - any failure means "use the other encoder"
        return (str(e).strip().splitlines() or [type(e).__name__])[0][:300]


def choose_encoder(job: dict) -> tuple[str, str]:
    """NVIDIA's hardware encoder when it works on this computer, otherwise x264 on the CPU."""
    if job.get("software"):
        return "libx264", "CPU encoding was selected."
    err = _encoder_error("h264_nvenc", job["width"], job["height"], job["level"])
    if err is None:
        return "h264_nvenc", ""
    return "libx264", f"NVIDIA encoder unavailable: {err}"


def _peak_memory_mb() -> float | None:
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
    c = Counters()
    c.cb = ctypes.sizeof(c)
    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    get_info = ctypes.windll.psapi.GetProcessMemoryInfo
    get_info.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    if not get_info(kernel32.GetCurrentProcess(), ctypes.byref(c), c.cb):
        return None
    return round(c.PeakWorkingSetSize / 2 ** 20, 1)


# ---- protocol --------------------------------------------------------------------------------------------

def _say(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _read_exact(inp, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = inp.read(n - len(buf))
        if not chunk:
            raise EOFError
        buf += chunk
    return bytes(buf)


def _read_msg(inp) -> dict:
    (n,) = struct.unpack("<I", _read_exact(inp, 4))
    return json.loads(_read_exact(inp, n))


class _EncodeFailed(Exception):
    pass


def run(job: dict, inp) -> dict:
    rate = job["sample_rate"]
    codec, reason = choose_encoder(job)
    _say({"encoder": codec, "reason": reason})
    rend = Renderer(job)
    enc = Encoder(job["out"], job["width"], job["height"], job["level"], codec)
    timeline: list | None = [] if job.get("timeline") else None
    lid: int | None = None
    last_t: float | None = None
    frames = 0
    samples = 0
    segs = 0

    def frame(t: float, picture: bool, key: bool = False) -> None:
        nonlocal last_t, frames
        view = rend.view_at(t)
        rgb, rect = rend.render(view, lid) if picture else (None, None)
        try:
            pts = enc.video(rgb, t, key)
        except av.FFmpegError as e:
            raise _EncodeFailed(str(e)) from e
        last_t = t
        frames += 1
        if timeline is not None:
            timeline.append({"pts": pts, "line": lid, "page": view.page, "view": [view.scale, view.x0, view.y0],
                             "rect": rect, "key": key, "repeat": not picture})

    def advance(to: float) -> None:
        """Glide and repeat frames strictly before `to`."""
        while True:
            nxt, picture = last_t + KEEPALIVE, False
            g = rend.next_glide_frame(last_t)
            if g is not None and g < nxt:
                nxt, picture = g, True
            if nxt >= to - 0.0015:
                return
            frame(nxt, picture)

    try:
        while True:
            msg = _read_msg(inp)
            if msg.get("end"):
                break
            pcm = np.frombuffer(_read_exact(inp, 2 * msg["n"]), "<i2")
            t0 = samples / rate
            for line, at in msg["marks"]:
                if line == lid:
                    continue
                t = t0 + at / rate
                if last_t is not None:
                    advance(t)
                lid = line
                frame(t, True, rend.set_line(line, t))
            if pcm.size:
                try:
                    enc.audio(pcm, samples, rate)
                except av.FFmpegError as e:
                    raise _EncodeFailed(str(e)) from e
            samples += pcm.size
            segs += 1
            _say({"seg": segs, "t": samples / rate})
        if last_t is None:
            raise ValueError("There was nothing to put in the video.")
        end = samples / rate
        advance(end)
        if last_t < end - 1 / 30:
            frame(end - 1 / 30, False)  # hold the last picture until the narration ends
        enc.close()
    except BaseException:
        try:
            enc.out.close()
        except Exception:  # noqa: BLE001
            pass
        raise
    if timeline is not None:
        with open(job["timeline"], "w", encoding="utf-8") as f:
            json.dump({"encoder": codec, "width": job["width"], "height": job["height"], "frames": timeline}, f)
    return {"duration": samples / rate, "frames": frames, "encoder": codec, "reason": reason,
            "peak_mb": _peak_memory_mb()}


def main() -> int:
    av.logging.set_level(av.logging.ERROR)
    inp = sys.stdin.buffer
    try:
        job = _read_msg(inp)["job"]
    except (EOFError, ValueError, KeyError):
        return 2
    try:
        _say({"done": run(job, inp)})
        return 0
    except EOFError:  # the application stopped sending: cancelled or closed
        try:
            os.remove(job["out"])
        except OSError:
            pass
        return 2
    except _EncodeFailed as e:
        traceback.print_exc()
        _say({"error": f"The video encoder failed: {e}", "retry_software": True})
        return 3
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        _say({"error": str(e) or type(e).__name__, "retry_software": False})
        return 1


if __name__ == "__main__":
    sys.exit(main())
