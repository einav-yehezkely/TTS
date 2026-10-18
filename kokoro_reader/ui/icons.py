"""Small vector icon set, drawn in the theme's colors (no image files, crisp at any DPI)."""

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF

from . import theme

S = 64  # drawing grid


def _poly(p: QPainter, pts) -> None:
    p.drawPolygon(QPolygonF([QPointF(x, y) for x, y in pts]))


def _draw(p: QPainter, name: str, c: QColor) -> None:
    pen = QPen(c, 5.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    fill = lambda: (p.setPen(Qt.PenStyle.NoPen), p.setBrush(c))  # noqa: E731
    if name == "play":
        fill()
        path = QPainterPath()
        path.moveTo(20, 12)
        path.lineTo(52, 32)
        path.lineTo(20, 52)
        path.closeSubpath()
        p.drawPath(path)
    elif name == "pause":
        fill()
        p.drawRoundedRect(QRectF(16, 12, 11, 40), 3, 3)
        p.drawRoundedRect(QRectF(37, 12, 11, 40), 3, 3)
    elif name == "stop":
        fill()
        p.drawRoundedRect(QRectF(16, 16, 32, 32), 5, 5)
    elif name in ("prev", "next"):
        fill()
        if name == "prev":
            _poly(p, [(48, 14), (22, 32), (48, 50)])
            p.drawRoundedRect(QRectF(14, 14, 7, 36), 2, 2)
        else:
            _poly(p, [(16, 14), (42, 32), (16, 50)])
            p.drawRoundedRect(QRectF(43, 14, 7, 36), 2, 2)
    elif name == "open":
        path = QPainterPath()
        path.moveTo(8, 50)
        path.lineTo(8, 16)
        path.lineTo(24, 16)
        path.lineTo(29, 22)
        path.lineTo(52, 22)
        path.lineTo(52, 30)
        p.drawPath(path)
        path2 = QPainterPath()
        path2.moveTo(8, 50)
        path2.lineTo(16, 30)
        path2.lineTo(58, 30)
        path2.lineTo(50, 50)
        path2.closeSubpath()
        p.drawPath(path2)
    elif name in ("zoom_in", "zoom_out", "search"):
        p.drawEllipse(QPointF(27, 27), 15, 15)
        p.drawLine(QPointF(38, 38), QPointF(54, 54))
        if name != "search":
            p.drawLine(QPointF(20, 27), QPointF(34, 27))
            if name == "zoom_in":
                p.drawLine(QPointF(27, 20), QPointF(27, 34))
    elif name == "fit":
        for (x, y, dx, dy) in ((10, 10, 1, 1), (54, 10, -1, 1), (10, 54, 1, -1), (54, 54, -1, -1)):
            p.drawLine(QPointF(x, y), QPointF(x + 12 * dx, y))
            p.drawLine(QPointF(x, y), QPointF(x, y + 12 * dy))
        p.drawLine(QPointF(22, 32), QPointF(42, 32))
    elif name == "transcript":
        p.drawRoundedRect(QRectF(10, 8, 44, 48), 7, 7)
        for y, w in ((22, 24), (32, 24), (42, 16)):
            p.drawLine(QPointF(20, y), QPointF(20 + w, y))
    elif name == "focus":
        p.drawEllipse(QPointF(32, 32), 20, 20)
        fill()
        p.drawEllipse(QPointF(32, 32), 7, 7)
    elif name == "edit":
        for y in (16, 32, 48):
            p.drawLine(QPointF(26, y), QPointF(54, y))
        fill()
        for y, k in ((16, 1), (32, 2), (48, 3)):
            p.drawEllipse(QPointF(13, y), 4.5, 4.5)
    elif name == "export":
        p.drawLine(QPointF(32, 8), QPointF(32, 38))
        p.drawPolyline(QPolygonF([QPointF(20, 27), QPointF(32, 39), QPointF(44, 27)]))
        p.drawPolyline(QPolygonF([QPointF(10, 40), QPointF(10, 54), QPointF(54, 54), QPointF(54, 40)]))
    elif name == "moon":
        fill()
        path = QPainterPath()
        path.addEllipse(QPointF(30, 34), 20, 20)
        cut = QPainterPath()
        cut.addEllipse(QPointF(42, 24), 17, 17)
        p.drawPath(path.subtracted(cut))
    elif name == "sun":
        fill()
        p.drawEllipse(QPointF(32, 32), 10, 10)
        p.setPen(pen)
        for k in range(8):
            a = k * math.pi / 4
            p.drawLine(QPointF(32 + 17 * math.cos(a), 32 + 17 * math.sin(a)),
                       QPointF(32 + 25 * math.cos(a), 32 + 25 * math.sin(a)))
    elif name == "timer":
        p.drawEllipse(QPointF(32, 35), 21, 21)
        p.drawLine(QPointF(32, 35), QPointF(32, 23))
        p.drawLine(QPointF(32, 35), QPointF(41, 40))
        p.drawLine(QPointF(25, 7), QPointF(39, 7))
    elif name == "follow":
        p.drawEllipse(QPointF(32, 32), 17, 17)
        for (a, b, c2, d) in ((32, 4, 32, 14), (32, 50, 32, 60), (4, 32, 14, 32), (50, 32, 60, 32)):
            p.drawLine(QPointF(a, b), QPointF(c2, d))
        fill()
        p.drawEllipse(QPointF(32, 32), 6, 6)
    elif name in ("chev_left", "chev_right"):
        pts = [(40, 14), (22, 32), (40, 50)] if name == "chev_left" else [(24, 14), (42, 32), (24, 50)]
        p.drawPolyline(QPolygonF([QPointF(x, y) for x, y in pts]))
    elif name == "speed":
        path = QPainterPath()
        path.arcMoveTo(QRectF(8, 12, 48, 48), 200)
        path.arcTo(QRectF(8, 12, 48, 48), 200, -220)
        p.drawPath(path)
        p.drawLine(QPointF(32, 36), QPointF(44, 22))
    elif name == "voice":
        p.drawRoundedRect(QRectF(24, 6, 16, 30), 8, 8)
        path = QPainterPath()
        path.moveTo(14, 30)
        path.cubicTo(14, 52, 50, 52, 50, 30)
        p.drawPath(path)
        p.drawLine(QPointF(32, 46), QPointF(32, 57))
    elif name == "down":
        p.drawLine(QPointF(32, 10), QPointF(32, 52))
        p.drawPolyline(QPolygonF([QPointF(16, 37), QPointF(32, 53), QPointF(48, 37)]))
    elif name == "gpu":
        p.drawRoundedRect(QRectF(12, 12, 40, 40), 6, 6)
        fill()
        p.drawRoundedRect(QRectF(22, 22, 20, 20), 3, 3)
    elif name == "close":
        p.drawLine(QPointF(18, 18), QPointF(46, 46))
        p.drawLine(QPointF(46, 18), QPointF(18, 46))


def pixmap(name: str, token: str = "text", size: int = 64, col: QColor | None = None) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(size / S, size / S)
    _draw(p, name, col or theme.color(token))
    p.end()
    return pm


def icon(name: str, token: str = "text") -> QIcon:
    ic = QIcon()
    ic.addPixmap(pixmap(name, token), QIcon.Mode.Normal, QIcon.State.Off)
    ic.addPixmap(pixmap(name, "accent"), QIcon.Mode.Normal, QIcon.State.On)
    ic.addPixmap(pixmap(name, "muted"), QIcon.Mode.Disabled, QIcon.State.Off)
    return ic
