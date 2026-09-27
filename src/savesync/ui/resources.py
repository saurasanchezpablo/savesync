"""Labels, colors and icons. Icons are painted at runtime, so the application
ships no binary assets."""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap

from ..core.state import GameState

GREEN = "#2e9d57"
YELLOW = "#d9a21b"
RED = "#d64545"
GREY = "#9aa0a6"
BLUE = "#3b7dd8"

STATE_LABELS = {
    GameState.SYNCED: "Synchronized",
    GameState.LOCAL_NEWER: "PC → USB",
    GameState.USB_NEWER: "USB → PC",
    GameState.CONFLICT: "Conflict",
    GameState.MISSING_LOCAL_PATH: "Pending configuration",
    GameState.RUNNING: "Game running",
    GameState.ERROR: "Error",
    GameState.UNKNOWN: "Unknown",
    GameState.FIRST_SYNC: "First synchronization",
}

STATE_COLORS = {
    GameState.SYNCED: GREEN,
    GameState.LOCAL_NEWER: YELLOW,
    GameState.USB_NEWER: YELLOW,
    GameState.CONFLICT: RED,
    GameState.MISSING_LOCAL_PATH: GREY,
    GameState.RUNNING: BLUE,
    GameState.ERROR: RED,
    GameState.UNKNOWN: GREY,
    GameState.FIRST_SYNC: BLUE,
}

STATE_GLYPHS = {
    GameState.SYNCED: "✓",
    GameState.LOCAL_NEWER: "↑",
    GameState.USB_NEWER: "↓",
    GameState.CONFLICT: "!",
    GameState.MISSING_LOCAL_PATH: "?",
    GameState.RUNNING: "▶",
    GameState.ERROR: "✕",
    GameState.UNKNOWN: "?",
    GameState.FIRST_SYNC: "1",
}

# Main-window categories (plan §19.2): name → states shown
CATEGORIES = [
    ("All", None),
    ("Synchronized", {GameState.SYNCED}),
    ("PC → USB", {GameState.LOCAL_NEWER}),
    ("USB → PC", {GameState.USB_NEWER}),
    ("Conflicts", {GameState.CONFLICT}),
    ("Pending", {GameState.FIRST_SYNC, GameState.MISSING_LOCAL_PATH, GameState.RUNNING,
                 GameState.UNKNOWN}),
    ("Errors", {GameState.ERROR}),
]

TRAY_COLORS = {"green": GREEN, "yellow": YELLOW, "red": RED, "white": "#f1f3f4"}
TRAY_TOOLTIPS = {
    "green": "Save Sync — USB connected, everything synchronized",
    "yellow": "Save Sync — USB connected, pending changes",
    "red": "Save Sync — conflict or error",
    "white": "Save Sync — USB not connected",
}

_cache = {}


def _disc(color: str, glyph: str = "", size: int = 64, ring: bool = False) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    margin = size * 0.06
    rect = QRectF(margin, margin, size - 2 * margin, size - 2 * margin)
    painter.setPen(QPen(QColor("#5f6368"), size * 0.06) if ring else Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    painter.drawEllipse(rect)
    if glyph:
        font = QFont()
        font.setBold(True)
        font.setPixelSize(int(size * 0.55))
        painter.setFont(font)
        painter.setPen(QColor("#202124" if ring else "white"))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, glyph)
    painter.end()
    return pixmap


def state_icon(state: GameState) -> QIcon:
    key = ("state", state)
    if key not in _cache:
        _cache[key] = QIcon(_disc(STATE_COLORS[state], STATE_GLYPHS[state]))
    return _cache[key]


def tray_icon(tray_state: str) -> QIcon:
    key = ("tray", tray_state)
    if key not in _cache:
        _cache[key] = QIcon(_usb_pixmap(TRAY_COLORS[tray_state], ring=tray_state == "white"))
    return _cache[key]


def _usb_pixmap(color: str, size: int = 64, ring: bool = False) -> QPixmap:
    """A memory-card silhouette in the state color."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    s = size
    path.moveTo(s * 0.22, s * 0.08)
    path.lineTo(s * 0.64, s * 0.08)
    path.lineTo(s * 0.80, s * 0.24)
    path.lineTo(s * 0.80, s * 0.92)
    path.lineTo(s * 0.22, s * 0.92)
    path.closeSubpath()
    painter.setPen(QPen(QColor("#5f6368"), s * 0.05))
    painter.setBrush(QColor(color))
    painter.drawPath(path)
    painter.setPen(QPen(QColor("#202124" if ring else "white"), s * 0.05))
    for i in range(4):
        x = s * (0.32 + i * 0.1)
        painter.drawLine(int(x), int(s * 0.16), int(x), int(s * 0.30))
    painter.end()
    return pixmap


def app_icon() -> QIcon:
    key = ("app",)
    if key not in _cache:
        _cache[key] = QIcon(_usb_pixmap(GREEN))
    return _cache[key]


def when_text(ts) -> str:
    if not ts:
        return "never"
    import time
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return "?"


def iso_text(when: str) -> str:
    """`2026-09-27T10:56:16.59Z` → local `2026-09-27 12:56`."""
    if not when:
        return "—"
    import calendar
    import time
    try:
        base = when[:19]
        stamp = calendar.timegm(time.strptime(base, "%Y-%m-%dT%H:%M:%S"))
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp))
    except ValueError:
        return when
