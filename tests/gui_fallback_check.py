"""GUI checks: CPU fallback explanation, OCR-required message, saved reading position.

Runs the real window with a simulated GPU failure, so the whole app runs on the CPU.
Message boxes are answered automatically and their text is recorded.
"""

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["KOKORO_SIMULATE_GPU_FAILURE"] = "1"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pymupdf  # noqa: E402
from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from kokoro_reader.app import _setup_logging  # noqa: E402
from kokoro_reader.docstate import Settings  # noqa: E402
from kokoro_reader.ui.main_window import MainWindow  # noqa: E402

_setup_logging()
app = QApplication([])
app.setQuitOnLastWindowClosed(False)  # the test closes and reopens the main window
boxes: list[str] = []


def answer_boxes():
    w = QApplication.activeModalWidget()
    if isinstance(w, QMessageBox):
        boxes.append(w.windowTitle() + ": " + w.text())
        w.accept()


auto = QTimer(interval=150, timeout=answer_boxes)
auto.start()
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


def main():
    pdf = str(ROOT / "samples/resnet_two_column.pdf")
    win = MainWindow(Settings())
    win.show()
    check("engine starts on CPU after GPU failure", wait(lambda: win.narrator is not None, 120)
          and win.engine.device == "cpu", win.device_label.text())
    wait(lambda: any("processor" in b for b in boxes), 5)
    check("user gets a readable explanation", any("Using the processor" in b for b in boxes), str(boxes[-1:]))
    check("active device shown in the status bar", "CPU" in win.device_label.text(), win.device_label.toolTip()
          .replace("\n", " | "))
    win.open_pdf(pdf)
    wait(lambda: win.layout_ is not None and win.segments, 30)
    lay = win.layout_
    target = next(l for l in lay.lines if l.page == 1 and "Shortcut Connections" in l.text)
    win._on_page_click(1, target.cx, target.cy)
    playing = wait(lambda: win.narrator.state == "playing", 20)
    wait(lambda: False, 6)
    check("CPU narration plays and highlights", playing and win.narrator.current_line is not None
          and win.narrator.player.underruns == 0, f"line {win.narrator.current_line}")
    saved_line = win.narrator.current_line
    win.close()
    wait(lambda: False, 0.5)

    win2 = MainWindow(Settings())
    win2.show()
    wait(lambda: win2.narrator is not None, 120)
    win2.open_pdf(pdf)
    wait(lambda: win2.layout_ is not None and win2.narrator.current_line is not None, 30)
    wait(lambda: False, 0.5)
    msg = win2.statusBar().currentMessage()
    check("reading position restored after reopening", win2.narrator.current_line == saved_line,
          f"saved {saved_line}, restored {win2.narrator.current_line}; status: '{msg}'")
    vis = win2.view.visible_pages()
    check("restored position is scrolled into view", lay.lines[saved_line].page in vis, f"visible {vis}")

    # scanned PDF
    src = pymupdf.open(pdf)
    scan = pymupdf.open()
    for i in range(2):
        pix = src[i].get_pixmap(dpi=90)
        pg = scan.new_page(width=src[i].rect.width, height=src[i].rect.height)
        pg.insert_image(pg.rect, pixmap=pix)
    path = Path(tempfile.mkdtemp()) / "scanned_book.pdf"
    scan.save(path)
    n = len(boxes)
    win2.open_pdf(str(path))
    wait(lambda: len(boxes) > n, 20)
    check("scanned PDF shows 'OCR required'", any("OCR required" in b for b in boxes[n:])
          and win2.banner.isVisible(), boxes[n:])
    win2.grab().save(str(Path(tempfile.gettempdir()) / "kokoro_ocr_banner.png"))
    win2.close()
    failed = [name for name, ok in results if not ok]
    print("RESULT:", "PASS" if not failed else f"FAIL {failed}")
    app.exit(1 if failed else 0)


QTimer.singleShot(100, main)
sys.exit(app.exec())
