"""Main window: PDF viewer in the center, playback toolbar, reading-order editor."""

import logging
import threading
import time
from pathlib import Path

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QFontMetrics, QKeySequence
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDockWidget, QFileDialog, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QProgressDialog, QPushButton,
                               QSlider, QStyle, QToolBar, QVBoxLayout, QWidget)

from .. import assets
from ..audiocache import AudioCache
from ..docstate import DocState, Settings
from ..engine import Cancelled, CancelToken, KokoroEngine
from ..export import export_audio
from ..layout import ROLE_LABELS, DocLayout, load_or_extract
from ..narrator import Narrator
from ..paths import CACHE_DIR, EXTRACT_CACHE_DIR, VOICES_DIR
from ..segments import build_segments, line_included, page_blocks, page_order
from .dialogs import Bridge, ExportDialog, HelpDialog, OptionsDialog, info, open_folder
from .viewer import PdfView

log = logging.getLogger(__name__)

STATE_TEXT = {"stopped": "Stopped", "playing": "Playing", "paused": "Paused",
              "buffering": "Preparing speech…"}


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings
        self.setWindowTitle("Kokoro PDF Reader")
        self.resize(1200, 900)
        self.engine: KokoroEngine | None = None
        self.narrator: Narrator | None = None
        self.cache: AudioCache | None = None
        self.layout_: DocLayout | None = None
        self.state: DocState | None = None
        self.segments = []
        self.reading_order: list[int] = []
        self._state_dirty = False
        self._export_token: CancelToken | None = None
        self._selected_block: int | None = None
        self._edit_page = 0
        self._bridge = Bridge()

        self.view = PdfView(self)
        self.setCentralWidget(self._central())
        self._build_actions()
        self._build_toolbar()
        self._build_menu()
        self._build_status()
        self._build_edit_dock()
        self.view.lineClicked.connect(self._on_page_click)
        self.view.regionSelected.connect(self._on_region)
        self.view.currentPageChanged.connect(self._on_page_changed)
        self.view.zoomChanged.connect(lambda z: self.zoom_label.setText(f"{z * 100:.0f}%"))
        if settings.zoom:
            self.view.set_zoom(settings.zoom)
        self._save_timer = QTimer(self, interval=3000, timeout=self._save_state)
        self._save_timer.start()
        self._set_controls_enabled(False)
        self._start_engine()

    # ---- layout ------------------------------------------------------------------------------------------
    def _central(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.now_reading = QLabel("Open a PDF to start (File → Open, or Ctrl+O).")
        self.now_reading.setStyleSheet("QLabel{background:#fff8e1;color:#333;padding:5px 10px;border-bottom:1px solid #e0c97a;}")
        self.now_reading.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.banner = QLabel("")
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet("QLabel{background:#fdecea;color:#611a15;padding:6px 10px;font-weight:bold;}")
        self.banner.hide()
        lay.addWidget(self.banner)
        lay.addWidget(self.now_reading)
        lay.addWidget(self.view, 1)
        return w

    def _icon(self, sp):
        return self.style().standardIcon(sp)

    def _build_actions(self) -> None:
        S = QStyle.StandardPixmap
        self.act_open = QAction(self._icon(S.SP_DialogOpenButton), "Open PDF…", self,
                                shortcut=QKeySequence(QKeySequence.StandardKey.Open), triggered=self.choose_pdf)
        self.act_play = QAction(self._icon(S.SP_MediaPlay), "Play", self, shortcut="Space", triggered=self._play_pause)
        self.act_play.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        self.act_stop = QAction(self._icon(S.SP_MediaStop), "Stop", self, shortcut="Esc",
                                triggered=lambda: self.narrator and self.narrator.stop())
        self.act_prev = QAction(self._icon(S.SP_MediaSkipBackward), "Previous sentence", self, shortcut="Ctrl+Left",
                                triggered=lambda: self.narrator and self.narrator.prev_sentence())
        self.act_next = QAction(self._icon(S.SP_MediaSkipForward), "Next sentence", self, shortcut="Ctrl+Right",
                                triggered=lambda: self.narrator and self.narrator.next_sentence())
        self.act_goto = QAction("Go to reading position", self, shortcut="Ctrl+G", triggered=self._goto_current)
        self.act_zoom_in = QAction("Zoom in", self, shortcut="Ctrl+=", triggered=lambda: self.view.zoom_by(1.2))
        self.act_zoom_out = QAction("Zoom out", self, shortcut="Ctrl+-", triggered=lambda: self.view.zoom_by(1 / 1.2))
        self.act_fit = QAction("Fit width", self, shortcut="Ctrl+0", triggered=self.view.set_fit_width)
        self.act_edit = QAction("Edit reading order", self, checkable=True, triggered=self._toggle_edit)
        self.act_export = QAction("Export audio…", self, shortcut="Ctrl+E", triggered=self._export)
        self.act_options = QAction("Narration options…", self, triggered=self._options)
        for a in (self.act_open, self.act_play, self.act_stop, self.act_prev, self.act_next, self.act_goto,
                  self.act_zoom_in, self.act_zoom_out, self.act_fit, self.act_export):
            self.addAction(a)

    def _build_toolbar(self) -> None:
        tb = QToolBar("Main", self)
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(tb)
        tb.addAction(self.act_open)
        tb.addSeparator()
        tb.addAction(self.act_play)
        tb.addAction(self.act_stop)
        tb.addAction(self.act_prev)
        tb.addAction(self.act_next)
        tb.addSeparator()
        tb.addWidget(QLabel(" Voice "))
        self.voice_box = QComboBox()
        for v in assets.installed_voices() or [assets.DEFAULT_VOICE]:
            self.voice_box.addItem(assets.VOICES.get(v, v), v)
        idx = self.voice_box.findData(self.settings.voice)
        self.voice_box.setCurrentIndex(max(0, idx))
        self.voice_box.currentIndexChanged.connect(self._voice_changed)
        self.voice_box.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        tb.addWidget(self.voice_box)
        tb.addWidget(QLabel("  Speed "))
        self.speed = QSlider(Qt.Orientation.Horizontal)
        self.speed.setRange(50, 200)
        self.speed.setSingleStep(5)
        self.speed.setPageStep(10)
        self.speed.setFixedWidth(130)
        self.speed.setValue(int(round(self.settings.speed * 100)))
        self.speed.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.speed_label = QLabel(f"{self.settings.speed:.2f}×")
        self.speed_label.setMinimumWidth(42)
        self.speed.valueChanged.connect(lambda v: self.speed_label.setText(f"{v / 100:.2f}×"))
        self.speed.sliderReleased.connect(self._speed_changed)
        self.speed.valueChanged.connect(lambda _: None if self.speed.isSliderDown() else self._speed_timer.start())
        self._speed_timer = QTimer(self, singleShot=True, interval=350, timeout=self._speed_changed)
        tb.addWidget(self.speed)
        tb.addWidget(self.speed_label)
        tb.addSeparator()
        self.follow = QCheckBox("Follow reading")
        self.follow.setToolTip("Scroll and turn pages automatically to keep the line being read visible.\n"
                               "Turn off to browse other pages while listening.")
        self.follow.setChecked(self.settings.follow)
        self.follow.toggled.connect(self._follow_toggled)
        self.follow.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        tb.addWidget(self.follow)
        tb.addAction(self.act_goto)
        tb.addSeparator()
        tb.addAction(self.act_zoom_out)
        self.zoom_label = QLabel("100%")
        self.zoom_label.setMinimumWidth(44)
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tb.addWidget(self.zoom_label)
        tb.addAction(self.act_zoom_in)
        tb.addAction(self.act_fit)
        tb.addSeparator()
        tb.addAction(self.act_edit)
        tb.addAction(self.act_export)

    def _build_menu(self) -> None:
        m = self.menuBar()
        f = m.addMenu("&File")
        f.addAction(self.act_open)
        self.recent_menu = f.addMenu("Open recent")
        self._refresh_recent()
        f.addSeparator()
        f.addAction(self.act_export)
        f.addSeparator()
        f.addAction(QAction("Exit", self, triggered=self.close))
        p = m.addMenu("&Playback")
        for a in (self.act_play, self.act_stop, self.act_prev, self.act_next, self.act_goto):
            p.addAction(a)
        v = m.addMenu("&View")
        for a in (self.act_zoom_in, self.act_zoom_out, self.act_fit):
            v.addAction(a)
        t = m.addMenu("&Tools")
        t.addAction(self.act_edit)
        t.addAction(self.act_options)
        h = m.addMenu("&Help")
        h.addAction(QAction("הוראות שימוש (Hebrew instructions)", self, triggered=lambda: HelpDialog(self).exec()))
        h.addAction(QAction("Speech device details", self, triggered=self._device_details))

    def _build_status(self) -> None:
        sb = self.statusBar()
        self.device_label = QLabel("Speech engine: starting…")
        self.device_label.setStyleSheet("padding:0 8px;")
        self.state_label = QLabel("")
        self.page_label = QLabel("")
        sb.addPermanentWidget(self.state_label)
        sb.addPermanentWidget(self.page_label)
        sb.addPermanentWidget(self.device_label)

    def _build_edit_dock(self) -> None:
        dock = QDockWidget("Reading order", self)
        dock.setAllowedAreas(Qt.DockWidgetArea.RightDockWidgetArea | Qt.DockWidgetArea.LeftDockWidgetArea)
        w = QWidget()
        lay = QVBoxLayout(w)
        self.edit_title = QLabel("")
        self.edit_title.setStyleSheet("font-weight:bold;")
        lay.addWidget(self.edit_title)
        row = QHBoxLayout()
        row.addWidget(QLabel("Page layout:"))
        self.layout_box = QComboBox()
        self.layout_box.addItem("Automatic", "auto")
        self.layout_box.addItem("One column", "single")
        self.layout_box.addItem("Two columns", "double")
        self.layout_box.activated.connect(self._layout_mode_changed)
        row.addWidget(self.layout_box, 1)
        lay.addLayout(row)
        self.block_list = QListWidget()
        self.block_list.setWordWrap(True)
        self.block_list.currentRowChanged.connect(self._block_selected)
        lay.addWidget(self.block_list, 1)
        btns = QHBoxLayout()
        self.btn_up = QPushButton("Move earlier")
        self.btn_down = QPushButton("Move later")
        self.btn_toggle = QPushButton("Exclude / include")
        self.btn_up.clicked.connect(lambda: self._move_block(-1))
        self.btn_down.clicked.connect(lambda: self._move_block(+1))
        self.btn_toggle.clicked.connect(self._toggle_block)
        for b in (self.btn_up, self.btn_down, self.btn_toggle):
            btns.addWidget(b)
        lay.addLayout(btns)
        reset = QPushButton("Reset this page")
        reset.clicked.connect(self._reset_page)
        lay.addWidget(reset)
        hint = QLabel("Numbers on the page show the reading order of text blocks.<br>"
                      "<span style='color:#1b5e20'>Green</span>: read aloud. "
                      "<span style='color:#b71c1c'>Red</span>: excluded by you. "
                      "<span style='color:#616161'>Grey</span>: skipped automatically "
                      "(headers, page numbers, footnotes, references, equations, figure text — see Narration options)."
                      "<br><br><b>Click</b> a block to select it. <b>Drag a rectangle</b> on the page to exclude "
                      "(or restore) everything inside it, for example a table or an equation.")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        dock.setWidget(w)
        dock.hide()
        dock.visibilityChanged.connect(lambda vis: (not vis and self.act_edit.isChecked()) and self._set_edit(False))
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        self.edit_dock = dock

    # ---- engine startup ---------------------------------------------------------------------------------------
    def _start_engine(self) -> None:
        bridge = self._bridge
        bridge.done.connect(self._engine_ready)
        bridge.failed.connect(self._engine_failed)
        prefer = self.settings.prefer_gpu
        self.device_label.setText("Speech engine: starting (checking the GPU)…")

        def work():
            try:
                from ..assets import MODEL_FILE
                bridge.done.emit(KokoroEngine(MODEL_FILE, VOICES_DIR, prefer_gpu=prefer))
            except Exception as e:  # noqa: BLE001
                log.exception("engine start failed")
                bridge.failed.emit(str(e))
        threading.Thread(target=work, name="engine-start", daemon=True).start()

    def _engine_ready(self, engine: KokoroEngine) -> None:
        self.engine = engine
        self.cache = AudioCache(CACHE_DIR, self.settings.cache_limit_mb)
        self.narrator = Narrator(engine, self.cache)
        self.narrator.voice = self.voice_box.currentData()
        self.narrator.speed = self.speed.value() / 100
        self.narrator.lineChanged.connect(self._on_line)
        self.narrator.stateChanged.connect(self._on_state)
        self.narrator.message.connect(lambda m: self.statusBar().showMessage(m, 6000))
        self.narrator.error.connect(lambda m: self.statusBar().showMessage(m, 10000))
        dev_bridge = Bridge()
        dev_bridge.done.connect(lambda _: self._device_changed())
        self._dev_bridge = dev_bridge
        engine.on_device_change = lambda e: dev_bridge.done.emit(None)
        self._update_device_label()
        if self.layout_ is not None:
            self._push_segments(initial=True)
        self._set_controls_enabled(self.layout_ is not None)
        if engine.device == "cpu" and self.settings.prefer_gpu and engine.fallback_reason:
            info(self, "Using the processor (CPU) for speech",
                 "The NVIDIA graphics card could not be used for speech, so the application is using the main "
                 "processor instead. Everything works, but speech is prepared more slowly.",
                 engine.fallback_reason)

    def _engine_failed(self, msg: str) -> None:
        self.device_label.setText("Speech engine: failed")
        QMessageBox.critical(self, "Speech engine could not start",
                             "The speech engine could not start, so documents can be viewed but not read aloud.\n\n"
                             f"Details: {msg}")

    def _device_changed(self) -> None:
        self._update_device_label()
        info(self, "Switched to CPU", "The graphics card stopped working for speech, so the application switched "
             "to the processor (CPU). Reading continues, a little slower.", self.engine.fallback_reason)

    def _update_device_label(self) -> None:
        e = self.engine
        if e.device == "cuda":
            self.device_label.setText(f"Speech device: GPU ({e.gpu.name}) ✓")
            self.device_label.setStyleSheet("padding:0 8px;color:#1b5e20;font-weight:bold;")
        else:
            self.device_label.setText("Speech device: CPU")
            self.device_label.setStyleSheet("padding:0 8px;color:#e65100;font-weight:bold;")
        self.device_label.setToolTip(e.device_details())

    def _device_details(self) -> None:
        if not self.engine:
            info(self, "Speech device", "The speech engine is still starting.")
            return
        cache = f"\nAudio cache: {self.cache.size_mb:.0f} MB of {self.settings.cache_limit_mb} MB" if self.cache else ""
        info(self, "Speech device", self.engine.device_details() + cache)

    # ---- documents ---------------------------------------------------------------------------------------------
    def _refresh_recent(self) -> None:
        self.recent_menu.clear()
        for p in self.settings.recent[:10]:
            self.recent_menu.addAction(QAction(p, self, triggered=lambda _=False, path=p: self.open_pdf(path)))
        self.recent_menu.setEnabled(bool(self.settings.recent))

    def choose_pdf(self) -> None:
        start = self.settings.last_dir or str(Path.home() / "Documents")
        path, _ = QFileDialog.getOpenFileName(self, "Open PDF", start, "PDF files (*.pdf)")
        if path:
            self.open_pdf(path)

    def open_pdf(self, path: str) -> None:
        path = str(Path(path))
        if not Path(path).exists():
            QMessageBox.warning(self, "File not found", f"The file could not be found:\n{path}")
            return
        self._save_state()
        if self.narrator:
            self.narrator.stop()
        progress = QProgressDialog("Reading the PDF's text and layout…", "Cancel", 0, 100, self)
        progress.setWindowTitle("Opening PDF")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(400)
        bridge = Bridge()
        cancelled = threading.Event()
        progress.canceled.connect(cancelled.set)
        bridge.progress.connect(lambda d, t, _: progress.setValue(int(100 * d / max(1, t))))

        def done(layout):
            progress.close()
            self._doc_loaded(path, layout)

        def failed(msg):
            progress.close()
            if not cancelled.is_set():
                QMessageBox.warning(self, "Could not open PDF", f"This PDF could not be opened.\n\n{msg}")
        bridge.done.connect(done)
        bridge.failed.connect(failed)
        self._open_bridge = bridge

        def work():
            try:
                lay = load_or_extract(path, EXTRACT_CACHE_DIR, lambda d, t: bridge.progress.emit(d, t, ""),
                                      cancelled.is_set)
                bridge.done.emit(lay)
            except InterruptedError:
                bridge.failed.emit("cancelled")
            except Exception as e:  # noqa: BLE001
                log.exception("open failed")
                bridge.failed.emit(str(e))
        threading.Thread(target=work, name="pdf-open", daemon=True).start()

    def _doc_loaded(self, path: str, layout: DocLayout) -> None:
        self.layout_ = layout
        self.state = DocState.load(layout.doc_id)
        self.state.path = path
        self.settings.last_dir = str(Path(path).parent)
        self.settings.recent = [path] + [p for p in self.settings.recent if p != path][:9]
        self.settings.save()
        self._refresh_recent()
        self.setWindowTitle(f"{Path(path).name} — Kokoro PDF Reader")
        self.view.load(path, [(p.width, p.height) for p in layout.pages])
        self._rebuild_segments()
        self._push_segments(initial=True)
        self._set_controls_enabled(self.engine is not None)
        self.banner.hide()
        if layout.scanned:
            self.banner.setText("OCR required: this PDF has no usable text layer (its pages are images, for example "
                                "a scan). It can be viewed, but it cannot be read aloud. Text recognition (OCR) is "
                                "not included in this version.")
            self.banner.show()
            QMessageBox.warning(self, "OCR required",
                                "This PDF has no usable text layer — its pages are images (for example, a scanned "
                                "book).\n\nThe pages can be viewed, but there is no text to read aloud. Text "
                                "recognition (OCR) is needed first; it is not included in this version.\n\n"
                                "Tip: if you have a version of this document with selectable text, open that one.")
        elif layout.garbled:
            self.banner.setText("Warning: much of this PDF's text could not be decoded (unusual font encoding). "
                                "Narration may be wrong in places.")
            self.banner.show()
        else:
            empty = [p.index + 1 for p in layout.pages if p.no_text]
            if empty:
                shown = ", ".join(map(str, empty[:12])) + ("…" if len(empty) > 12 else "")
                self.statusBar().showMessage(f"Pages without a text layer (skipped, OCR required): {shown}", 15000)
        if not self.segments and not layout.scanned:
            self.now_reading.setText("No readable text was found with the current narration options.")

    def _rebuild_segments(self) -> None:
        lay, st = self.layout_, self.state
        self.segments = build_segments(lay, st, self.settings.options)
        self.reading_order = [lid for p in range(lay.page_count) for lid in page_order(lay, st, p)]

    def _push_segments(self, initial: bool = False) -> None:
        if not self.narrator or self.layout_ is None:
            return
        if initial:
            self.narrator.set_segments(self.segments, self.reading_order)
            last = self.state.last_line
            if last is not None and last < len(self.layout_.lines):
                self.narrator.seek_line(last)
                self._show_line(self.narrator.current_line, force_scroll=True)
                pg = self.layout_.lines[last].page + 1
                self.statusBar().showMessage(f"Continuing from where you stopped (page {pg}). Press Play.", 8000)
            elif self.segments:
                self._show_line(self.segments[0].first_line, force_scroll=False)
        else:
            self.narrator.replace_segments(self.segments, self.reading_order)
            if self.narrator.current_line is not None:
                self._show_line(self.narrator.current_line, force_scroll=False)

    # ---- playback ------------------------------------------------------------------------------------------------
    def _set_controls_enabled(self, on: bool) -> None:
        for a in (self.act_play, self.act_stop, self.act_prev, self.act_next, self.act_export, self.act_goto):
            a.setEnabled(on)

    def _play_pause(self) -> None:
        if self.narrator and self.segments:
            self.narrator.toggle()

    def _voice_changed(self) -> None:
        v = self.voice_box.currentData()
        self.settings.voice = v
        if self.narrator:
            self.narrator.set_voice(v)

    def _speed_changed(self) -> None:
        s = self.speed.value() / 100
        self.settings.speed = s
        if self.narrator:
            self.narrator.set_speed(s)

    def _follow_toggled(self, on: bool) -> None:
        self.settings.follow = on
        if on:
            self._goto_current()

    def _goto_current(self) -> None:
        if self.narrator and self.narrator.current_line is not None:
            self._show_line(self.narrator.current_line, force_scroll=True)

    def _on_state(self, state: str) -> None:
        S = QStyle.StandardPixmap
        playing = state in ("playing", "buffering")
        self.act_play.setText("Pause" if playing else ("Resume" if state == "paused" else "Play"))
        self.act_play.setIcon(self._icon(S.SP_MediaPause if playing else S.SP_MediaPlay))
        self.state_label.setText(STATE_TEXT.get(state, state))

    def _on_line(self, lid: int) -> None:
        self._show_line(lid, force_scroll=False)
        if self.state is not None:
            self.state.last_line = lid
            self._state_dirty = True

    def _show_line(self, lid: int | None, force_scroll: bool) -> None:
        if lid is None or self.layout_ is None:
            return
        line = self.layout_.lines[lid]
        self.view.set_highlight(line.page, [line.bbox])
        if force_scroll or self.follow.isChecked():
            self.view.ensure_visible(line.page, line.bbox)
        if self.narrator and self.segments and 0 <= self.narrator.current_seg < len(self.segments):
            text = self.segments[self.narrator.current_seg].text
            fm = QFontMetrics(self.now_reading.font())
            self.now_reading.setText(fm.elidedText(text, Qt.TextElideMode.ElideRight, max(200, self.width() - 40)))
            self.now_reading.setToolTip(text)

    def _on_page_changed(self, page: int) -> None:
        if self.layout_:
            p = self.layout_.pages[page]
            label = f" (printed {p.label})" if p.label and p.label != str(page + 1) else ""
            extra = " — no text layer, OCR required" if p.no_text else ""
            self.page_label.setText(f"Page {page + 1} of {self.layout_.page_count}{label}{extra}")
            if self.act_edit.isChecked():
                self._edit_page = page
                self._refresh_edit()

    def _hit_line(self, page: int, x: float, y: float, prefer_narrated: bool = True) -> int | None:
        lay = self.layout_
        best, best_d = None, 1e9
        for lid in lay.pages[page].line_ids:
            l = lay.lines[lid]
            x0, y0, x1, y1 = l.bbox
            dx = max(x0 - x, 0, x - x1)
            dy = max(y0 - y, 0, y - y1)
            d = dx * 3 + dy * 4
            if prefer_narrated and not line_included(l, self.state, self.settings.options):
                d += 6
            if d < best_d:
                best, best_d = lid, d
        return best if best_d < 60 else None

    def _on_page_click(self, page: int, x: float, y: float) -> None:
        if self.layout_ is None:
            return
        if self.act_edit.isChecked():
            lid = self._hit_line(page, x, y, prefer_narrated=False)
            self._edit_page = page
            self._selected_block = self.layout_.lines[lid].block if lid is not None else None
            self._refresh_edit()
            return
        lid = self._hit_line(page, x, y)
        if lid is None or not self.narrator:
            return
        if not line_included(self.layout_.lines[lid], self.state, self.settings.options):
            self.statusBar().showMessage("That line is not read aloud (see Narration options or Edit reading "
                                         "order). Starting from the next line that is.", 6000)
        self.narrator.play_line(lid)

    # ---- reading order editing ------------------------------------------------------------------------------------
    def _toggle_edit(self, on: bool) -> None:
        self._set_edit(on)

    def _set_edit(self, on: bool) -> None:
        self.act_edit.setChecked(on)
        self.view.edit_mode = on
        if on:
            vis = self.view.visible_pages()
            self._edit_page = vis[0] if vis else 0
            self.edit_dock.show()
            self._refresh_edit()
        else:
            self.edit_dock.hide()
            self.view.set_overlays([])

    def _refresh_edit(self) -> None:
        if not self.act_edit.isChecked() or self.layout_ is None:
            return
        lay, st, opts = self.layout_, self.state, self.settings.options
        page = self._edit_page
        self.edit_title.setText(f"Page {page + 1}: blocks in reading order")
        mode = st.page_mode.get(page, "auto")
        self.layout_box.setCurrentIndex(max(0, self.layout_box.findData(mode)))
        auto_cols = lay.pages[page].columns
        self.layout_box.setItemText(0, f"Automatic (detected {auto_cols} column{'s' if auto_cols > 1 else ''})")
        blocks = page_blocks(lay, st, page)
        by_block: dict[int, list] = {}
        for lid in page_order(lay, st, page):
            by_block.setdefault(lay.lines[lid].block, []).append(lay.lines[lid])
        overlays = []
        self.block_list.blockSignals(True)
        self.block_list.clear()
        for k, b in enumerate(blocks):
            lines = by_block[b]
            for l in lines:
                if l.id in st.excluded:
                    fill, edge = QColor(229, 57, 53, 70), QColor(183, 28, 28)
                elif line_included(l, st, opts):
                    fill, edge = QColor(67, 160, 71, 40), QColor(46, 125, 50)
                else:
                    fill, edge = QColor(120, 120, 120, 60), QColor(97, 97, 97)
                overlays.append((page, l.bbox, fill, edge, ""))
            x0 = min(l.x0 for l in lines)
            y0 = min(l.y0 for l in lines)
            x1 = max(l.x1 for l in lines)
            y1 = max(l.y1 for l in lines)
            selected = b == self._selected_block
            edge = QColor(25, 118, 210) if not selected else QColor(255, 111, 0)
            overlays.append((page, (x0, y0, x1, y1), QColor(0, 0, 0, 0), edge, str(k + 1)))
            narrated = [l for l in lines if line_included(l, st, opts)]
            roles = {ROLE_LABELS.get(l.role, l.role) for l in lines}
            status = "read" if len(narrated) == len(lines) else ("not read" if not narrated else "partly read")
            text = " ".join(l.text for l in lines)
            item = QListWidgetItem(f"{k + 1}. [{', '.join(sorted(roles))}; {status}] {text[:140]}")
            item.setData(Qt.ItemDataRole.UserRole, b)
            if not narrated:
                item.setForeground(QColor(140, 140, 140))
            self.block_list.addItem(item)
            if selected:
                self.block_list.setCurrentItem(item)
        self.block_list.blockSignals(False)
        self.view.set_overlays(overlays)

    def _block_selected(self, row: int) -> None:
        item = self.block_list.item(row)
        if item is not None:
            self._selected_block = item.data(Qt.ItemDataRole.UserRole)
            self._refresh_edit()

    def _edited(self) -> None:
        self.state.save()
        self._rebuild_segments()
        self._push_segments()
        self._refresh_edit()

    def _move_block(self, delta: int) -> None:
        if self._selected_block is None:
            return
        page = self._edit_page
        blocks = page_blocks(self.layout_, self.state, page)
        if self._selected_block not in blocks:
            return
        i = blocks.index(self._selected_block)
        j = i + delta
        if not 0 <= j < len(blocks):
            return
        blocks[i], blocks[j] = blocks[j], blocks[i]
        self.state.block_order[page] = blocks
        self._edited()

    def _toggle_lines(self, lines) -> None:
        st, opts = self.state, self.settings.options
        if any(line_included(l, st, opts) for l in lines):
            for l in lines:
                st.excluded.add(l.id)
                st.included.discard(l.id)
        else:
            for l in lines:
                st.excluded.discard(l.id)
                st.included.add(l.id)
        self._edited()

    def _toggle_block(self) -> None:
        if self._selected_block is None:
            return
        lay = self.layout_
        lines = [lay.lines[i] for i in lay.pages[self._edit_page].line_ids
                 if lay.lines[i].block == self._selected_block]
        if lines:
            self._toggle_lines(lines)

    def _on_region(self, page: int, rect: QRectF) -> None:
        lay = self.layout_
        lines = []
        for lid in lay.pages[page].line_ids:
            l = lay.lines[lid]
            if rect.contains(l.cx, l.cy):
                lines.append(l)
        self._edit_page = page
        if lines:
            self._toggle_lines(lines)

    def _layout_mode_changed(self, _idx: int) -> None:
        mode = self.layout_box.currentData()
        if mode == "auto":
            self.state.page_mode.pop(self._edit_page, None)
        else:
            self.state.page_mode[self._edit_page] = mode
        self.state.block_order.pop(self._edit_page, None)
        self._edited()

    def _reset_page(self) -> None:
        page = self._edit_page
        st = self.state
        st.page_mode.pop(page, None)
        st.block_order.pop(page, None)
        ids = set(self.layout_.pages[page].line_ids)
        st.excluded -= ids
        st.included -= ids
        self._edited()

    # ---- options / export ---------------------------------------------------------------------------------------
    def _options(self) -> None:
        dlg = OptionsDialog(self.settings, self.cache.size_mb if self.cache else 0, self)
        if dlg.exec():
            dlg.apply(self.settings)
            self.settings.save()
            if self.cache:
                self.cache.set_limit(self.settings.cache_limit_mb)
                if dlg.clear_requested:
                    self.cache.clear()
                    self.statusBar().showMessage("Audio cache cleared.", 5000)
            if self.layout_ is not None:
                self._rebuild_segments()
                self._push_segments()
                self._refresh_edit()

    def _export(self) -> None:
        if not (self.layout_ and self.engine and self.narrator):
            return
        if self._export_token is not None:
            QMessageBox.information(self, "Export running", "An export is already running.")
            return
        current = self.view.visible_pages()[0] if self.view.visible_pages() else 0
        dlg = ExportDialog(self.layout_.page_count, current, Path(self.state.path).stem,
                           self.settings.last_dir or str(Path.home()), self)
        if not dlg.exec():
            return
        pages = dlg.pages()
        segs = build_segments(self.layout_, self.state, self.settings.options, pages)
        if not segs:
            QMessageBox.information(self, "Nothing to export", "There is no narrated text on those pages.")
            return
        voice, speed, path = self.narrator.voice, self.narrator.speed, dlg.path
        token = CancelToken()
        self._export_token = token
        progress = QProgressDialog(f"Exporting {len(segs)} sentences to {Path(path).name}…", "Cancel", 0, len(segs), self)
        progress.setWindowTitle("Exporting audio")
        progress.setWindowModality(Qt.WindowModality.NonModal)
        progress.setMinimumDuration(0)
        progress.canceled.connect(token.cancel)
        bridge = Bridge()
        bridge.progress.connect(lambda d, t, _: progress.setValue(d))
        started = time.perf_counter()

        def done(seconds):
            progress.close()
            self._export_token = None
            m, s = divmod(int(seconds), 60)
            box = QMessageBox(QMessageBox.Icon.Information, "Export finished",
                              f"Saved {m} min {s} s of audio to:\n{path}\n\n"
                              f"(took {time.perf_counter() - started:.0f} s)", QMessageBox.StandardButton.Ok, self)
            folder = box.addButton("Show in folder", QMessageBox.ButtonRole.ActionRole)
            box.exec()
            if box.clickedButton() is folder:
                open_folder(path)

        def failed(msg):
            progress.close()
            self._export_token = None
            if msg != "cancelled":
                QMessageBox.warning(self, "Export failed", msg)
            else:
                self.statusBar().showMessage("Export cancelled.", 5000)
        bridge.done.connect(done)
        bridge.failed.connect(failed)
        self._export_bridge = bridge

        def work():
            try:
                secs = export_audio(self.engine, self.cache, segs, path, voice, speed,
                                    lambda d, t: bridge.progress.emit(d, t, ""), token)
                bridge.done.emit(secs)
            except Cancelled:
                bridge.failed.emit("cancelled")
            except Exception as e:  # noqa: BLE001
                log.exception("export failed")
                bridge.failed.emit(str(e))
        threading.Thread(target=work, name="export", daemon=True).start()

    # ---- persistence ------------------------------------------------------------------------------------------------
    def _save_state(self) -> None:
        if self.state is not None and self._state_dirty:
            self.state.save()
            self._state_dirty = False

    def closeEvent(self, event) -> None:
        self._state_dirty = True
        self._save_state()
        self.settings.zoom = 0.0 if self.view.fit_width else self.view.zoom
        self.settings.save()
        if self._export_token:
            self._export_token.cancel()
        if self.narrator:
            self.narrator.shutdown()
        self.view.close_document()
        super().closeEvent(event)


def app_instance() -> QApplication:
    return QApplication.instance()
