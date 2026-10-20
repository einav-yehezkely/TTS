"""Main window: transcript panel, PDF viewer, player bar and reading-order editor."""

import html
import logging
import threading
import time
from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QColor, QKeySequence
from PySide6.QtWidgets import (QApplication, QComboBox, QDockWidget, QFileDialog, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QMainWindow, QMenu, QMessageBox, QProgressDialog, QPushButton,
                               QSizePolicy, QSlider, QToolBar, QToolButton, QVBoxLayout, QWidget, QWidgetAction)

from .. import assets
from ..audiocache import AudioCache
from ..docstate import DocState, Settings
from ..engine import Cancelled, CancelToken, KokoroEngine
from ..export import export_audio
from ..layout import ROLE_LABELS, DocLayout, load_or_extract
from ..narrator import Narrator
from ..paths import CACHE_DIR, EXTRACT_CACHE_DIR, VOICES_DIR
from ..segments import build_segments, line_included, page_blocks, page_order
from ..video import ENCODER_LABELS, export_video
from . import icons, theme
from .dialogs import Bridge, ExportDialog, HelpDialog, OptionsDialog, info, open_folder
from .transcript import Rail, SideSplitter, TranscriptPanel
from .viewer import PdfView
from .widgets import PlayButton, Scrubber, Toast, fraction_to_index

log = logging.getLogger(__name__)

STATE_TEXT = {"stopped": "Stopped", "playing": "Playing", "paused": "Paused",
              "buffering": "Preparing speech…"}
SPEEDS = (0.75, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0)


def _fmt_duration(seconds: float) -> str:
    m = int(round(seconds / 60))
    if m < 1:
        return "<1 min"
    if m < 60:
        return f"{m} min"
    return f"{m // 60} h {m % 60:02d} min"


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings
        self.setWindowTitle("Kokoro PDF Reader")
        self.resize(1320, 920)
        self.engine: KokoroEngine | None = None
        self.narrator: Narrator | None = None
        self.cache: AudioCache | None = None
        self.layout_: DocLayout | None = None
        self.state: DocState | None = None
        self.segments = []
        self.reading_order: list[int] = []
        self._cum: list[int] = [0]
        self._state_dirty = False
        self._export_token: CancelToken | None = None
        self._selected_block: int | None = None
        self._edit_page = 0
        self._sleep_deadline: float | None = None
        self._sleep_page: int | None = None
        self._icon_targets: list[tuple[object, str, str]] = []
        self._bridge = Bridge()
        theme.apply(QApplication.instance(), settings.theme)

        self.view = PdfView(self)
        self._build_actions()
        self._build_topbar()
        self.setCentralWidget(self._build_central())
        self._build_edit_dock()
        self.statusBar().hide()  # messages appear as toasts; the status bar keeps them for tests and logs
        self.view.lineClicked.connect(self._on_page_click)
        self.view.regionSelected.connect(self._on_region)
        self.view.currentPageChanged.connect(self._on_page_changed)
        self.view.zoomChanged.connect(lambda z: self.zoom_label.setText(f"{z * 100:.0f}%"))
        self.view.set_spotlight(settings.spotlight)
        if settings.zoom:
            self.view.set_zoom(settings.zoom)
        if not settings.transcript_open:
            self.splitter.set_collapsed(True, animate=False)
        self._save_timer = QTimer(self, interval=3000, timeout=self._save_state)
        self._save_timer.start()
        self._clock = QTimer(self, interval=1000, timeout=self._tick_clock)
        self._clock.start()
        self._apply_icons()
        self._set_controls_enabled(False)
        self._start_engine()

    # ---- actions -----------------------------------------------------------------------------------------
    def _act(self, text, icon=None, shortcut=None, slot=None, checkable=False, tip=None) -> QAction:
        a = QAction(text, self)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        if slot:
            a.triggered.connect(slot)
        a.setCheckable(checkable)
        a.setToolTip(tip or (f"{text} ({QKeySequence(shortcut).toString()})" if shortcut else text))
        if icon:
            self._icon_targets.append((a, icon, "text"))
        self.addAction(a)
        return a

    def _build_actions(self) -> None:
        self.act_open = self._act("Open", "open", QKeySequence.StandardKey.Open, self.choose_pdf, tip="Open a PDF (Ctrl+O)")
        self.act_play = self._act("Play", "play", "Space", self._play_pause)
        self.act_stop = self._act("Stop", "stop", "Esc", lambda: self.narrator and self.narrator.stop())
        self.act_prev = self._act("Previous sentence", "prev", "Ctrl+Left",
                                  lambda: self.narrator and self.narrator.prev_sentence())
        self.act_next = self._act("Next sentence", "next", "Ctrl+Right",
                                  lambda: self.narrator and self.narrator.next_sentence())
        self.act_goto = self._act("Go to reading position", None, "Ctrl+G", self._goto_current)
        self.act_zoom_in = self._act("Zoom in", "zoom_in", "Ctrl+=", lambda: self.view.zoom_by(1.2))
        self.act_zoom_out = self._act("Zoom out", "zoom_out", "Ctrl+-", lambda: self.view.zoom_by(1 / 1.2))
        self.act_fit = self._act("Fit width", "fit", "Ctrl+0", self.view.set_fit_width)
        self.act_transcript = self._act("Transcript", "transcript", "Ctrl+T", self._toggle_transcript, checkable=True,
                                        tip="Show or hide the transcript (Ctrl+T)")
        self.act_transcript.setChecked(self.settings.transcript_open)
        self.act_focus = self._act("Focus", "focus", "F", self._toggle_focus, checkable=True,
                                   tip="Focus mode: dim everything except the line being read (F)")
        self.act_focus.setChecked(self.settings.spotlight)
        self.act_edit = self._act("Reading order", "edit", None, self._toggle_edit, checkable=True,
                                  tip="Check and correct the reading order, or exclude parts of a page")
        self.act_export = self._act("Export", "export", "Ctrl+E", self._export,
                                    tip="Save as audio (WAV, MP3) or video (MP4) (Ctrl+E)")
        self.act_options = self._act("Narration options…", None, None, self._options)
        self.act_theme = self._act("Theme", "sun", "Ctrl+Shift+L", self._toggle_theme,
                                   tip="Switch between dark and light (Ctrl+Shift+L)")
        self.act_search = self._act("Search transcript", None, "Ctrl+F", self._focus_search)

    # ---- top bar ----------------------------------------------------------------------------------------------
    def _build_topbar(self) -> None:
        tb = QToolBar("Main", self)
        tb.setObjectName("TopBar")
        tb.setMovable(False)
        tb.setFloatable(False)
        tb.setIconSize(QSize(18, 18))
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        tb.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)
        self.addToolBar(tb)
        tb.addAction(self.act_open)
        title_box = QWidget()
        tl = QVBoxLayout(title_box)
        tl.setContentsMargins(12, 0, 12, 0)
        tl.setSpacing(0)
        self.doc_title = QLabel("No document open", objectName="DocTitle")
        self.doc_sub = QLabel("Open a PDF to start reading", objectName="DocSub")
        tl.addWidget(self.doc_title)
        tl.addWidget(self.doc_sub)
        tb.addWidget(title_box)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        for a in (self.act_zoom_out,):
            tb.addAction(a)
        self.zoom_label = QLabel("100%", objectName="ZoomLabel")
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tb.addWidget(self.zoom_label)
        tb.addAction(self.act_zoom_in)
        tb.addAction(self.act_fit)
        tb.addSeparator()
        for a in (self.act_transcript, self.act_focus, self.act_edit, self.act_export):
            tb.addAction(a)
        for a in (self.act_zoom_out, self.act_zoom_in, self.act_fit):
            btn = tb.widgetForAction(a)
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        tb.addSeparator()
        tb.addAction(self.act_theme)
        tb.widgetForAction(self.act_theme).setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.device_label = QLabel("Starting…", objectName="Badge")
        self.device_label.setToolTip("The speech engine is starting")
        tb.addWidget(self.device_label)
        self.menu_btn = QToolButton(toolTip="More")
        self.menu_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.menu_btn.setText("⋯")
        self.menu_btn.setStyleSheet("font-size: 16pt; padding: 0 8px;")
        self.menu_btn.setMenu(self._build_menu())
        tb.addWidget(self.menu_btn)
        self._set_badge("Starting…", "muted")

    def _build_menu(self) -> QMenu:
        m = QMenu(self)
        m.addAction(self.act_open)
        self.recent_menu = m.addMenu("Open recent")
        self._refresh_recent()
        m.addAction(self.act_export)
        m.addSeparator()
        for a in (self.act_play, self.act_stop, self.act_prev, self.act_next, self.act_goto):
            m.addAction(a)
        m.addSeparator()
        m.addAction(self.act_options)
        m.addAction(self.act_edit)
        m.addAction(self.act_search)
        m.addSeparator()
        m.addAction(QAction("הוראות שימוש (Hebrew instructions)", self, triggered=lambda: HelpDialog(self).exec()))
        m.addAction(QAction("Speech device details", self, triggered=self._device_details))
        m.addSeparator()
        m.addAction(QAction("Exit", self, triggered=self.close))
        return m

    def _set_badge(self, text: str, token: str) -> None:
        c = theme.color(token)
        bg = QColor(c)
        bg.setAlpha(40)
        self.device_label.setText(text)
        self.device_label.setStyleSheet(
            f"background: rgba({bg.red()},{bg.green()},{bg.blue()},{bg.alpha()}); color: {c.name()};")
        self._badge_token = token

    # ---- central area -----------------------------------------------------------------------------------------
    def _build_central(self) -> QWidget:
        central = QWidget(objectName="Central")
        lay = QVBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.banner = QLabel("", objectName="Banner")
        self.banner.setWordWrap(True)
        self.banner.hide()
        lay.addWidget(self.banner)

        self.reading_area = QWidget()
        ra = QVBoxLayout(self.reading_area)
        ra.setContentsMargins(0, 0, 0, 0)
        ra.addWidget(self.view)
        self.toast = Toast(self.reading_area)

        self.transcript = TranscriptPanel()
        self.transcript.activated.connect(self._transcript_activated)
        self.transcript.collapseRequested.connect(lambda: self._set_transcript(False))
        self.rail = Rail()
        self.rail.clicked.connect(lambda: self._set_transcript(True))
        self.splitter = SideSplitter(self.transcript, self.rail, self.reading_area, self.settings.transcript_width)
        lay.addWidget(self.splitter, 1)
        lay.addWidget(self._build_player())
        return central

    def _build_player(self) -> QWidget:
        bar = QWidget(objectName="PlayerBar")
        bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        v = QVBoxLayout(bar)
        v.setContentsMargins(18, 10, 18, 12)
        v.setSpacing(6)
        top = QHBoxLayout()
        self.caption = QLabel("", objectName="Caption")
        self.caption.setWordWrap(True)
        self.caption.setTextFormat(Qt.TextFormat.RichText)
        self.caption.setMinimumHeight(22)
        self.caption.setMaximumHeight(46)
        self.caption.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.state_label = QLabel("", objectName="CaptionHint")
        top.addWidget(self.caption, 1)
        top.addWidget(self.state_label, 0, Qt.AlignmentFlag.AlignTop)
        v.addLayout(top)

        row = QHBoxLayout()
        row.setSpacing(6)

        def tbtn(action, size=20):
            b = QToolButton()
            b.setDefaultAction(action)
            b.setIconSize(QSize(size, size))
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            b.setAutoRaise(True)
            return b
        row.addWidget(tbtn(self.act_prev))
        self.play_btn = PlayButton()
        self.play_btn.clicked.connect(self._play_pause)
        row.addWidget(self.play_btn)
        row.addWidget(tbtn(self.act_next))
        row.addWidget(tbtn(self.act_stop, 16))
        row.addSpacing(8)
        self.scrubber = Scrubber()
        self.scrubber.seekRequested.connect(self._seek_fraction)
        self.scrubber.describe = self._describe_fraction
        row.addWidget(self.scrubber, 1)
        self.page_label = QLabel("", objectName="Meta")
        self.page_label.setMinimumWidth(150)
        self.page_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.page_label)
        row.addSpacing(8)

        self.voice_box = QComboBox(objectName="Chip")
        for vname in assets.installed_voices() or [assets.DEFAULT_VOICE]:
            self.voice_box.addItem(assets.VOICES.get(vname, vname), vname)
        self.voice_box.setCurrentIndex(max(0, self.voice_box.findData(self.settings.voice)))
        self.voice_box.currentIndexChanged.connect(self._voice_changed)
        self.voice_box.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.voice_box.setToolTip("Voice")
        row.addWidget(self.voice_box)

        self.speed_btn = QToolButton(objectName="Chip")
        self.speed_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.speed_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.speed_btn.setToolTip("Reading speed")
        self._icon_targets.append((self.speed_btn, "speed", "muted"))
        menu = QMenu(self.speed_btn)
        group = QActionGroup(menu)
        self._speed_actions = {}
        for s in SPEEDS:
            a = menu.addAction(f"{s:.2f}×".replace(".00×", ".0×"))
            a.setCheckable(True)
            group.addAction(a)
            a.triggered.connect(lambda _=False, s=s: self.speed.setValue(int(round(s * 100))))
            self._speed_actions[s] = a
        menu.addSeparator()
        slider_box = QWidget()
        sl = QHBoxLayout(slider_box)
        sl.setContentsMargins(12, 4, 12, 6)
        self.speed = QSlider(Qt.Orientation.Horizontal)
        self.speed.setRange(50, 200)
        self.speed.setSingleStep(5)
        self.speed.setPageStep(10)
        self.speed.setFixedWidth(170)
        self.speed.setValue(int(round(self.settings.speed * 100)))
        sl.addWidget(QLabel("0.5×"))
        sl.addWidget(self.speed)
        sl.addWidget(QLabel("2×"))
        wa = QWidgetAction(menu)
        wa.setDefaultWidget(slider_box)
        menu.addAction(wa)
        self.speed_btn.setMenu(menu)
        self.speed.valueChanged.connect(self._speed_display)
        self.speed.valueChanged.connect(lambda _: None if self.speed.isSliderDown() else self._speed_timer.start())
        self.speed.sliderReleased.connect(self._speed_changed)
        self._speed_timer = QTimer(self, singleShot=True, interval=350, timeout=self._speed_changed)
        self._speed_display(self.speed.value())
        row.addWidget(self.speed_btn)

        self.sleep_btn = QToolButton(objectName="Chip")
        self.sleep_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.sleep_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.sleep_btn.setText("Sleep")
        self.sleep_btn.setToolTip("Sleep timer: pause reading automatically")
        self._icon_targets.append((self.sleep_btn, "timer", "muted"))
        sm = QMenu(self.sleep_btn)
        sm.addAction("Off", lambda: self._set_sleep(None))
        for mins in (5, 15, 30, 45, 60, 90):
            sm.addAction(f"In {mins} minutes", lambda m=mins: self._set_sleep(m * 60))
        sm.addAction("At the end of this page", lambda: self._set_sleep("page"))
        self.sleep_btn.setMenu(sm)
        row.addWidget(self.sleep_btn)

        self.follow = QToolButton(objectName="Chip")
        self.follow.setCheckable(True)
        self.follow.setText("Follow")
        self.follow.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.follow.setToolTip("Scroll and turn pages automatically to keep the line being read visible.\n"
                               "Turn off to browse other pages while listening.")
        self.follow.setChecked(self.settings.follow)
        self.follow.toggled.connect(self._follow_toggled)
        self.follow.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._icon_targets.append((self.follow, "follow", "muted"))
        row.addWidget(self.follow)
        v.addLayout(row)
        self._set_caption(None)
        return bar

    def _apply_icons(self) -> None:
        for target, name, token in self._icon_targets:
            target.setIcon(icons.icon(name, token))
        if self.narrator and self.narrator.state in ("playing", "buffering"):
            self.act_play.setIcon(icons.icon("pause"))
        self.act_theme.setIcon(icons.icon("sun" if theme.current_name == "dark" else "moon"))
        self.voice_box.setIconSize(QSize(16, 16))
        for i in range(self.voice_box.count()):
            self.voice_box.setItemIcon(i, icons.icon("voice", "muted"))

    # ---- edit dock --------------------------------------------------------------------------------------------
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
                      "<span style='color:#43a047'>Green</span>: read aloud. "
                      "<span style='color:#e53935'>Red</span>: excluded by you. "
                      "<span style='color:#9e9e9e'>Grey</span>: skipped automatically "
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

    # ---- notifications ----------------------------------------------------------------------------------------
    def notify(self, text: str, ms: int = 5000) -> None:
        self.toast.show_message(text, ms)
        self.statusBar().showMessage(text, ms)

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        if self.toast.isVisible():
            self.toast.move((self.reading_area.width() - self.toast.width()) // 2, 18)

    # ---- engine startup ---------------------------------------------------------------------------------------
    def _start_engine(self) -> None:
        bridge = self._bridge
        bridge.done.connect(self._engine_ready)
        bridge.failed.connect(self._engine_failed)
        prefer = self.settings.prefer_gpu
        self._set_badge("Checking GPU…", "muted")

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
        # a new sentence can start on the line the previous one ended on: no line change, but a new sentence
        self.narrator.segmentChanged.connect(lambda _: self._show_line(self.narrator.current_line, False))
        self.narrator.stateChanged.connect(self._on_state)
        self.narrator.message.connect(lambda m: self.notify(m, 6000))
        self.narrator.error.connect(lambda m: self.notify(m, 10000))
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
        self._set_badge("Speech unavailable", "bad")
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
            name = e.gpu.name.replace("NVIDIA ", "").replace("GeForce ", "")
            self._set_badge(f"GPU · {name} ✓", "good")
        else:
            self._set_badge("CPU", "warn")
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
        self.doc_title.setText(Path(path).stem)
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
                self.notify(f"Pages without a text layer (skipped, OCR required): {shown}", 12000)
        if not self.segments and not layout.scanned:
            self._set_caption(None, "No readable text was found with the current narration options.")

    def _rebuild_segments(self) -> None:
        lay, st = self.layout_, self.state
        self.segments = build_segments(lay, st, self.settings.options)
        self.reading_order = [lid for p in range(lay.page_count) for lid in page_order(lay, st, p)]
        cum = [0]
        for s in self.segments:
            cum.append(cum[-1] + len(s.text) + 1)
        self._cum = cum
        ticks, last = [], None
        for s in self.segments:
            pg = lay.lines[s.first_line].page
            if pg != last:
                ticks.append(cum[s.index] / max(1, cum[-1]))
                last = pg
        self.scrubber.set_ticks(ticks)
        self.transcript.set_document(self.segments, lay)
        mins = _fmt_duration(cum[-1] / (14.5 * self.speed.value() / 100))
        self.doc_sub.setText(f"{lay.page_count} pages · {len(self.segments):,} sentences · about {mins} of listening")

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
                self.notify(f"Continuing from where you stopped (page {pg}). Press Play.", 8000)
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
        self.play_btn.setEnabled(on)
        self.scrubber.setEnabled(on)

    def _play_pause(self) -> None:
        if self.narrator and self.segments:
            self.narrator.toggle()

    def _voice_changed(self) -> None:
        v = self.voice_box.currentData()
        self.settings.voice = v
        if self.narrator:
            self.narrator.set_voice(v)
        self.notify(f"Voice: {self.voice_box.currentText()}", 2500)

    def _speed_display(self, v: int) -> None:
        s = v / 100
        self.speed_btn.setText(f"{s:.2f}×")
        for val, a in self._speed_actions.items():
            a.setChecked(abs(val - s) < 0.001)

    def _speed_changed(self) -> None:
        s = self.speed.value() / 100
        self.settings.speed = s
        if self.narrator:
            self.narrator.set_speed(s)
        self._update_meta()

    def _follow_toggled(self, on: bool) -> None:
        self.settings.follow = on
        if on:
            self._goto_current()

    def _goto_current(self) -> None:
        if self.narrator and self.narrator.current_line is not None:
            self._show_line(self.narrator.current_line, force_scroll=True)
            self.transcript.resume_following()

    def _on_state(self, state: str) -> None:
        playing = state in ("playing", "buffering")
        self.act_play.setText("Pause" if playing else ("Resume" if state == "paused" else "Play"))
        self.act_play.setIcon(icons.icon("pause" if playing else "play"))
        self.play_btn.set_mode("buffering" if state == "buffering" else ("pause" if playing else "play"))
        self.state_label.setText(STATE_TEXT.get(state, state) if state != "playing" else "")

    def _on_line(self, lid: int) -> None:
        self._show_line(lid, force_scroll=False)
        if self.state is not None:
            self.state.last_line = lid
            self._state_dirty = True
        if self._sleep_page is not None and self.layout_.lines[lid].page != self._sleep_page:
            self._sleep_fire()

    def _show_line(self, lid: int | None, force_scroll: bool) -> None:
        if lid is None or self.layout_ is None:
            return
        line = self.layout_.lines[lid]
        self.view.set_highlight(line.page, [line.bbox])
        if force_scroll or self.follow.isChecked():
            self.view.ensure_visible(line.page, line.bbox, animate=not force_scroll)
        if self.narrator and self.segments and 0 <= self.narrator.current_seg < len(self.segments):
            self.transcript.set_position(self.narrator.current_seg, lid)
            self._set_caption(self.narrator.current_seg, lid=lid)
        self._update_meta()

    def _set_caption(self, seg: int | None, hint: str = "", lid: int | None = None) -> None:
        if seg is None:
            text = hint or ("Click any line in the document — or any sentence in the transcript — to start reading "
                            "from there." if self.layout_ else "Open a PDF to start reading.")
            self.caption.setText(f"<span style='color:{theme.current['muted']}'>{html.escape(text)}</span>")
            return
        parts = []
        mark = theme.color("mark_soft")
        for flid, t in self.segments[seg].fragments:
            t = html.escape(t)
            if flid == lid:
                parts.append(f"<span style='background-color:rgba({mark.red()},{mark.green()},{mark.blue()},"
                             f"{mark.alpha()});'>{t}</span>")
            else:
                parts.append(t)
        self.caption.setText(" ".join(parts))

    def _update_meta(self) -> None:
        if not (self.layout_ and self.segments and self.narrator):
            self.page_label.setText("")
            return
        seg = max(0, min(self.narrator.current_seg, len(self.segments) - 1))
        total = self._cum[-1]
        frac = self._cum[seg] / max(1, total)
        self.scrubber.set_value(frac)
        rate = self.narrator.chars_per_second * self.narrator.speed
        left = _fmt_duration((total - self._cum[seg]) / max(1.0, rate))
        page = self.layout_.lines[self.segments[seg].first_line].page
        self.page_label.setText(f"Page {page + 1}/{self.layout_.page_count}  ·  {frac * 100:.0f}%  ·  {left} left")

    def _describe_fraction(self, f: float) -> str:
        if not self.segments or not self.layout_:
            return ""
        seg = self.segments[fraction_to_index(self._cum, f)]
        page = self.layout_.lines[seg.first_line].page + 1
        text = seg.text if len(seg.text) < 90 else seg.text[:88] + "…"
        return f"Page {page} of {self.layout_.page_count}  ·  {f * 100:.0f}%\n“{text}”"

    def _seek_fraction(self, f: float) -> None:
        if not (self.narrator and self.segments):
            return
        seg = fraction_to_index(self._cum, f)
        if self.narrator.state in ("playing", "buffering"):
            self.narrator.play(seg)
        else:
            self.narrator.seek_line(self.segments[seg].first_line)
            self._show_line(self.segments[seg].first_line, force_scroll=True)
        self.transcript.resume_following()

    def _transcript_activated(self, seg: int, line: int) -> None:
        if self.narrator:
            self.narrator.play(seg, line)

    def _tick_clock(self) -> None:
        if self._sleep_deadline is not None:
            left = self._sleep_deadline - time.monotonic()
            if left <= 0:
                self._sleep_fire()
            else:
                m, s = divmod(int(left), 60)
                self.sleep_btn.setText(f"{m}:{s:02d}")

    def _set_sleep(self, value) -> None:
        self._sleep_deadline = None
        self._sleep_page = None
        if value is None:
            self.sleep_btn.setText("Sleep")
            self.notify("Sleep timer off", 2500)
        elif value == "page":
            if self.narrator and self.layout_ and self.narrator.current_line is not None:
                self._sleep_page = self.layout_.lines[self.narrator.current_line].page
                self.sleep_btn.setText("End of page")
                self.notify(f"Reading will pause at the end of page {self._sleep_page + 1}", 3500)
        else:
            self._sleep_deadline = time.monotonic() + value
            self.notify(f"Reading will pause in {value // 60} minutes", 3500)
            self._tick_clock()

    def _sleep_fire(self) -> None:
        self._sleep_deadline = None
        self._sleep_page = None
        self.sleep_btn.setText("Sleep")
        if self.narrator:
            self.narrator.pause()
        self.notify("Sleep timer: reading paused. Press Play to continue.", 8000)

    # ---- panels and modes ------------------------------------------------------------------------------------------
    def _toggle_transcript(self, on: bool) -> None:
        self._set_transcript(on)

    def _set_transcript(self, on: bool) -> None:
        self.act_transcript.setChecked(on)
        self.settings.transcript_open = on
        self.splitter.set_collapsed(not on)

    def _focus_search(self) -> None:
        self._set_transcript(True)
        self.transcript.focus_search()

    def _toggle_focus(self, on: bool) -> None:
        self.settings.spotlight = on
        self.view.set_spotlight(on)
        self.notify("Focus mode on: everything except the line being read is dimmed" if on else "Focus mode off",
                    2500)

    def _toggle_theme(self) -> None:
        name = "light" if theme.current_name == "dark" else "dark"
        theme.apply(QApplication.instance(), name)
        self.settings.theme = name
        self._apply_icons()
        self.view.refresh_theme()
        self.transcript.refresh_theme()
        if self.layout_ is not None:
            seg, line = (self.narrator.current_seg, self.narrator.current_line) if self.narrator else (None, None)
            self.transcript.set_document(self.segments, self.layout_)
            if line is not None:
                self.transcript.set_position(seg, line)
                self._set_caption(seg, lid=line)
        else:
            self._set_caption(None)
        if self.engine:
            self._update_device_label()
        else:
            self._set_badge(self.device_label.text(), self._badge_token)
        self.rail.update()
        self.play_btn.update()
        self.scrubber.update()
        if self.act_edit.isChecked():
            self._refresh_edit()

    def _on_page_changed(self, page: int) -> None:
        if self.layout_ and self.act_edit.isChecked():
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
            self.notify("That line is not read aloud (see Narration options or Reading order). Starting from the "
                        "next line that is.", 6000)
        self.narrator.play_line(lid)
        self.transcript.resume_following()

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
        accent = theme.color("accent")
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
            edge = accent if not selected else QColor(255, 111, 0)
            overlays.append((page, (x0, y0, x1, y1), QColor(0, 0, 0, 0), edge, str(k + 1)))
            narrated = [l for l in lines if line_included(l, st, opts)]
            roles = {ROLE_LABELS.get(l.role, l.role) for l in lines}
            status = "read" if len(narrated) == len(lines) else ("not read" if not narrated else "partly read")
            text = " ".join(l.text for l in lines)
            item = QListWidgetItem(f"{k + 1}. [{', '.join(sorted(roles))}; {status}] {text[:140]}")
            item.setData(Qt.ItemDataRole.UserRole, b)
            if not narrated:
                item.setForeground(theme.color("muted"))
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
                    self.notify("Audio cache cleared.", 4000)
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
        dlg = ExportDialog(self.layout_.page_count, current, Path(self.state.path).stem, self.settings, self)
        if not dlg.exec():
            return
        self.settings.export_format = dlg.format
        if dlg.format == "mp4":
            self.settings.video_view, self.settings.video_quality = dlg.video_view, dlg.video_quality
        self.settings.save()
        self.start_export(dlg.pages(), dlg.path, dlg.video_view, dlg.video_quality)

    def start_export(self, pages: range, path: str, video_view: str = "zoom_portrait",
                     video_quality: int = 1080) -> None:
        """Export the pages to path (.wav, .mp3 or .mp4) on a worker thread, with a progress window."""
        segs = build_segments(self.layout_, self.state, self.settings.options, pages)
        if not segs:
            QMessageBox.information(self, "Nothing to export", "There is no narrated text on those pages.")
            return
        video = path.lower().endswith(".mp4")
        voice, speed, layout = self.narrator.voice, self.narrator.speed, self.layout_
        token = CancelToken()
        self._export_token = token
        progress = QProgressDialog(f"Exporting {len(segs)} sentences to {Path(path).name}…", "Cancel", 0, len(segs),
                                   self)
        progress.setWindowTitle("Exporting video" if video else "Exporting audio")
        progress.setWindowModality(Qt.WindowModality.NonModal)
        progress.setMinimumDuration(0)
        progress.setMinimumWidth(460)
        progress.canceled.connect(token.cancel)
        self._export_progress = progress
        bridge = Bridge()

        def on_progress(d, t, text):
            progress.setMaximum(t)
            progress.setValue(d)
            if text:
                progress.setLabelText(text)
        bridge.progress.connect(on_progress)
        started = time.perf_counter()

        def done(result):
            progress.close()
            self._export_token = None
            seconds = result.duration if video else result
            m, s = divmod(int(seconds), 60)
            what = "video" if video else "audio"
            text = f"Saved {m} min {s} s of {what} to:\n{path}\n\n(took {time.perf_counter() - started:.0f} s)"
            if video:
                text += f"\n\nEncoded with the {ENCODER_LABELS.get(result.encoder, result.encoder)}."
                if result.fallback_reason:
                    text += f"\n{result.fallback_reason}"
            box = QMessageBox(QMessageBox.Icon.Information, "Export finished", text, QMessageBox.StandardButton.Ok,
                              self)
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
                self.notify("Export cancelled.", 4000)
        bridge.done.connect(done)
        bridge.failed.connect(failed)
        self._export_bridge = bridge

        def work():
            try:
                if video:
                    result = export_video(self.engine, self.cache, layout, segs, path, voice, speed, video_view,
                                          video_quality, bridge.progress.emit, token)
                else:
                    result = export_audio(self.engine, self.cache, segs, path, voice, speed,
                                          lambda d, t: bridge.progress.emit(d, t, ""), token)
                bridge.done.emit(result)
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
        if not self.splitter.collapsed:
            self.settings.transcript_width = self.splitter.sizes()[0]
        self.settings.save()
        if self._export_token:
            self._export_token.cancel()
        if self.narrator:
            self.narrator.shutdown()
        self.view.close_document()
        super().closeEvent(event)
