"""Toolbar icons drawn from Windows' own icon font.

Windows 11 ships "Segoe Fluent Icons" (and its predecessor "Segoe MDL2
Assets"), the font the Explorer and Settings draw their glyphs from. Taking
our toolbar icons from it means the vault window looks like the windows next
to it, in both themes, without shipping a single image. On a machine without
the font the buttons simply keep their text.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QFontMetrics, QGuiApplication, QIcon, QPainter, QPixmap

FONTS = ("Segoe Fluent Icons", "Segoe MDL2 Assets")

# Names we use -> code points, identical in both fonts.
GLYPHS: dict[str, int] = {
    "upload": 0xE898,    # Upload
    "export": 0xEDE1,    # Export
    "copy": 0xE8C8,      # Copy
    "move": 0xE8DE,      # MoveToFolder
    "folder": 0xE8F4,    # NewFolder
    "delete": 0xE74D,    # Delete
    "sort": 0xE8CB,      # Sort
    "ask": 0xE721,       # Search
    "broom": 0xEA99,     # Broom
    "restore": 0xE7A7,   # Undo — the Trash view is where things come back from
    "save": 0xE74E,      # Save
    "phone": 0xE8EA,     # CellPhone
    "setup": 0xE713,     # Setting
    "receipts": 0xE8FD,  # BulletedList
    "pending": 0xE823,   # Recent
    "moon": 0xE708,      # QuietHours
    "sun": 0xE706,       # Brightness
}

ICON_PX = 16


def icon_font() -> str | None:
    """The first Windows icon font present, or None off Windows (or before there is an application:
    Qt's font database cannot be asked without one, and asking anyway kills the process)."""
    if QGuiApplication.instance() is None:
        return None
    families = set(QFontDatabase.families())
    return next((f for f in FONTS if f in families), None)


def glyph_icon(name: str, color: str, px: int = ICON_PX) -> QIcon:
    """One glyph as an icon in the given colour; an empty icon when the font is missing."""
    family = icon_font()
    code = GLYPHS.get(name)
    if family is None or code is None:
        return QIcon()
    if not QFontMetrics(QFont(family)).inFontUcs4(code):
        return QIcon()
    scale = 2  # crisp on HiDPI: draw at twice the size and tell Qt so
    pixmap = QPixmap(px * scale, px * scale)
    pixmap.setDevicePixelRatio(scale)
    pixmap.fill(Qt.GlobalColor.transparent)
    font = QFont(family)
    font.setPixelSize(px)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setFont(font)
        painter.setPen(QColor(color))
        painter.drawText(QRectF(0, 0, px, px), Qt.AlignmentFlag.AlignCenter, chr(code))
    finally:
        painter.end()
    return QIcon(pixmap)
