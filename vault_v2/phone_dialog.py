"""A QR code as a pixmap, for the pairing dialog (devices_dialog.py).

The old Phone dialog that showed the shared key is gone: a device now pairs
with a one-time ticket and gets a key of its own.
"""

from __future__ import annotations

import io

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap


def qr_pixmap(text: str, size: int = 300) -> QPixmap | None:
    try:
        import qrcode
    except ImportError:
        return None
    img = qrcode.make(text, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    pm = QPixmap()
    pm.loadFromData(buf.getvalue(), "PNG")
    return pm.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                     Qt.TransformationMode.SmoothTransformation)
