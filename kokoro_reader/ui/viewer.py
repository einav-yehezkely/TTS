"""PDF viewer with coordinate-exact overlays.

Scene units are PDF points. Each page bitmap is rendered at (zoom x screen
pixel ratio) and scaled back into point units, while highlights and edit
overlays are drawn directly in point units. Zooming, resizing and scrolling
only change the view transform, so overlays cannot drift from the text.
"""

import threading

import pymupdf
from PySide6.QtCore import QEasingCurve, QPointF, QRect, QRectF, QSize, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QImage, QPainter, QPainterPath, QPen, QPixmap, QTransform
from PySide6.QtWidgets import (QFrame, QGraphicsPathItem, QGraphicsPixmapItem, QGraphicsRectItem, QGraphicsScene,
                               QGraphicsSimpleTextItem, QGraphicsView, QRubberBand)

from . import theme

PAGE_GAP = 14.0
MARGIN = 10.0


class _Renderer:
    """Renders pages on a background thread with its own document handle."""

    def __init__(self, path: str, done):
        self.path = path
        self.done = done  # callable(page, scale, QImage) - invoked from the worker thread
        self._cv = threading.Condition()
        self._pending: dict[int, float] = {}
        self._priority: list[int] = []
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="pdf-render", daemon=True)
        self._thread.start()

    def request(self, pages: dict[int, float], priority: list[int]) -> None:
        with self._cv:
            self._pending = dict(pages)
            self._priority = list(priority)
            self._cv.notify()

    def close(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify()

    def _run(self) -> None:
        doc = pymupdf.open(self.path)
        while True:
            with self._cv:
                while not self._pending and not self._stop:
                    self._cv.wait()
                if self._stop:
                    doc.close()
                    return
                page = next((p for p in self._priority if p in self._pending), next(iter(self._pending)))
                scale = self._pending.pop(page)
            try:
                pix = doc[page].get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
                img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()
                self.done(page, scale, img)
            except Exception:  # noqa: BLE001 - a broken page stays blank instead of crashing
                pass


class PdfView(QGraphicsView):
    lineClicked = Signal(int, float, float)        # page, x, y in PDF points
    regionSelected = Signal(int, object)           # page, QRectF in PDF points
    currentPageChanged = Signal(int)
    zoomChanged = Signal(float)
    _rendered = Signal(int, float, QImage)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setBackgroundBrush(theme.color("canvas"))
        self.spotlight = False
        self._spot: QGraphicsPathItem | None = None
        self._scroll_anim = QVariantAnimation(self, duration=280, easingCurve=QEasingCurve.Type.OutCubic)
        self._scroll_anim.valueChanged.connect(lambda v: self.verticalScrollBar().setValue(int(v)))
        self.setRenderHints(QPainter.RenderHint.SmoothPixmapTransform | QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)
        self._pages: list[tuple[QGraphicsRectItem, QGraphicsPixmapItem]] = []
        self._sizes: list[tuple[float, float]] = []
        self._offsets: list[float] = []
        self._scales: dict[int, float] = {}
        self._renderer: _Renderer | None = None
        self.zoom = 1.0
        self.fit_width = True
        self.edit_mode = False
        self._current_page = -1
        self._press = None
        self._band = QRubberBand(QRubberBand.Shape.Rectangle, self.viewport())
        self._overlays: list = []
        self._hl_items: list[QGraphicsRectItem] = []
        self._render_timer = QTimer(self, singleShot=True, interval=40, timeout=self._request_visible)
        self._rendered.connect(self._on_rendered)
        self.verticalScrollBar().valueChanged.connect(self._on_scroll)
        self.horizontalScrollBar().valueChanged.connect(self._on_scroll)

    # ---- document --------------------------------------------------------------------------------------
    def load(self, path: str, sizes: list[tuple[float, float]]) -> None:
        self.close_document()
        scene = self.scene()
        scene.clear()
        self._pages, self._sizes, self._offsets, self._scales = [], list(sizes), [], {}
        self._hl_items, self._overlays, self._spot = [], [], None
        max_w = max((w for w, _ in sizes), default=600)
        y = MARGIN
        for i, (w, h) in enumerate(sizes):
            x = (max_w - w) / 2 + MARGIN
            bg = QGraphicsRectItem(0, 0, w, h)
            bg.setPos(x, y)
            bg.setBrush(QBrush(Qt.GlobalColor.white))
            bg.setPen(QPen(QColor(0, 0, 0, 60), 0))
            bg.setZValue(0)
            scene.addItem(bg)
            pix = QGraphicsPixmapItem(bg)
            pix.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
            pix.setZValue(1)
            self._pages.append((bg, pix))
            self._offsets.append(y)
            y += h + PAGE_GAP
        scene.setSceneRect(0, 0, max_w + 2 * MARGIN, y - PAGE_GAP + MARGIN)
        self._renderer = _Renderer(path, lambda p, s, img: self._rendered.emit(p, s, img))
        self.apply_zoom()
        self.verticalScrollBar().setValue(0)
        self._schedule()

    def close_document(self) -> None:
        if self._renderer:
            self._renderer.close()
            self._renderer = None

    @property
    def page_count(self) -> int:
        return len(self._pages)

    # ---- geometry --------------------------------------------------------------------------------------
    def page_origin(self, page: int) -> QPointF:
        return self._pages[page][0].pos()

    def page_rect_scene(self, page: int, bbox) -> QRectF:
        o = self.page_origin(page)
        x0, y0, x1, y1 = bbox
        return QRectF(o.x() + x0, o.y() + y0, x1 - x0, y1 - y0)

    def scene_to_page(self, pt: QPointF) -> tuple[int, float, float] | None:
        for i, (bg, _) in enumerate(self._pages):
            local = pt - bg.pos()
            w, h = self._sizes[i]
            if -PAGE_GAP / 2 <= local.y() <= h + PAGE_GAP / 2 and -MARGIN <= local.x() <= w + MARGIN:
                return i, local.x(), local.y()
        return None

    def visible_pages(self) -> list[int]:
        r = self.mapToScene(self.viewport().rect()).boundingRect()
        out = []
        for i, top in enumerate(self._offsets):
            h = self._sizes[i][1]
            if top + h >= r.top() and top <= r.bottom():
                out.append(i)
        return out

    # ---- zoom ----------------------------------------------------------------------------------------------
    def apply_zoom(self) -> None:
        if self.fit_width and self._sizes:
            avail = max(100, self.viewport().width() - 4)
            self.zoom = avail / self.scene().sceneRect().width()
        self.zoom = max(0.2, min(6.0, self.zoom))
        self.setTransform(QTransform.fromScale(self.zoom, self.zoom))
        self.zoomChanged.emit(self.zoom)
        self._schedule()

    def set_zoom(self, z: float) -> None:
        self.fit_width = False
        self.zoom = z
        self.apply_zoom()

    def zoom_by(self, factor: float) -> None:
        self.set_zoom(self.zoom * factor)

    def set_fit_width(self) -> None:
        self.fit_width = True
        self.apply_zoom()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.fit_width:
            self.apply_zoom()
        self._schedule()

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom_by(1.15 if event.angleDelta().y() > 0 else 1 / 1.15)
            event.accept()
            return
        super().wheelEvent(event)

    # ---- rendering -------------------------------------------------------------------------------------------
    def _schedule(self) -> None:
        self._render_timer.start()

    def _on_scroll(self) -> None:
        self._schedule()
        vis = self.visible_pages()
        if vis:
            r = self.mapToScene(self.viewport().rect()).boundingRect()
            mid = r.center().y()
            page = min(vis, key=lambda i: abs(self._offsets[i] + self._sizes[i][1] / 2 - mid))
            if page != self._current_page:
                self._current_page = page
                self.currentPageChanged.emit(page)

    def _request_visible(self) -> None:
        if not self._renderer:
            return
        vis = self.visible_pages()
        if not vis:
            return
        scale = self.zoom * self.devicePixelRatioF()
        want = {}
        around = sorted(set(vis) | {max(0, vis[0] - 1), min(len(self._pages) - 1, vis[-1] + 1)})
        for p in around:
            have = self._scales.get(p)
            if have is None or abs(have - scale) / scale > 0.04:
                want[p] = scale
        keep = set(range(max(0, vis[0] - 4), min(len(self._pages), vis[-1] + 5)))
        for p in list(self._scales):  # free bitmaps of pages far away
            if p not in keep:
                self._pages[p][1].setPixmap(QPixmap())
                del self._scales[p]
        if want:
            self._renderer.request(want, vis)

    def _on_rendered(self, page: int, scale: float, img: QImage) -> None:
        if page >= len(self._pages):
            return
        _, pix = self._pages[page]
        pix.setPixmap(QPixmap.fromImage(img))
        pix.setScale(1.0 / scale)
        self._scales[page] = scale

    # ---- highlight --------------------------------------------------------------------------------------------
    def set_highlight(self, page: int | None, bboxes: list) -> None:
        for it in self._hl_items:
            self.scene().removeItem(it)
        self._hl_items = []
        if page is None:
            return
        for bbox in bboxes:
            r = self.page_rect_scene(page, bbox).adjusted(-2, -1.5, 2, 1.5)
            it = QGraphicsRectItem(r)
            it.setBrush(QColor(255, 196, 0, 105))
            it.setPen(QPen(QColor(230, 120, 0, 220), 0.8))
            it.setZValue(10)
            self.scene().addItem(it)
            self._hl_items.append(it)
        self._update_spotlight()

    # ---- focus mode -------------------------------------------------------------------------------------------
    def set_spotlight(self, on: bool) -> None:
        self.spotlight = on
        self._update_spotlight()

    def _update_spotlight(self) -> None:
        if not self.spotlight or not self._hl_items:
            if self._spot is not None:
                self._spot.hide()
            return
        if self._spot is None:
            self._spot = QGraphicsPathItem()
            self._spot.setPen(QPen(Qt.PenStyle.NoPen))
            self._spot.setZValue(8)  # above the page bitmaps, below the highlight
            self.scene().addItem(self._spot)
        self._spot.setBrush(QColor(8, 10, 16, 165))
        path = QPainterPath()
        path.setFillRule(Qt.FillRule.OddEvenFill)
        path.addRect(self.scene().sceneRect())
        hole = self._hl_items[0].rect().adjusted(-10, -9, 10, 9)
        path.addRoundedRect(hole, 6, 6)
        self._spot.setPath(path)
        self._spot.show()

    def spotlight_rect_in_viewport(self) -> QRect | None:
        if self._spot is None or not self._spot.isVisible():
            return None
        return self.mapFromScene(self._hl_items[0].rect()).boundingRect()

    def refresh_theme(self) -> None:
        self.setBackgroundBrush(theme.color("canvas"))

    def highlight_rect_in_viewport(self) -> QRect | None:
        """Where the highlight is drawn on screen (for tests)."""
        if not self._hl_items:
            return None
        return self.mapFromScene(self._hl_items[0].rect()).boundingRect()

    def ensure_visible(self, page: int, bbox, animate: bool = True) -> None:
        r = self.page_rect_scene(page, bbox)
        view = self.mapToScene(self.viewport().rect()).boundingRect()
        band_top = view.top() + view.height() * 0.12
        band_bottom = view.bottom() - view.height() * 0.18
        if r.top() < band_top or r.bottom() > band_bottom:
            # glide so the line ends up about a third of the way down
            target_center = r.center().y() + view.height() * 0.15
            bar = self.verticalScrollBar()
            target = bar.value() + int((target_center - view.center().y()) * self.zoom)
            target = max(bar.minimum(), min(bar.maximum(), target))
            self._scroll_anim.stop()
            if animate and abs(target - bar.value()) < self.viewport().height() * 3:
                self._scroll_anim.setStartValue(bar.value())
                self._scroll_anim.setEndValue(target)
                self._scroll_anim.start()
            else:
                bar.setValue(target)
        view = self.mapToScene(self.viewport().rect()).boundingRect()
        padded = r.adjusted(-4, 0, 4, 0)
        hbar = self.horizontalScrollBar()
        margin = 24 / self.zoom
        if padded.left() < view.left() or padded.width() + 2 * margin > view.width():
            hbar.setValue(hbar.value() - int((view.left() - padded.left() + margin) * self.zoom))
        elif padded.right() > view.right():
            hbar.setValue(hbar.value() + int((padded.right() - view.right() + margin) * self.zoom))

    # ---- edit overlays -------------------------------------------------------------------------------------------
    def set_overlays(self, items: list[tuple[int, tuple, QColor, QColor, str]]) -> None:
        """items: (page, bbox, fill, outline, label)."""
        for it in self._overlays:
            self.scene().removeItem(it)
        self._overlays = []
        font = QFont("Segoe UI", 7)
        font.setBold(True)
        for page, bbox, fill, outline, label in items:
            r = self.page_rect_scene(page, bbox).adjusted(-1.5, -1, 1.5, 1)
            box = QGraphicsRectItem(r)
            box.setBrush(fill)
            box.setPen(QPen(outline, 0.9))
            box.setZValue(5)
            self.scene().addItem(box)
            self._overlays.append(box)
            if label:
                tag = QGraphicsSimpleTextItem(label)
                tag.setFont(font)
                tag.setBrush(QColor("white"))
                tb = tag.boundingRect()
                bg = QGraphicsRectItem(0, 0, tb.width() + 4, tb.height())
                bg.setBrush(outline)
                bg.setPen(QPen(Qt.PenStyle.NoPen))
                bg.setPos(r.left() - tb.width() - 6, r.top())
                bg.setZValue(6)
                tag.setParentItem(bg)
                tag.setPos(2, 0)
                self.scene().addItem(bg)
                self._overlays.append(bg)

    # ---- mouse ----------------------------------------------------------------------------------------------------
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = event.position().toPoint()
            if self.edit_mode:
                self._band.setGeometry(QRect(self._press, QSize()))
                self._band.show()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._press is not None and self.edit_mode:
            self._band.setGeometry(QRect(self._press, event.position().toPoint()).normalized())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._press is None:
            return super().mouseReleaseEvent(event)
        start, end = self._press, event.position().toPoint()
        self._press = None
        self._band.hide()
        dragged = (end - start).manhattanLength() > 6
        if self.edit_mode and dragged:
            a = self.scene_to_page(self.mapToScene(start))
            b = self.scene_to_page(self.mapToScene(end))
            if a and b and a[0] == b[0]:
                self.regionSelected.emit(a[0], QRectF(QPointF(a[1], a[2]), QPointF(b[1], b[2])).normalized())
            return
        if not dragged:
            hit = self.scene_to_page(self.mapToScene(end))
            if hit:
                self.lineClicked.emit(*hit)
