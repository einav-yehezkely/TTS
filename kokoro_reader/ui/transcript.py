"""Transcript side panel: every narrated sentence, grouped by page, following the reading.

The current sentence gets a soft background and the exact line being heard
inside it a stronger one, both as "extra selections" so the document itself is
never rewritten while playing. Scrolling away suspends following and offers a
"Back to current" button; clicking any part of a sentence reads from there.
"""

from PySide6.QtCore import (Property, QEasingCurve, QPropertyAnimation, QRectF, Qt, QVariantAnimation,
                            Signal)
from PySide6.QtGui import (QFont, QPainter, QTextBlockFormat, QTextCharFormat, QTextCursor, QTextDocument,
                           QTextFormat)
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QPushButton, QSplitter, QTextBrowser, QTextEdit,
                               QToolButton, QVBoxLayout, QWidget)

from . import icons, theme


class TranscriptView(QTextBrowser):
    userScrolled = Signal()
    clickedAt = Signal(int)  # document position

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Transcript")
        self.setOpenLinks(False)
        self.setReadOnly(True)
        self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
        self._press = None
        self.verticalScrollBar().actionTriggered.connect(lambda _: self.userScrolled.emit())

    def wheelEvent(self, e) -> None:
        super().wheelEvent(e)
        self.userScrolled.emit()

    def mousePressEvent(self, e) -> None:
        self._press = e.position().toPoint()
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e) -> None:
        super().mouseReleaseEvent(e)
        if self._press is not None and (e.position().toPoint() - self._press).manhattanLength() < 5 \
                and not self.textCursor().hasSelection():
            self.clickedAt.emit(self.cursorForPosition(e.position().toPoint()).position())
        self._press = None


class TranscriptPanel(QWidget):
    activated = Signal(int, int)       # segment index, line id
    collapseRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("SidePanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.segments = []
        self._seg_block: list[int] = []                    # segment -> block number
        self._block_seg: dict[int, int] = {}               # block number -> segment
        self._frag_offsets: list[list[tuple[int, int, int]]] = []  # per segment: (line, start, end) in block
        self._cur_seg: int | None = None
        self._cur_line: int | None = None
        self._search_sel = None
        self.following = True
        self._anim = QVariantAnimation(self, duration=260, easingCurve=QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(lambda v: self.view.verticalScrollBar().setValue(int(v)))

        header = QWidget(objectName="PanelHeader")
        hl = QVBoxLayout(header)
        hl.setContentsMargins(14, 12, 10, 8)
        hl.setSpacing(8)
        row = QHBoxLayout()
        title = QLabel("Transcript", objectName="PanelTitle")
        self.count = QLabel("", objectName="PanelCount")
        self.collapse_btn = QToolButton(toolTip="Hide transcript (Ctrl+T)")
        self.collapse_btn.clicked.connect(self.collapseRequested)
        row.addWidget(title)
        row.addWidget(self.count)
        row.addStretch(1)
        row.addWidget(self.collapse_btn)
        hl.addLayout(row)
        self.search = QLineEdit(placeholderText="Search the text  (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self.search.returnPressed.connect(self.find_next)
        self.search.textChanged.connect(self._search_changed)
        hl.addWidget(self.search)
        self.search_info = QLabel("", objectName="PanelCount")
        self.search_info.hide()
        hl.addWidget(self.search_info)

        self.view = TranscriptView()
        self.view.userScrolled.connect(self._user_scrolled)
        self.view.clickedAt.connect(self._clicked)
        self.back_btn = QPushButton("Back to current", self.view, objectName="Pill")
        self.back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_btn.clicked.connect(self.resume_following)
        self.back_btn.hide()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(header)
        lay.addWidget(self.view, 1)
        self.refresh_theme()

    # ---- content -------------------------------------------------------------------------------------------
    def set_document(self, segments, layout) -> None:
        self.segments = segments
        doc = QTextDocument(self)
        doc.setDocumentMargin(14)
        f = QFont(self.font())
        f.setPointSizeF(10.5)
        doc.setDefaultFont(f)
        cur = QTextCursor(doc)
        cur.beginEditBlock()
        body_block = QTextBlockFormat()
        body_block.setBottomMargin(7)
        body_block.setLineHeight(118, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
        head_block = QTextBlockFormat()
        head_block.setTopMargin(14)
        head_block.setBottomMargin(6)
        head_fmt = QTextCharFormat()
        head_fmt.setFontWeight(QFont.Weight.Bold)
        head_fmt.setFontPointSize(8.5)
        head_fmt.setFontLetterSpacing(108)
        head_fmt.setForeground(theme.color("muted"))
        body_fmt = QTextCharFormat()
        self._seg_block, self._block_seg, self._frag_offsets = [], {}, []
        last_page = None
        first = True
        for s in segments:
            page = layout.lines[s.first_line].page
            if page != last_page:
                info = layout.pages[page]
                label = f"PAGE {page + 1}" + (f"   ·   PRINTED {info.label.upper()}"
                                               if info.label and info.label != str(page + 1) else "")
                if not first:
                    cur.insertBlock(head_block, head_fmt)
                else:
                    cur.setBlockFormat(head_block)
                    cur.setBlockCharFormat(head_fmt)
                    first = False
                cur.insertText(label, head_fmt)
                last_page = page
            cur.insertBlock(body_block, body_fmt)
            block_no = cur.blockNumber()
            offsets, pos = [], 0
            parts = []
            for lid, t in s.fragments:
                if parts:
                    pos += 1
                offsets.append((lid, pos, pos + len(t)))
                parts.append(t)
                pos += len(t)
            cur.insertText(" ".join(parts), body_fmt)
            self._seg_block.append(block_no)
            self._block_seg[block_no] = s.index
            self._frag_offsets.append(offsets)
        cur.endEditBlock()
        self.view.setDocument(doc)
        self.count.setText(f"{len(segments):,} sentences")
        self._cur_seg = self._cur_line = None
        self._search_sel = None
        self._apply_selections()

    def clear(self) -> None:
        self.segments = []
        self.view.setDocument(QTextDocument(self))
        self.count.setText("")

    # ---- following -----------------------------------------------------------------------------------------
    def set_position(self, seg: int | None, line: int | None) -> None:
        if seg is None or not (0 <= seg < len(self._seg_block)):
            return
        changed_seg = seg != self._cur_seg
        self._cur_seg, self._cur_line = seg, line
        self._apply_selections()
        if self.following and (changed_seg or not self._block_visible(seg)):
            self._scroll_to_block(self._seg_block[seg])

    def current_block(self) -> int | None:
        return self._seg_block[self._cur_seg] if self._cur_seg is not None else None

    def _block_visible(self, seg: int) -> bool:
        r = self._block_rect(self._seg_block[seg])
        top = self.view.verticalScrollBar().value()
        h = self.view.viewport().height()
        return r.top() >= top + h * 0.08 and r.bottom() <= top + h * 0.8

    def _block_rect(self, block_no: int) -> QRectF:
        block = self.view.document().findBlockByNumber(block_no)
        return self.view.document().documentLayout().blockBoundingRect(block)

    def _scroll_to_block(self, block_no: int) -> None:
        r = self._block_rect(block_no)
        h = self.view.viewport().height()
        bar = self.view.verticalScrollBar()
        target = max(bar.minimum(), min(bar.maximum(), int(r.top() - h * 0.3)))
        self._anim.stop()
        self._anim.setStartValue(bar.value())
        self._anim.setEndValue(target)
        self._anim.start()

    def _user_scrolled(self) -> None:
        if self._cur_seg is None:
            return
        self._anim.stop()
        if self.following:
            self.following = False
            self._place_back_btn()
            self.back_btn.show()

    def resume_following(self) -> None:
        self.following = True
        self.back_btn.hide()
        if self._cur_seg is not None:
            self._scroll_to_block(self._seg_block[self._cur_seg])

    def _place_back_btn(self) -> None:
        self.back_btn.adjustSize()
        vp = self.view.viewport().geometry()
        self.back_btn.move(vp.center().x() - self.back_btn.width() // 2, vp.bottom() - self.back_btn.height() - 14)
        self.back_btn.raise_()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        if self.back_btn.isVisible():
            self._place_back_btn()

    # ---- selections ----------------------------------------------------------------------------------------
    def _apply_selections(self) -> None:
        sels = []
        doc = self.view.document()
        if self._cur_seg is not None and self._cur_seg < len(self._seg_block):
            block = doc.findBlockByNumber(self._seg_block[self._cur_seg])
            s = QTextEdit.ExtraSelection()
            s.cursor = QTextCursor(block)
            s.cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
            s.format.setBackground(theme.color("accent_soft"))
            sels.append(s)
            for lid, a, b in self._frag_offsets[self._cur_seg]:
                if lid == self._cur_line:
                    m = QTextEdit.ExtraSelection()
                    m.cursor = QTextCursor(block)
                    m.cursor.setPosition(block.position() + a)
                    m.cursor.setPosition(block.position() + b, QTextCursor.MoveMode.KeepAnchor)
                    m.format.setBackground(theme.color("mark_soft"))
                    m.format.setProperty(QTextFormat.Property.FontWeight, QFont.Weight.DemiBold)
                    sels.append(m)
                    break
        if self._search_sel is not None:
            sels.append(self._search_sel)
        self.view.setExtraSelections(sels)

    # ---- clicks and search ---------------------------------------------------------------------------------
    def _clicked(self, pos: int) -> None:
        block = self.view.document().findBlock(pos)
        seg = self._block_seg.get(block.blockNumber())
        if seg is None:
            return
        rel = pos - block.position()
        line = self.segments[seg].first_line
        for lid, a, b in self._frag_offsets[seg]:
            if a <= rel <= b + 1:
                line = lid
                break
        self.following = True
        self.back_btn.hide()
        self.activated.emit(seg, line)

    def focus_search(self) -> None:
        self.search.setFocus()
        self.search.selectAll()

    def _search_changed(self, text: str) -> None:
        if not text:
            self._search_sel = None
            self.search_info.hide()
            self._apply_selections()

    def find_next(self) -> None:
        text = self.search.text().strip()
        if not text:
            return
        doc = self.view.document()
        start = self._search_sel.cursor if self._search_sel is not None else QTextCursor(doc)
        found = doc.find(text, start)
        wrapped = False
        if found.isNull():
            found = doc.find(text, QTextCursor(doc))
            wrapped = True
        if found.isNull():
            self.search_info.setText("No matches")
            self.search_info.show()
            self._search_sel = None
            self._apply_selections()
            return
        total = 0
        c = doc.find(text, QTextCursor(doc))
        while not c.isNull() and total < 9999:
            total += 1
            c = doc.find(text, c)
        sel = QTextEdit.ExtraSelection()
        sel.cursor = found
        sel.format.setBackground(theme.color("accent"))
        sel.format.setForeground(theme.color("accent_text"))
        self._search_sel = sel
        self.search_info.setText(f"{total} match{'es' if total != 1 else ''}"
                                 + ("  ·  wrapped to the top" if wrapped else "") + "  ·  Enter for next")
        self.search_info.show()
        self._apply_selections()
        self._user_scrolled()
        self._scroll_to_block(found.block().blockNumber())

    def refresh_theme(self) -> None:
        self.collapse_btn.setIcon(icons.icon("chev_left", "muted"))
        if self.segments:
            self._apply_selections()


class Rail(QWidget):
    """The collapsed transcript: a slim strip that expands the panel when clicked."""
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Rail")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(34)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Show transcript (Ctrl+T)")

    def mouseReleaseEvent(self, e) -> None:
        self.clicked.emit()

    def paintEvent(self, e) -> None:
        super().paintEvent(e)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.drawPixmap(7, 12, 20, 20, icons.pixmap("chev_right", "muted"))
        p.drawPixmap(7, 44, 20, 20, icons.pixmap("transcript", "muted"))
        p.setPen(theme.color("muted"))
        f = QFont(self.font())
        f.setPointSizeF(8.5)
        f.setBold(True)
        f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 115)
        p.setFont(f)
        p.translate(22, 78)
        p.rotate(90)
        p.drawText(0, 0, "TRANSCRIPT")
        p.end()


class SideSplitter(QSplitter):
    """Splitter whose first widget (the transcript) slides open and closed."""

    def __init__(self, panel: TranscriptPanel, rail: Rail, main: QWidget, width: int = 360):
        super().__init__(Qt.Orientation.Horizontal)
        self.panel, self.rail = panel, rail
        self.side = QWidget()
        sl = QHBoxLayout(self.side)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)
        sl.addWidget(panel)
        sl.addWidget(rail)
        self.addWidget(self.side)
        self.addWidget(main)
        self.setCollapsible(0, False)
        self.setCollapsible(1, False)
        self.setHandleWidth(1)
        self.open_width = width
        self.collapsed = False
        self.rail.hide()
        self._anim = QPropertyAnimation(self, b"sideWidth", self, duration=220,
                                        easingCurve=QEasingCurve.Type.OutCubic)
        self._anim.finished.connect(self._done)
        self.setSizes([width, 1000])

    def _get_w(self) -> int:
        return self.sizes()[0]

    def _set_w(self, w: int) -> None:
        total = sum(self.sizes())
        self.setSizes([int(w), max(100, total - int(w))])

    sideWidth = Property(int, _get_w, _set_w)

    def set_collapsed(self, collapsed: bool, animate: bool = True) -> None:
        if collapsed == self.collapsed:
            return
        if collapsed:
            self.open_width = max(240, self.sizes()[0])
        self.collapsed = collapsed
        self.side.setMinimumWidth(0)
        self.side.setMaximumWidth(16777215)
        end = self.rail.width() if collapsed else self.open_width
        if collapsed:
            self.panel.hide()
            self.rail.show()
        else:
            self.rail.hide()
            self.panel.show()
        if animate:
            self._anim.stop()
            self._anim.setStartValue(self.sizes()[0])
            self._anim.setEndValue(end)
            self._anim.start()
        else:
            self._set_w(end)
            self._done()

    def _done(self) -> None:
        if self.collapsed:
            self.side.setFixedWidth(self.rail.width())
            self.handle(1).setEnabled(False)
        else:
            self.side.setMinimumWidth(240)
            self.side.setMaximumWidth(700)
            self.handle(1).setEnabled(True)
