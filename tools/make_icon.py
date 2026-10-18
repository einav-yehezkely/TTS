"""Draws launcher/app.ico (a page with a play symbol)."""

import sys
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPolygonF

app = QGuiApplication(sys.argv)
size = 256
img = QImage(size, size, QImage.Format.Format_ARGB32)
img.fill(Qt.GlobalColor.transparent)
p = QPainter(img)
p.setRenderHint(QPainter.RenderHint.Antialiasing)
p.setBrush(QColor("#ffffff"))
p.setPen(QColor("#37474f"))
p.drawRoundedRect(QRectF(36, 16, 160, 210), 14, 14)
p.setPen(Qt.PenStyle.NoPen)
p.setBrush(QColor("#90a4ae"))
for i, w in enumerate((110, 120, 95, 120, 70)):
    p.drawRoundedRect(QRectF(60, 46 + i * 24, w, 9), 4, 4)
p.setBrush(QColor("#ffc400"))
p.drawRoundedRect(QRectF(56, 66, 128, 17), 4, 4)
p.setBrush(QColor("#1e88e5"))
p.drawEllipse(QPointF(176, 186), 62, 62)
p.setBrush(QColor("white"))
p.drawPolygon(QPolygonF([QPointF(158, 156), QPointF(158, 216), QPointF(208, 186)]))
p.end()
out = Path(__file__).resolve().parent.parent / "launcher" / "app.ico"
ok = img.save(str(out), "ICO")
print("saved" if ok else "failed", out)
