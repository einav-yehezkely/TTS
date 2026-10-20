"""End-to-end check of MP4 video export: real synthesis, real encoding, then the file is decoded and measured.

Usage: python tests/video_check.py [out_dir] [--long]

For each export the expected timeline is rebuilt independently from the
narration (the cached per-sentence audio and line marks), not from the
worker's own frame log, and compared with what the decoded video shows:
  - container: H.264 High, level, yuv420p, no B-frame reordering, AAC, moov before mdat, stream durations
  - audio: the decoded AAC track against the narration samples (offset by cross-correlation)
  - highlight: in every decoded frame the highlight box is found by its edge color and
    must sit on the line the narration is speaking at that frame's time
  - line switches: the frame that shows a new line must start at that line's audio sample
  - page changes: at the first mark on the new page, on a keyframe
  - readability: body text size in pixels; PNG snapshots are saved for viewing
  - playback by a second decoder: Windows Media Foundation (tests/mf_decode_check.ps1) reads the
    file, decodes frames that must show the same highlight, and transcodes it (decoding every sample)
Also: NVENC fallback to x264 (simulated failure), cancellation, and (--long) memory on a long range.
"""

import difflib
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import av
import numpy as np
import pymupdf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from kokoro_reader.assets import MODEL_FILE  # noqa: E402
from kokoro_reader.audiocache import AudioCache  # noqa: E402
from kokoro_reader.docstate import DocState, Options  # noqa: E402
from kokoro_reader.engine import SAMPLE_RATE, Cancelled, CancelToken, KokoroEngine  # noqa: E402
from kokoro_reader.export import segment_audio  # noqa: E402
from kokoro_reader.layout import extract  # noqa: E402
from kokoro_reader.paths import CACHE_DIR, VOICES_DIR  # noqa: E402
from kokoro_reader.segments import build_segments  # noqa: E402
from kokoro_reader.video import export_video  # noqa: E402
from kokoro_reader.videoworker import EDGE  # noqa: E402

args = [a for a in sys.argv[1:] if not a.startswith("--")]
OUT = Path(args[0] if args else tempfile.mkdtemp(prefix="video_check_"))
OUT.mkdir(parents=True, exist_ok=True)
LONG = "--long" in sys.argv
VOICE, SPEED = "af_heart", 1.0
results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'} {name} {detail}", flush=True)


def top_atoms(path: Path) -> list[str]:
    names = []
    with open(path, "rb") as f:
        while True:
            head = f.read(8)
            if len(head) < 8:
                return names
            size, name = struct.unpack(">I4s", head)
            if size == 1:
                size = struct.unpack(">Q", f.read(8))[0]
                f.seek(size - 16, 1)
            else:
                f.seek(size - 8, 1)
            names.append(name.decode("latin-1"))


def expected_timeline(segments, synths):
    """(time s, line) of every line start, plus total duration, from the narration alone."""
    marks, offset = [], 0
    for synth in synths:
        for lid, s in synth.marks:
            if not marks or marks[-1][1] != lid:
                marks.append(((offset + s) / SAMPLE_RATE, lid))
        offset += len(synth.audio)
    return marks, offset / SAMPLE_RATE


def line_at(marks, t):
    """The line being spoken at t. Frame times are whole milliseconds, so a switch may show up to 0.5 ms early."""
    cur = marks[0][1]
    for at, lid in marks:
        if at <= t + 0.0006:
            cur = lid
        else:
            break
    return cur


def detect_highlight(img: np.ndarray, tolerance: int = 60):
    """Bounding box of the highlight's orange edge, or None."""
    d = np.abs(img.astype(np.int16) - np.array(EDGE, np.int16)).sum(axis=2)
    ys, xs = np.nonzero(d < tolerance)
    if len(xs) < 40:
        return None
    return [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1]


def iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union else 0.0


def expected_rect(layout, lid, view, w, h):
    scale, vx, vy = view
    x0, y0, x1, y1 = layout.lines[lid].bbox
    return [max(0, int((x0 - 2 - vx) * scale)), max(0, int((y0 - 1.5 - vy) * scale)),
            min(w, int((x1 + 2 - vx) * scale) + 1), min(h, int((y1 + 1.5 - vy) * scale) + 1)]


def save_png(img: np.ndarray, path: Path) -> None:
    pymupdf.Pixmap(pymupdf.csRGB, img.shape[1], img.shape[0], img.tobytes(), 0).save(str(path))


def analyze(tag, mp4: Path, timeline: Path, layout, segments, synths, encoder_expected, size_expected):
    marks, total = expected_timeline(segments, synths)
    tl = json.loads(timeline.read_text())
    c = av.open(str(mp4))
    vs, as_ = c.streams.video[0], c.streams.audio[0]
    vcc, acc = vs.codec_context, as_.codec_context
    vdur, adur = float(vs.duration * vs.time_base), float(as_.duration * as_.time_base)
    check(f"{tag}: H.264 High yuv420p {size_expected[0]}x{size_expected[1]}",
          vcc.name == "h264" and vcc.profile == "High" and vcc.pix_fmt == "yuv420p"
          and (vcc.width, vcc.height) == tuple(size_expected), f"{vcc.name} {vcc.profile} level {vcc.level / 10:.1f}")
    check(f"{tag}: H.264 level within 4.0", vcc.level <= 40, f"level {vcc.level / 10:.1f}")
    check(f"{tag}: AAC audio", acc.name == "aac" and as_.sample_rate == 48000 and acc.layout.nb_channels == 1,
          f"{acc.name} {as_.sample_rate} Hz {acc.layout.name} {acc.bit_rate // 1000 if acc.bit_rate else '?'} kbps")
    with av.open(str(mp4)) as c2:
        packets = [(pk.pts, pk.dts) for pk in c2.demux(video=0) if pk.pts is not None]
    check(f"{tag}: no frame reordering (decode order = display order)", all(a == b for a, b in packets),
          f"{sum(a != b for a, b in packets)} of {len(packets)} packets reordered, first video pts {packets[0][0]}")
    atoms = top_atoms(mp4)
    check(f"{tag}: index before media data (faststart)", atoms.index("moov") < atoms.index("mdat"), " ".join(atoms))
    check(f"{tag}: encoder", tl["encoder"] == encoder_expected, tl["encoder"])
    check(f"{tag}: audio length = narration", abs(adur - total) < 0.03, f"{adur:.3f}s vs {total:.3f}s")
    check(f"{tag}: video length = audio length", abs(vdur - adur) < 0.06, f"video {vdur:.3f}s, audio {adur:.3f}s")

    # ---- audio: the decoded track against the narration samples -------------------------------------------
    pcm = np.concatenate([np.clip(s.audio, -1, 1) for s in synths]).astype(np.float32)
    ref = np.interp(np.arange(len(pcm) * 2) / 2, np.arange(len(pcm)), pcm).astype(np.float32)  # to 48 kHz
    chunks, first_pts = [], None
    for fr in c.decode(audio=0):
        if first_pts is None:
            first_pts = float(fr.pts * fr.time_base)
        chunks.append(fr.to_ndarray().reshape(-1))
    dec = np.concatenate(chunks)
    lags, corrs = [], []
    win = 48000 * 2
    for frac in (0.1, 0.5, 0.9):
        p = int(len(ref) * frac)
        a, b = ref[p:p + win], dec[p:p + win]
        if len(a) < win or len(b) < win:
            continue
        n = 2 * win
        xc = np.fft.irfft(np.fft.rfft(b, n) * np.conj(np.fft.rfft(a, n)), n)
        xc = np.concatenate([xc[-480:], xc[:481]])  # lags -10..+10 ms
        lag = int(np.argmax(xc)) - 480
        lags.append(lag / 48)
        s = b[max(0, lag):][:win - 480]
        r = a[max(0, -lag):][:len(s)]
        corrs.append(float(np.corrcoef(r, s)[0, 1]))
    check(f"{tag}: audio in time with the narration", lags and max(abs(x) for x in lags) <= 1.0,
          f"offset {', '.join(f'{x:+.2f}' for x in lags)} ms at 10/50/90%, first audio pts {first_pts:+.3f}s, "
          f"waveform correlation {min(corrs):.3f}")

    # ---- video: what each decoded frame shows ----------------------------------------------------------
    c.seek(0)
    frames = [(round(fr.time * 1000), fr.key_frame, fr.to_ndarray(format="rgb24")) for fr in c.decode(video=0)]
    c.close()
    by_pts = {f["pts"]: f for f in tl["frames"]}
    check(f"{tag}: decoded frames match the encoder's", len(frames) == len(tl["frames"])
          and all(p in by_pts for p, _, _ in frames), f"{len(frames)} decoded, {len(tl['frames'])} encoded")
    doc = pymupdf.open(layout.path)
    wrong_line, text_bad, worst_iou = [], [], 1.0
    shown = []  # (pts, line seen, keyframe)
    for pts, key, img in frames:
        meta = by_pts.get(pts)
        want = line_at(marks, pts / 1000)
        box = detect_highlight(img)
        if meta is None or box is None:
            wrong_line.append((pts, "no highlight"))
            continue
        if meta["page"] != layout.lines[want].page:
            wrong_line.append((pts, f"page {meta['page']} shown, line {want} is on {layout.lines[want].page}"))
            continue
        exp = expected_rect(layout, want, meta["view"], img.shape[1], img.shape[0])
        score = iou(box, exp)
        worst_iou = min(worst_iou, score)
        if score < 0.85:
            # which line is it then?
            wrong_line.append((pts, f"expected line {want}, IoU {score:.2f}"))
            continue
        shown.append((pts, want, key))
        # the boxed area of the page must contain that line's words
        scale, vx, vy = meta["view"]
        rect = pymupdf.Rect(box[0] / scale + vx + 2, box[1] / scale + vy + 1.5, box[2] / scale + vx - 2,
                            box[3] / scale + vy - 1.5)
        got = " ".join(doc[meta["page"]].get_textbox(rect).split())
        ratio = difflib.SequenceMatcher(None, got, " ".join(layout.lines[want].text.split())).ratio()
        if ratio < 0.8:
            text_bad.append((pts, round(ratio, 2), got[:40]))
    check(f"{tag}: every frame highlights the line being spoken", not wrong_line,
          f"{len(frames) - len(wrong_line)}/{len(frames)} frames, worst box IoU {worst_iou:.2f}"
          + (f"; e.g. {wrong_line[:3]}" if wrong_line else ""))
    check(f"{tag}: highlighted area contains that line's text", not text_bad,
          f"{len(shown) - len(text_bad)}/{len(shown)} frames" + (f"; e.g. {text_bad[:3]}" if text_bad else ""))

    # ---- line switches happen at the audio sample of the line start -------------------------------------
    errors, missing = [], []
    pts_list = [p for p, _, _ in frames]
    seen = {p: lid for p, lid, _ in shown}
    for at, lid in marks:
        ms = at * 1000
        cands = [p for p in pts_list if abs(p - ms) <= 1.5]
        if not cands or seen.get(cands[0]) != lid:
            missing.append((round(at, 3), lid))
            continue
        before = [p for p in pts_list if p < cands[0]]
        if before and seen.get(before[-1]) == lid:
            missing.append((round(at, 3), lid, "already shown earlier"))
            continue
        errors.append(cands[0] - ms)
    check(f"{tag}: highlight switches at the audio time of each line", not missing,
          f"{len(errors)}/{len(marks)} switches, max |frame time - audio time| "
          f"{max((abs(e) for e in errors), default=0):.2f} ms" + (f"; missing {missing[:3]}" if missing else ""))

    # ---- page changes ---------------------------------------------------------------------------------
    page_changes = []
    for (at, lid), (_, prev) in zip(marks[1:], marks):
        if layout.lines[lid].page != layout.lines[prev].page:
            p = min(pts_list, key=lambda x: abs(x - at * 1000))
            key = next(k for q, k, _ in frames if q == p)
            page_changes.append((round(at, 3), layout.lines[prev].page + 1, layout.lines[lid].page + 1, key,
                                 abs(p - at * 1000)))
    check(f"{tag}: page changes on time, on keyframes",
          page_changes and all(k and d <= 1.5 for *_, k, d in page_changes),
          "; ".join(f"p{a}->p{b} at {t}s key={k}" for t, a, b, k, _ in page_changes) or "no page change")
    keys = [p for p, k, _ in frames if k]
    gaps = np.diff(keys) / 1000 if len(keys) > 1 else [vdur]
    longest = max(np.diff(pts_list)) / 1000
    glides = sum(1 for f in tl["frames"] if not f["repeat"]) - len(marks)
    print(f"  {len(frames)} frames for {vdur:.1f}s ({len(frames) / vdur:.2f}/s avg): {len(marks)} line starts, "
          f"{glides} camera-glide frames, longest frame {longest:.2f}s, longest keyframe gap {max(gaps):.1f}s")

    # ---- readability ------------------------------------------------------------------------------------
    scales = sorted(f["view"][0] for f in tl["frames"])
    scale = scales[len(scales) // 2]
    print(f"  body text {layout.body_size:.1f} pt -> {layout.body_size * scale:.0f} px per em at the median "
          f"scale {scale:.2f} px/pt, in a {vcc.width}x{vcc.height} frame")
    mf_check(tag, mp4, frames, [round(t + 0.05, 2) for t, *_ in page_changes] + [round(vdur * 0.4, 2)], vdur)
    picks = [frames[0], frames[len(frames) // 2]]
    picks += [f for f in frames if any(abs(f[0] - t * 1000) <= 1.5 for t, *_ in page_changes)][:1]
    for pts, _, img in picks:
        save_png(img, OUT / f"{tag}_{pts / 1000:07.2f}s.png")
    return {"duration": vdur, "frames": len(frames), "mb": mp4.stat().st_size / 1e6}


def mf_check(tag, mp4: Path, frames, times, duration):
    """Decode with Windows Media Foundation and compare its frames with FFmpeg's."""
    out = OUT / f"{tag}_mf"
    out.mkdir(exist_ok=True)
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                        str(ROOT / "tests/mf_decode_check.ps1"), str(mp4), str(out), ",".join(map(str, times))],
                       capture_output=True, text=True, timeout=600)
    info = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line and not line.startswith("frame"))
    check(f"{tag}: Media Foundation reads H.264 + AAC, same duration",
          info.get("video", "").startswith("H264") and info.get("audio", "").startswith("AAC")
          and abs(float(info.get("duration_s", 0)) - duration) < 0.05,
          f"{info.get('video')}, {info.get('audio')}, {info.get('duration_s')}s" + (f" {r.stderr[-300:]}" if r.returncode else ""))
    worst = 1.0
    for t in times:
        jpg = out / f"mf_{t}.jpg"
        if not jpg.exists():
            worst = 0.0
            continue
        pix = pymupdf.Pixmap(str(jpg))
        img = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3]
        ff = max((f for f in frames if f[0] <= t * 1000), key=lambda f: f[0])[2]
        a, b = detect_highlight(img, tolerance=110), detect_highlight(ff)
        worst = min(worst, iou(a, b) if a and b else 0.0)
    check(f"{tag}: Media Foundation frames show the same highlight as FFmpeg's", worst >= 0.85,
          f"{len(times)} frames at {times}s, worst box IoU {worst:.2f}")
    tc = out / "mf_transcoded.mp4"
    ok = "transcode=None" in r.stdout and tc.exists()
    if ok:
        c = av.open(str(tc))
        tdur, has_audio = float(c.duration / av.time_base), bool(c.streams.audio)
        c.close()
        ok = abs(tdur - duration) < 0.2 and has_audio
        detail = f"re-encoded {tdur:.2f}s with audio={has_audio}"
    else:
        detail = r.stdout[-200:]
    check(f"{tag}: Media Foundation decodes the whole file (transcode)", ok, detail)


def run_export(tag, layout, pages, view, quality, max_segs=None, expect="h264_nvenc", env=None):
    segs = build_segments(layout, DocState(layout.doc_id), Options(), pages)
    if max_segs:
        segs = segs[:max_segs]
    out, tl = OUT / f"{tag}.mp4", OUT / f"{tag}.timeline.json"
    old = {k: os.environ.get(k) for k in (env or {})}
    os.environ.update(env or {})
    try:
        t = time.perf_counter()
        res = export_video(engine, cache, layout, segs, out, VOICE, SPEED, view, quality, timeline=str(tl))
        took = time.perf_counter() - t
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print(f"{tag}: {len(segs)} sentences, {res.duration:.1f}s of video in {took:.1f}s with {res.encoder}"
          f"{' (' + res.fallback_reason + ')' if res.fallback_reason else ''}, worker peak {res.worker_peak_mb} MB")
    synths = [segment_audio(engine, cache, s, VOICE, SPEED) for s in segs]  # cached by the export
    size = json.loads(tl.read_text())
    info = analyze(tag, out, tl, layout, segs, synths, expect, (size["width"], size["height"]))
    return res, info


def worker_processes() -> int:
    """Python processes running the worker (the venv's python.exe is a launcher with a child interpreter)."""
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "@(Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and "
                          "$_.CommandLine -like '*-m kokoro_reader.videoworker*' }).Count"],
                         capture_output=True, text=True)
    return int(out.stdout.strip() or 0)


engine = KokoroEngine(MODEL_FILE, VOICES_DIR)
print("speech device:", engine.device_label(), flush=True)
cache = AudioCache(CACHE_DIR)
paper = extract(ROOT / "samples/resnet_two_column.pdf")
book = extract(ROOT / "samples/think_python_book.pdf")

# 1. two-column paper, zoomed portrait, GPU encoder: pages 1-2 (crosses a page and both columns)
run_export("paper_zoom_portrait", paper, range(0, 2), "zoom_portrait", 1080)
# 2. book, full page, 720p, NVENC made to fail: must fall back to x264
res, _ = run_export("book_page_x264", book, range(22, 24), "page", 720, expect="libx264",
                    env={"KOKORO_SIMULATE_NVENC_FAILURE": "1"})
check("NVENC failure falls back to the CPU encoder", res.encoder == "libx264" and "Simulated" in res.fallback_reason,
      res.fallback_reason)
# 3. book, zoomed landscape
run_export("book_zoom_landscape", book, range(32, 34), "zoom_landscape", 1080)

# 4. cancellation in the middle of a long export
segs = build_segments(book, DocState(book.doc_id), Options(), range(0, book.page_count))
token = CancelToken()
out = OUT / "cancelled.mp4"
box: dict = {}


def work():
    t = time.perf_counter()
    try:
        export_video(engine, cache, book, segs, out, VOICE, SPEED, "zoom_portrait", 1080,
                     progress=lambda d, n, s: box.__setitem__("progress", d), token=token)
        box["result"] = "finished"
    except Cancelled:
        box["result"] = "cancelled"
    except Exception as e:  # noqa: BLE001
        box["result"] = f"error {e}"
    box["took"] = time.perf_counter() - t


th = threading.Thread(target=work)
th.start()
time.sleep(6)
running = worker_processes()
t_cancel = time.perf_counter()
token.cancel()
th.join(30)
stopped_in = time.perf_counter() - t_cancel
time.sleep(0.5)
left = worker_processes()
check("cancel stops a long video export", box.get("result") == "cancelled" and stopped_in < 3,
      f"{len(segs)} sentences, {box.get('progress', 0)} done when cancelled, stopped {stopped_in:.2f}s after cancel")
check("cancel ends the worker process and removes the partial file",
      running >= 1 and left == 0 and not out.exists() and not out.with_name(out.name + ".part").exists(),
      f"workers before {running}, after {left}")

# 5. long range: memory stays flat
if LONG:
    rng = range(30, 70)
    segs = build_segments(book, DocState(book.doc_id), Options(), rng)
    t = time.perf_counter()
    res = export_video(engine, cache, book, segs, OUT / "book_long.mp4", VOICE, SPEED, "zoom_portrait", 1080)
    took = time.perf_counter() - t
    c = av.open(str(OUT / "book_long.mp4"))
    d = float(c.duration / av.time_base)
    c.close()
    check("long export (40 pages)", abs(d - res.duration) < 0.1,
          f"{len(segs)} sentences, {res.duration / 60:.1f} min of video in {took:.0f}s, {res.frames} frames, "
          f"worker peak memory {res.worker_peak_mb} MB, {(OUT / 'book_long.mp4').stat().st_size / 1e6:.1f} MB file")

print()
failed = [r for r in results if not r[1]]
print(f"{len(results) - len(failed)}/{len(results)} checks passed. Output in {OUT}")
sys.exit(1 if failed else 0)
