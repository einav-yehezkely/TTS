"""Checks for the transcript panel and the redesigned controls, on the real window with real audio.

Usage: python tests/gui_ui_check.py [pdf] [screenshot_dir]
"""

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from kokoro_reader.app import _setup_logging  # noqa: E402
from kokoro_reader.docstate import Settings  # noqa: E402
from kokoro_reader.ui.main_window import MainWindow  # noqa: E402

pdf = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "samples/resnet_two_column.pdf")
shots = Path(sys.argv[2] if len(sys.argv) > 2 else tempfile.mkdtemp())
shots.mkdir(parents=True, exist_ok=True)
_setup_logging()
app = QApplication([])
app.setQuitOnLastWindowClosed(False)


def answer_boxes():
    w = QApplication.activeModalWidget()
    if isinstance(w, QMessageBox):
        w.accept()


QTimer(app, interval=200, timeout=answer_boxes).start()
win = MainWindow(Settings())
win.resize(1440, 920)
win.show()
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'} {name} {detail}", flush=True)


def wait(pred, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def pump(s):
    wait(lambda: False, s)


def shot(name):
    pump(0.35)
    win.grab().save(str(shots / f"{name}.png"))


def block_visible(seg):
    t = win.transcript
    r = t._block_rect(t._seg_block[seg])
    top = t.view.verticalScrollBar().value()
    return r.top() >= top - 2 and r.bottom() <= top + t.view.viewport().height() + 2


def main():
    check("engine ready", wait(lambda: win.narrator is not None, 120), win.device_label.text())
    t0 = time.perf_counter()
    win.open_pdf(pdf)
    wait(lambda: win.layout_ is not None and win.segments, 60)
    t = win.transcript
    check("transcript lists every narrated sentence", len(t._seg_block) == len(win.segments) > 0,
          f"{len(win.segments)} sentences, built in {time.perf_counter() - t0:.1f}s with the document")
    shot("ui_01_opened_dark")

    lay = win.layout_
    target = next((l for l in lay.lines if l.page == 0 and l.role == "body" and "ImageNet dataset we" in l.text),
                  None) or lay.lines[win.segments[min(10, len(win.segments) - 1)].first_line]
    win._on_page_click(target.page, target.cx, target.cy)
    wait(lambda: win.narrator.state == "playing", 15)
    seen = set()
    end = time.monotonic() + 8
    in_sync = True
    while time.monotonic() < end:
        app.processEvents()
        n = win.narrator
        if n.state == "playing":
            seen.add(n.current_line)
            pump(0.05)
            if t._cur_seg != n.current_seg or t._cur_line != n.current_line:
                pump(0.1)  # allow the next timer tick
                in_sync &= (t._cur_seg == n.current_seg and t._cur_line == n.current_line)
        time.sleep(0.01)
    check("transcript follows the sentence and the line being read", in_sync and len(seen) >= 2,
          f"lines seen {sorted(seen)[:8]}")
    pump(0.4)
    check("current sentence scrolled into view in the transcript", block_visible(win.narrator.current_seg))
    caption = win.caption.text()
    check("player caption shows the sentence with the current line marked", "background-color" in caption,
          caption[:90])
    shot("ui_02_playing_dark")

    # click a later sentence's second line in the transcript
    seg = next(s for s in win.segments[win.narrator.current_seg + 3:] if len(s.line_ids) >= 2)
    block = t.view.document().findBlockByNumber(t._seg_block[seg.index])
    lid, a, b = t._frag_offsets[seg.index][1]
    t._clicked(block.position() + a + 1)
    ok = wait(lambda: win.narrator.current_seg == seg.index and win.narrator.current_line == lid
              and win.narrator.state == "playing", 10)
    check("clicking a transcript sentence reads from that exact line", ok,
          f"wanted seg {seg.index} line {lid}, got seg {win.narrator.current_seg} line {win.narrator.current_line}")

    # scrolling away suspends following; the pill brings it back
    t.view.verticalScrollBar().setValue(t.view.verticalScrollBar().maximum())
    t._user_scrolled()
    pump(1.5)
    check("scrolling the transcript pauses following and offers 'Back to current'",
          not t.following and t.back_btn.isVisible() and not block_visible(win.narrator.current_seg))
    t.back_btn.click()
    pump(0.6)
    check("'Back to current' resumes following", t.following and block_visible(win.narrator.current_seg))

    # search
    t.search.setText("ImageNet")
    t.find_next()
    check("transcript search finds text", t._search_sel is not None and "match" in t.search_info.text(),
          t.search_info.text())
    t.search.clear()

    # collapse / expand
    w_open = win.splitter.sizes()[0]
    view_w = win.view.width()
    win._set_transcript(False)
    pump(0.5)
    check("transcript collapses to a slim rail and the page gets the room",
          win.splitter.sizes()[0] <= 40 and win.view.width() > view_w + 200,
          f"panel {w_open}px -> {win.splitter.sizes()[0]}px, viewer {view_w} -> {win.view.width()}px")
    shot("ui_03_collapsed")
    win._set_transcript(True)
    pump(0.5)
    check("transcript expands again", abs(win.splitter.sizes()[0] - w_open) < 30 and t.isVisible(),
          f"{win.splitter.sizes()[0]}px")

    # focus mode
    win.narrator.pause()
    pump(0.3)
    img_before = win.view.viewport().grab().toImage()
    win.act_focus.trigger()
    pump(0.4)
    img_after = win.view.viewport().grab().toImage()
    hl = win.view.highlight_rect_in_viewport()
    probe = (hl.center().x(), max(5, hl.top() - 120)) if hl.top() > 130 else (hl.center().x(), hl.bottom() + 120)
    dpr = img_after.devicePixelRatio()

    def lum(img, x, y):
        c = img.pixelColor(int(x * dpr), int(y * dpr))
        return 0.3 * c.red() + 0.59 * c.green() + 0.11 * c.blue()
    away_before, away_after = lum(img_before, *probe), lum(img_after, *probe)
    line_before = lum(img_before, hl.left() + 3, hl.center().y())
    line_after = lum(img_after, hl.left() + 3, hl.center().y())
    check("focus mode dims the page but not the line being read",
          away_after < away_before * 0.6 and abs(line_after - line_before) < 12,
          f"page brightness {away_before:.0f} -> {away_after:.0f}, line {line_before:.0f} -> {line_after:.0f}")
    shot("ui_04_focus")
    win.act_focus.trigger()

    # scrubber seek while playing
    win.narrator.resume()
    wait(lambda: win.narrator.state == "playing", 5)
    win._seek_fraction(0.5)
    ok = wait(lambda: win.narrator.state == "playing", 10)
    frac = win._cum[win.narrator.current_seg] / win._cum[-1]
    check("progress bar jumps to the middle of the document", ok and 0.4 < frac < 0.6,
          f"now at {frac * 100:.0f}% · '{win.page_label.text()}'")
    check("time left is estimated", "left" in win.page_label.text(), win.page_label.text())
    tip = win._describe_fraction(0.25)
    check("hovering the progress bar previews page and text", tip.startswith("Page ") and "“" in tip,
          tip.replace("\n", " | ")[:100])

    # sleep timer (2 seconds for the test)
    win._set_sleep(2)
    ok = wait(lambda: win.narrator.state == "paused", 6)
    check("sleep timer pauses reading", ok, win.sleep_btn.text())

    # light theme
    win._toggle_theme()
    pump(0.5)
    shot("ui_05_light")
    bg = win.transcript.view.palette().color(win.transcript.view.backgroundRole())
    check("light theme applied", win.settings.theme == "light" and bg.lightness() > 200, bg.name())
    win._toggle_theme()
    pump(0.3)
    check("dark theme restored", win.settings.theme == "dark")
    win.narrator.stop()
    failed = [n for n, ok in results if not ok]
    print(f"screenshots in {shots}")
    print("RESULT:", "PASS" if not failed else f"FAIL {failed}")
    win.close()
    app.exit(1 if failed else 0)


QTimer.singleShot(100, main)
sys.exit(app.exec())
