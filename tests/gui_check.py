"""Drive the real application window and check highlighting, following, zoom and export.

Usage: python tests/gui_check.py [pdf] [screenshot_dir]
Each step waits on real playback. Screenshots are saved for visual inspection.
"""

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from kokoro_reader.app import _setup_logging  # noqa: E402
from kokoro_reader.docstate import Settings  # noqa: E402
from kokoro_reader.ui.main_window import MainWindow  # noqa: E402

pdf = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "samples/resnet_two_column.pdf")
shots = Path(sys.argv[2] if len(sys.argv) > 2 else tempfile.mkdtemp())
shots.mkdir(parents=True, exist_ok=True)
_setup_logging()
app = QApplication([])
settings = Settings()  # defaults, independent of the user's saved settings
win = MainWindow(settings)
win.resize(1280, 900)
win.show()
results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'} {name} {detail}", flush=True)


def wait(pred, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def pump(seconds):
    wait(lambda: False, seconds)


def ink_alignment(tag):
    """Check the highlight box covers the rendered text line: ink inside, gaps above and below."""
    view = win.view
    pump(0.4)  # let the page bitmap at this zoom arrive
    r = view.highlight_rect_in_viewport()
    img: QImage = view.viewport().grab().toImage()
    img.save(str(shots / f"{tag}.png"))
    if r is None:
        return False, "no highlight"
    lid = win.narrator.current_line
    line = win.layout_.lines[lid]
    # the line's own box in viewport pixels (without the highlight padding)
    exact = view.mapFromScene(view.page_rect_scene(line.page, line.bbox)).boundingRect()
    dpr = img.devicePixelRatio()

    def dark_rows(x0, x1, y0, y1):
        rows = []
        for y in range(max(0, y0), min(img.height() // 1, y1)):
            dark = 0
            for x in range(max(0, x0), min(img.width(), x1), 2):
                c = img.pixelColor(int(x * dpr), int(y * dpr))
                # text is dark (grey when anti-aliased at small zoom); the yellow tint stays bright
                if 0.3 * c.red() + 0.59 * c.green() + 0.11 * c.blue() < 150:
                    dark += 1
            rows.append(dark)
        return rows
    inside = dark_rows(exact.left(), exact.right(), exact.top(), exact.bottom() + 1)
    h = max(1, exact.height())
    above = dark_rows(exact.left(), exact.right(), exact.top() - h // 3, exact.top() - 1)
    ink_inside = sum(inside)
    visible = view.viewport().rect().contains(r)
    ok = ink_inside > 10 and visible and (not above or max(above) <= max(inside) * 0.6)
    return ok, (f"line {lid} '{line.text[:40]}' box={exact.getRect()} ink_inside={ink_inside} "
                f"max_above={max(above) if above else 0} max_inside={max(inside) if inside else 0} visible={visible}")


def main():
    t0 = time.monotonic()
    check("engine starts", wait(lambda: win.narrator is not None, 120), win.device_label.text())
    if win.engine:
        check("GPU verified", win.engine.device == "cuda" and (win.engine.gpu_share or 0) > 0.5,
              win.engine.device_details().replace("\n", " | "))
    win.open_pdf(pdf)
    check("PDF opens", wait(lambda: win.layout_ is not None and win.segments, 60),
          f"{win.layout_.page_count if win.layout_ else 0} pages, {len(win.segments)} sentences")
    lay = win.layout_
    pump(1.0)
    win.grab().save(str(shots / "01_opened.png"))

    # click a body line in the left column of page 1 and check playback starts at that line
    target = next((l for l in lay.lines if l.page == 0 and l.role == "body" and "ImageNet dataset we" in l.text),
                  None)
    if target is None:  # other documents: first sentence spanning 3+ lines, at least a few pages in
        seg = next(s for s in win.segments if len(s.line_ids) >= 3 and lay.lines[s.first_line].page >= min(
            25, lay.page_count // 4))
        target = lay.lines[seg.first_line]
    win._on_page_click(target.page, target.cx, target.cy)
    started = wait(lambda: win.narrator.state == "playing", 15)
    check("click starts reading at that line", started and win.narrator.current_line == target.id,
          f"clicked {target.id}, reading {win.narrator.current_line}, startup {time.monotonic() - t0:.1f}s")
    pump(1.5)
    for z, tag in ((1.0, "zoom100"), (1.6, "zoom160"), (2.5, "zoom250"), (0.7, "zoom070")):
        win.view.set_zoom(z)
        win._goto_current()
        ok, detail = ink_alignment(f"02_{tag}")
        check(f"highlight on text at zoom {int(z * 100)}%", ok, detail)
    win.view.set_fit_width()
    win.resize(1000, 800)
    pump(0.3)
    win._goto_current()
    ok, detail = ink_alignment("02_fit_after_resize")
    check("highlight on text after window resize (fit width)", ok, detail)

    # the highlight must move from line to line inside a multi-line sentence
    seg = win.narrator.current_seg
    win.narrator.play(seg)  # watch the sentence from its first word
    lines_seen = []

    def watch():
        if win.narrator.current_line not in lines_seen:
            lines_seen.append(win.narrator.current_line)
        return win.narrator.current_seg != seg
    wait(watch, 30)
    expected = win.segments[seg].line_ids
    check("highlight moves across lines of one sentence", lines_seen[:len(expected)] == expected[:len(lines_seen)]
          and len(lines_seen) >= min(2, len(expected)), f"expected {expected}, saw {lines_seen}")

    # automatic page change: start at the last sentence of page 1, follow should bring page 2 into view
    p = target.page
    last_p1 = max((s for s in win.segments if lay.lines[s.first_line].page == p), key=lambda s: s.index)
    win.view.set_zoom(1.6)
    win.narrator.play(last_p1.index)
    moved = wait(lambda: lay.lines[win.narrator.current_line].page == p + 1, 60)
    pump(0.6)
    vis = win.view.visible_pages()
    r = win.view.highlight_rect_in_viewport()
    check("automatic page change follows reading", moved and p + 1 in vis and r is not None
          and win.view.viewport().rect().contains(r), f"visible pages {vis}, highlight {r and r.getRect()}")
    win.grab().save(str(shots / "03_page2.png"))

    # follow off: browsing elsewhere must not be undone by the narration
    win.follow.setChecked(False)
    win.view.verticalScrollBar().setValue(win.view.verticalScrollBar().maximum())
    pump(4.0)
    vis = win.view.visible_pages()
    check("follow off keeps my scroll position", lay.page_count - 1 in vis, f"visible {vis}")
    win.follow.setChecked(True)
    pump(0.5)
    vis = win.view.visible_pages()
    check("follow on returns to reading line", lay.lines[win.narrator.current_line].page in vis, f"visible {vis}")

    # pause / resume from the GUI action
    win._play_pause()
    pump(0.3)
    line_at_pause = win.narrator.current_line
    pump(2.0)
    check("pause holds the highlight", win.narrator.state == "paused" and win.narrator.current_line == line_at_pause)
    win._play_pause()
    check("resume continues", wait(lambda: win.narrator.state == "playing", 5))

    # edit mode overlay screenshot
    win.narrator.stop()
    win.view.set_fit_width()
    win.view.verticalScrollBar().setValue(0)
    pump(0.5)
    win._set_edit(True)
    pump(0.8)
    win.grab().save(str(shots / "04_edit_mode.png"))
    check("edit mode lists blocks", win.block_list.count() > 3, f"{win.block_list.count()} blocks on page 1")
    win._set_edit(False)

    # export page 1 to WAV and MP3
    from kokoro_reader.export import export_audio
    from kokoro_reader.segments import build_segments
    import soundfile as sf
    segs = build_segments(lay, win.state, win.settings.options, range(target.page, target.page + 1))
    for ext in (".wav", ".mp3"):
        out = shots / f"page1{ext}"
        t = time.perf_counter()
        secs = export_audio(win.engine, win.cache, segs, out, win.narrator.voice, win.narrator.speed)
        size = out.stat().st_size
        detail = f"{secs:.1f}s audio, {size / 1e6:.1f} MB, {time.perf_counter() - t:.1f}s to export"
        if ext == ".wav":
            info = sf.info(str(out))
            check("WAV export", abs(info.duration - secs) < 0.05 and info.samplerate == 24000, detail)
        else:
            check("MP3 export", size > secs * 128000 / 8 * 0.8, detail)
    print(f"\nscreenshots in {shots}")
    failed = [n for n, ok, _ in results if not ok]
    print("RESULT:", "PASS" if not failed else f"FAIL {failed}")
    win.close()
    app.exit(1 if failed else 0)


QTimer.singleShot(100, main)
sys.exit(app.exec())
