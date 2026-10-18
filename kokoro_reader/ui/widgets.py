"""Custom controls for the player bar: play button, document scrubber, toast notifications."""

import bisect

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QAbstractButton, QGraphicsOpacityEffect, QLabel, QToolTip, QWidget

from . import icons, theme


class PlayButton(QAbstractButton):
    """Round accent button: play / pause, with a spinning ring while speech is being prepared."""

    def __init__(self, parent=None, size: int = 50):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mode = "play"  # play / pause / buffering
        self._angle = 0
        self._spin = QTimer(self, interval=16, timeout=self._step)

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        if mode == "buffering":
            self._spin.start()
        else:
            self._spin.stop()
        self.setToolTip({"play": "Play (Space)", "pause": "Pause (Space)",
                         "buffering": "Preparing speech… (Space to pause)"}[mode])
        self.update()

    def _step(self) -> None:
        self._angle = (self._angle + 6) % 360
        self.update()

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(3, 3, -3, -3)
        acc = theme.color("accent")
        if self.underMouse():
            acc = acc.lighter(112)
        if not self.isEnabled():
            acc = theme.color("surface3")
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(acc)
        p.drawEllipse(r)
        if self.mode == "buffering":
            pen = QPen(theme.color("accent_text"), 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawArc(r.adjusted(5, 5, -5, -5), -self._angle * 16, 100 * 16)
        name = "play" if self.mode == "play" else "pause"
        s = r.width() * 0.42
        off = s * 0.08 if name == "play" else 0
        target = QRectF(r.center().x() - s / 2 + off, r.center().y() - s / 2, s, s)
        p.drawPixmap(target.toRect(), icons.pixmap(name, "accent_text", 96))
        p.end()

    def enterEvent(self, e) -> None:
        self.update()

    def leaveEvent(self, e) -> None:
        self.update()


class Scrubber(QWidget):
    """Whole-document progress: played part, page ticks, hover preview, click or drag to jump."""
    seekRequested = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(160)
        self.setFixedHeight(26)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.value = 0.0
        self.ticks: list[float] = []
        self.describe = None  # callable(fraction) -> str for the hover preview
        self._hover: float | None = None
        self._drag = False

    def set_ticks(self, ticks: list[float]) -> None:
        self.ticks = ticks
        self.update()

    def set_value(self, v: float) -> None:
        if not self._drag:
            self.value = max(0.0, min(1.0, v))
            self.update()

    def _frac(self, x: float) -> float:
        return max(0.0, min(1.0, (x - 8) / max(1, self.width() - 16)))

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width() - 16
        y = self.height() / 2
        track = QRectF(8, y - 3, w, 6)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.color("surface3"))
        p.drawRoundedRect(track, 3, 3)
        shown = self.value
        if self._hover is not None:
            hv = QColor(theme.color("accent"))
            hv.setAlpha(60)
            p.setBrush(hv)
            p.drawRoundedRect(QRectF(8, y - 3, w * self._hover, 6), 3, 3)
        p.setBrush(theme.color("accent"))
        p.drawRoundedRect(QRectF(8, y - 3, max(6, w * shown), 6), 3, 3)
        # page ticks, thinned out when there are many
        if self.ticks:
            step = max(1, int(len(self.ticks) / max(1, w / 6)))
            tick = QColor(theme.color("muted"))
            tick.setAlpha(110)
            p.setPen(QPen(tick, 1))
            for t in self.ticks[1::step]:
                x = 8 + w * t
                p.drawLine(int(x), int(y + 5), int(x), int(y + 8))
        p.setPen(QPen(theme.color("surface"), 2))
        p.setBrush(theme.color("accent"))
        p.drawEllipse(QRectF(8 + w * shown - 7, y - 7, 14, 14))
        p.end()

    def mouseMoveEvent(self, e) -> None:
        f = self._frac(e.position().x())
        self._hover = f
        if self._drag:
            self.value = f
        if self.describe:
            QToolTip.showText(self.mapToGlobal(QPoint(int(e.position().x()), -42)), self.describe(f), self)
        self.update()

    def leaveEvent(self, e) -> None:
        self._hover = None
        QToolTip.hideText()
        self.update()

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = True
            self.value = self._frac(e.position().x())
            self.update()

    def mouseReleaseEvent(self, e) -> None:
        if self._drag:
            self._drag = False
            self.seekRequested.emit(self._frac(e.position().x()))


class Toast(QLabel):
    """A short message that fades in at the top of the reading area and fades out by itself."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("Toast")
        self.setWordWrap(True)
        self.setMaximumWidth(560)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._fx = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._fx)
        self._anim = QPropertyAnimation(self._fx, b"opacity", self, duration=220,
                                        easingCurve=QEasingCurve.Type.OutCubic)
        self._anim.finished.connect(self._finished)
        self._fading_out = False
        self._timer = QTimer(self, singleShot=True, timeout=self._fade_out)
        self.hide()

    def _finished(self) -> None:
        if self._fading_out:
            self.hide()

    def show_message(self, text: str, ms: int = 4000) -> None:
        self.setText(text)
        self.adjustSize()
        par = self.parentWidget()
        self.move((par.width() - self.width()) // 2, 18)
        was_visible = self.isVisible()
        self.raise_()
        self.show()
        self._anim.stop()
        self._fading_out = False
        self._anim.setStartValue(self._fx.opacity() if was_visible else 0.0)
        self._anim.setEndValue(1.0)
        self._anim.start()
        self._timer.start(ms)

    def _fade_out(self) -> None:
        self._anim.stop()
        self._fading_out = True
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.0)
        self._anim.start()


def fraction_to_index(cum: list[int], fraction: float) -> int:
    """Index of the segment containing the given fraction of all characters."""
    total = cum[-1] if cum else 0
    return max(0, min(len(cum) - 2, bisect.bisect_right(cum, fraction * total) - 1)) if total else 0
