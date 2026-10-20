"""Drive the real window through MP4 export and check the interface stays responsive.

Usage: python tests/gui_video_check.py [pdf] [out_dir]
Checks: the export dialog offers MP4 with view and quality choices; a video export started from the
window shows a progress window whose value and label advance; the Qt event loop never stalls (largest
gap between 10 ms timer ticks); narration keeps playing and highlighting during the export; Cancel in
the progress window stops the export and leaves no file and no worker process behind.
"""

import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import av  # noqa: E402
from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton  # noqa: E402

from kokoro_reader.app import _setup_logging  # noqa: E402
from kokoro_reader.docstate import Settings  # noqa: E402
from kokoro_reader.ui.dialogs import ExportDialog  # noqa: E402
from kokoro_reader.ui.main_window import MainWindow  # noqa: E402

pdf = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "samples/resnet_two_column.pdf")
out_dir = Path(sys.argv[2] if len(sys.argv) > 2 else tempfile.mkdtemp())
out_dir.mkdir(parents=True, exist_ok=True)
_setup_logging()
app = QApplication([])
settings = Settings()  # defaults, independent of the user's saved settings
settings.save = lambda: None  # do not overwrite the user's settings file
win = MainWindow(settings)
win.resize(1280, 900)
win.show()
results: list[tuple[str, bool, str]] = []
boxes: list[str] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'} {name} {detail}", flush=True)


def wait(pred, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.005)
    return pred()


def close_message_boxes():  # the "Export finished" box is modal: read it and close it
    w = QApplication.activeModalWidget()
    if isinstance(w, QMessageBox):
        boxes.append(w.text())
        w.done(0)


class StallMeter:
    """Largest gap between ticks of a 10 ms timer: how long the interface was frozen."""

    def __init__(self):
        self.last = time.perf_counter()
        self.worst = 0.0
        self.timer = QTimer(interval=10, timeout=self._tick)

    def _tick(self):
        now = time.perf_counter()
        self.worst = max(self.worst, now - self.last)
        self.last = now

    def start(self):
        self.last, self.worst = time.perf_counter(), 0.0
        self.timer.start()

    def stop(self) -> float:
        self.timer.stop()
        return self.worst


def worker_processes() -> int:
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "@(Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and "
                          "$_.CommandLine -like '*-m kokoro_reader.videoworker*' }).Count"],
                         capture_output=True, text=True)
    return int(out.stdout.strip() or 0)


def main():
    closer = QTimer(interval=100, timeout=close_message_boxes)
    closer.start()
    win.open_pdf(pdf)
    check("PDF opens and the speech engine starts",
          wait(lambda: win.layout_ is not None and win.segments and win.narrator is not None, 90),
          f"{win.layout_.page_count if win.layout_ else 0} pages")
    lay = win.layout_

    # ---- the dialog --------------------------------------------------------------------------------------
    dlg = ExportDialog(lay.page_count, 0, "doc", settings, win)
    dlg.show()
    hidden_for_audio = not dlg.video_box.isVisible()
    dlg.fmt.setCurrentIndex(2)
    wait(lambda: False, 0.2)
    dlg.view.setCurrentIndex(dlg.view.findData("zoom_landscape"))
    dlg.grab().save(str(out_dir / "export_dialog.png"))
    check("export dialog offers MP4 video with view and quality",
          hidden_for_audio and dlg.video_box.isVisible() and dlg.format == "mp4"
          and dlg.video_view == "zoom_landscape" and dlg.video_quality == 1080 and dlg.view.count() == 3,
          f"views: {[dlg.view.itemText(i) for i in range(dlg.view.count())]}")
    dlg.close()

    # ---- an export while narration plays --------------------------------------------------------------
    win.narrator.play(4)
    wait(lambda: win.narrator.state == "playing", 20)
    lines_during = set()
    out = out_dir / "gui_export.mp4"
    meter = StallMeter()
    meter.start()
    t0 = time.perf_counter()
    win.start_export(range(0, 3), str(out), "zoom_portrait", 1080)
    prog = win._export_progress
    seen_values, seen_labels, states = set(), set(), set()

    def running():
        seen_values.add(prog.value())
        seen_labels.add(prog.labelText())
        states.add(win.narrator.state)
        lines_during.add(win.narrator.current_line)
        return win._export_token is None
    finished = wait(running, 600)
    took = time.perf_counter() - t0
    stall = meter.stop()
    wait(lambda: bool(boxes), 5)
    check("video export from the window finishes", finished and out.exists() and boxes
          and "Saved" in boxes[-1], f"{took:.1f}s; message: {boxes[-1].splitlines()[0] if boxes else None}")
    check("encoder named in the finished message", boxes and "NVENC" in boxes[-1] or "x264" in boxes[-1],
          boxes[-1].splitlines()[-1] if boxes else "")
    check("progress window advances", len(seen_values) > 5 and any("Sentence" in l for l in seen_labels),
          f"{len(seen_values)} distinct values; e.g. '{sorted(l for l in seen_labels if 'Sentence' in l)[-1:]}'")
    check("interface stays responsive during export", stall < 0.2,
          f"longest event-loop stall {stall * 1000:.0f} ms over {took:.1f}s")
    check("narration keeps playing during export", "playing" in states and len(lines_during) > 3,
          f"states {sorted(s for s in states if s)}, {len(lines_during)} different lines highlighted")
    with av.open(str(out)) as c:
        check("exported file plays (decodes)", c.streams.video[0].codec_context.name == "h264"
              and c.streams.audio[0].codec_context.name == "aac", f"{float(c.duration / av.time_base):.1f}s")
    win.narrator.stop()

    # ---- cancel from the progress window ----------------------------------------------------------------
    out2 = out_dir / "gui_cancel.mp4"
    win.start_export(range(0, lay.page_count), str(out2), "zoom_landscape", 720)
    prog = win._export_progress
    wait(lambda: prog.value() >= 3, 120)
    workers = worker_processes()
    t1 = time.perf_counter()
    prog.findChild(QPushButton).click()
    stopped = wait(lambda: win._export_token is None, 10)
    stop_time = time.perf_counter() - t1
    wait(lambda: False, 0.5)
    check("Cancel in the progress window stops the export", stopped and stop_time < 2,
          f"stopped {stop_time:.2f}s after Cancel")
    check("cancelled export leaves no file and no worker",
          workers >= 1 and worker_processes() == 0 and not out2.exists()
          and not out2.with_name(out2.name + ".part").exists(), f"workers while running {workers}")

    failed = [n for n, ok, _ in results if not ok]
    print("RESULT:", "PASS" if not failed else f"FAIL {failed}")
    print(f"output in {out_dir}")
    win.close()
    app.exit(1 if failed else 0)


QTimer.singleShot(100, main)
sys.exit(app.exec())
