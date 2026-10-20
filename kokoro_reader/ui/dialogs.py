"""Dialogs: one-time model download, export, narration options, help."""

import os
import subprocess
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QTextCursor, QTextTable
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QGroupBox, QHBoxLayout, QLabel, QMessageBox, QProgressBar, QPushButton, QSpinBox,
                               QTextBrowser, QVBoxLayout)

from .. import assets
from ..docstate import Options, Settings
from ..paths import README_HE


class Bridge(QObject):
    """Carries results from worker threads to the GUI thread."""
    progress = Signal(int, int, str)
    done = Signal(object)
    failed = Signal(str)


# ---- one-time setup download --------------------------------------------------------------------------------

class SetupDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("First-time setup: download speech files")
        self.setMinimumWidth(560)
        self._cancel = threading.Event()
        self._bridge = Bridge()
        self._bridge.progress.connect(self._on_progress)
        self._bridge.done.connect(self._on_done)
        self._bridge.failed.connect(self._on_failed)
        missing = assets.missing_assets()
        total = sum(a.size for a in missing) / 1e6
        info = QLabel(
            "<b>The voice model needs to be downloaded once.</b><br><br>"
            f"This downloads the Kokoro-82M speech model and {len(assets.VOICES)} English voices "
            f"(about {total:.0f} MB) from Hugging Face.<br>"
            "<b>This is the only step that needs the internet.</b> After it finishes, reading documents "
            "and generating speech work completely offline, on this computer.<br><br>"
            "No account, payment or key is needed.")
        info.setWordWrap(True)
        self.status = QLabel("Ready to download.")
        self.status.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.start_btn = QPushButton("Download")
        self.start_btn.setDefault(True)
        self.start_btn.clicked.connect(self._start)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._on_cancel)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.start_btn)
        row.addWidget(self.cancel_btn)
        lay = QVBoxLayout(self)
        lay.addWidget(info)
        lay.addSpacing(8)
        lay.addWidget(self.bar)
        lay.addWidget(self.status)
        lay.addLayout(row)
        self._running = False

    def _start(self) -> None:
        self._cancel.clear()
        self._running = True
        self.start_btn.setEnabled(False)
        self.status.setText("Connecting…")

        def work():
            try:
                assets.download_all(lambda d, t, name: self._bridge.progress.emit(d, t, name), self._cancel)
                self._bridge.done.emit(None)
            except assets.DownloadCancelled:
                self._bridge.failed.emit("Download cancelled. Already downloaded parts are kept, so the next "
                                         "attempt continues where this one stopped.")
            except Exception as e:  # noqa: BLE001
                self._bridge.failed.emit(f"{e}\n\nCheck the internet connection and press Retry. Already "
                                         "downloaded parts are kept.")
        threading.Thread(target=work, daemon=True).start()

    def _on_progress(self, done: int, total: int, name: str) -> None:
        self.bar.setValue(int(1000 * done / max(1, total)))
        self.status.setText(f"Downloading {name}… {done / 1e6:.0f} of {total / 1e6:.0f} MB")

    def _on_done(self, _) -> None:
        self._running = False
        self.bar.setValue(1000)
        self.status.setText("All files downloaded. The application now works offline.")
        self.accept()

    def _on_failed(self, msg: str) -> None:
        self._running = False
        self.status.setText(msg)
        self.start_btn.setText("Retry")
        self.start_btn.setEnabled(True)

    def _on_cancel(self) -> None:
        if self._running:
            self._cancel.set()
        else:
            self.reject()

    def closeEvent(self, event) -> None:
        self._cancel.set()
        super().closeEvent(event)


# ---- narration options --------------------------------------------------------------------------------------

class OptionsDialog(QDialog):
    def __init__(self, settings: Settings, cache_mb: float, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Narration options")
        o = settings.options
        self.boxes = {}
        group = QGroupBox("Leave out of narration (detected automatically; detection is approximate)")
        gl = QVBoxLayout(group)
        for name, label in [
            ("skip_footnotes", "Footnotes"),
            ("skip_references", "References / bibliography section"),
            ("skip_symbolic", "Symbol-heavy lines (equations, table numbers, code prompts)"),
            ("skip_figure_text", "Text inside figures and charts"),
            ("skip_captions", "Figure and table captions"),
            ("skip_citations", "Citation markers like [12] or [3, 4]"),
        ]:
            cb = QCheckBox(label)
            cb.setChecked(getattr(o, name))
            gl.addWidget(cb)
            self.boxes[name] = cb
        self.read_headers = QCheckBox("Read repeated page headers, footers and page numbers")
        self.read_headers.setChecked(o.read_headers)
        gl.addWidget(self.read_headers)
        tip = QLabel("Tip: use <b>Edit reading order</b> to remove or restore specific parts of a page.")
        tip.setWordWrap(True)
        gl.addWidget(tip)

        cache_group = QGroupBox("Audio cache")
        form = QFormLayout(cache_group)
        self.cache_limit = QSpinBox()
        self.cache_limit.setRange(100, 100_000)
        self.cache_limit.setSingleStep(500)
        self.cache_limit.setSuffix(" MB")
        self.cache_limit.setValue(settings.cache_limit_mb)
        form.addRow("Maximum size:", self.cache_limit)
        self.cache_info = QLabel(f"Currently used: {cache_mb:.0f} MB")
        self.clear_btn = QPushButton("Clear cache")
        self.clear_requested = False
        self.clear_btn.clicked.connect(self._clear)
        row = QHBoxLayout()
        row.addWidget(self.cache_info)
        row.addStretch(1)
        row.addWidget(self.clear_btn)
        form.addRow(row)

        dev_group = QGroupBox("Speech device")
        dl = QVBoxLayout(dev_group)
        self.prefer_gpu = QCheckBox("Use the NVIDIA GPU when available (applies after restart)")
        self.prefer_gpu.setChecked(settings.prefer_gpu)
        dl.addWidget(self.prefer_gpu)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(group)
        lay.addWidget(cache_group)
        lay.addWidget(dev_group)
        lay.addWidget(buttons)

    def _clear(self) -> None:
        self.clear_requested = True
        self.cache_info.setText("Will be cleared when you press OK")

    def apply(self, settings: Settings) -> None:
        o = Options()
        for name, cb in self.boxes.items():
            setattr(o, name, cb.isChecked())
        o.read_headers = self.read_headers.isChecked()
        settings.options = o
        settings.cache_limit_mb = self.cache_limit.value()
        settings.prefer_gpu = self.prefer_gpu.isChecked()


# ---- export -----------------------------------------------------------------------------------------------------

class ExportDialog(QDialog):
    FORMATS = [("wav", "WAV audio (uncompressed, large)", "WAV audio (*.wav)"),
               ("mp3", "MP3 audio (128 kbps, small)", "MP3 audio (*.mp3)"),
               ("mp4", "MP4 video – the pages with the line being read highlighted", "MP4 video (*.mp4)")]

    def __init__(self, page_count: int, current_page: int, default_name: str, settings: Settings, parent=None):
        super().__init__(parent)
        from ..video import QUALITIES, VIEWS  # here, so the first-run setup dialog does not load the speech engine
        self.setWindowTitle("Export")
        self.page_count = page_count
        self.first = QSpinBox()
        self.first.setRange(1, page_count)
        self.first.setValue(1)
        self.last = QSpinBox()
        self.last.setRange(1, page_count)
        self.last.setValue(page_count)
        self.scope = QComboBox()
        self.scope.addItems(["Whole document", "Page range", "Current page only"])
        self.scope.currentIndexChanged.connect(self._scope_changed)
        self.current_page = current_page
        self.fmt = QComboBox()
        for _, label, _ in self.FORMATS:
            self.fmt.addItem(label)
        self.fmt.setCurrentIndex(next((i for i, f in enumerate(self.FORMATS) if f[0] == settings.export_format), 0))
        self.fmt.currentIndexChanged.connect(self._format_changed)
        self.default_name = default_name
        self.start_dir = settings.last_dir or str(Path.home())
        form = QFormLayout()
        form.addRow("What to export:", self.scope)
        pr = QHBoxLayout()
        pr.addWidget(QLabel("from page"))
        pr.addWidget(self.first)
        pr.addWidget(QLabel("to page"))
        pr.addWidget(self.last)
        pr.addStretch(1)
        form.addRow(pr)
        form.addRow("Format:", self.fmt)

        self.video_box = QGroupBox("Video")
        self.view = QComboBox()
        for key, label in VIEWS.items():
            self.view.addItem(label, key)
        self.view.setCurrentIndex(max(0, self.view.findData(settings.video_view)))
        self.quality = QComboBox()
        for key, label in QUALITIES.items():
            self.quality.addItem(label, key)
        self.quality.setCurrentIndex(max(0, self.quality.findData(settings.video_quality)))
        vform = QFormLayout(self.video_box)
        vform.addRow("View:", self.view)
        vform.addRow("Quality:", self.quality)
        vnote = QLabel("Zoomed views fit the text column to the screen width and follow the line being read, so "
                       "the text stays readable on a phone. Encoding uses the NVIDIA graphics card when available, "
                       "otherwise the processor.")
        vnote.setWordWrap(True)
        vform.addRow(vnote)

        note = QLabel("Export uses the current voice, speed and narration options. Parts already heard are reused "
                      "from the cache. You can keep reading while it runs.")
        note.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Choose file and export…")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self.video_box)
        lay.addWidget(note)
        lay.addWidget(buttons)
        self._scope_changed(0)
        self._format_changed()
        self.path = ""

    @property
    def format(self) -> str:
        return self.FORMATS[self.fmt.currentIndex()][0]

    @property
    def video_view(self) -> str:
        return self.view.currentData()

    @property
    def video_quality(self) -> int:
        return self.quality.currentData()

    def _format_changed(self) -> None:
        self.video_box.setVisible(self.format == "mp4")
        self.adjustSize()

    def _scope_changed(self, idx: int) -> None:
        self.first.setEnabled(idx == 1)
        self.last.setEnabled(idx == 1)
        if idx == 0:
            self.first.setValue(1)
            self.last.setValue(self.page_count)
        elif idx == 2:
            self.first.setValue(self.current_page + 1)
            self.last.setValue(self.current_page + 1)

    def pages(self) -> range:
        a, b = sorted((self.first.value(), self.last.value()))
        return range(a - 1, b)

    def _accept(self) -> None:
        fmt, _, file_filter = self.FORMATS[self.fmt.currentIndex()]
        ext = "." + fmt
        pr = self.pages()
        suffix = "" if (pr.start == 0 and pr.stop == self.page_count) else f"_pages{pr.start + 1}-{pr.stop}"
        name = str(Path(self.start_dir) / f"{self.default_name}{suffix}{ext}")
        path, _ = QFileDialog.getSaveFileName(self, "Save as", name, file_filter)
        if not path:
            return
        if not path.lower().endswith(ext):
            path += ext
        self.path = path
        self.accept()


def open_folder(path: str) -> None:
    try:
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    except OSError:
        pass


# ---- help -------------------------------------------------------------------------------------------------------

class HelpDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("הוראות שימוש")
        self.resize(720, 640)
        view = QTextBrowser()
        view.setOpenExternalLinks(True)
        view.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        try:
            text = README_HE.read_text(encoding="utf-8")
        except OSError:
            text = "README_HE.md was not found."
        view.setMarkdown(text)
        doc = view.document()
        # the Markdown importer sets per-block formats, so right-to-left has to be applied to each block and table
        block = doc.begin()
        while block.isValid():
            cursor = QTextCursor(block)
            fmt = block.blockFormat()
            fmt.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
            fmt.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignAbsolute)
            cursor.setBlockFormat(fmt)
            block = block.next()
        for frame in doc.rootFrame().childFrames():
            if isinstance(frame, QTextTable):
                tf = frame.format()
                tf.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
                frame.setFormat(tf)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(view)
        lay.addWidget(buttons)


def info(parent, title: str, text: str, detail: str = "") -> None:
    box = QMessageBox(QMessageBox.Icon.Information, title, text, QMessageBox.StandardButton.Ok, parent)
    if detail:
        box.setDetailedText(detail)
    box.exec()
