"""Read-only owner previews. No agent/backend is involved in viewing a file."""
from pathlib import Path
from datetime import datetime
from threading import Event

from PySide6.QtCore import Qt, QThread
from PySide6.QtGui import QImageReader, QPixmap
from PySide6.QtWidgets import (QDialog, QGraphicsScene, QGraphicsView, QHBoxLayout,
                               QLabel, QPlainTextEdit, QPushButton, QVBoxLayout)

from .cards import CardStore, content_kind
from .errors import VaultError
from .file_access import visible_file
from .paths import VaultPaths
from .pdf_preview import render_pdf_page
from .receipts import sha256_file

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".avif"}
# A parentless viewer may lose its last caller reference immediately on close.
# Retain it until the thread's queued completion has run, never a blocking join.
_ACTIVE_PDF_VIEWERS = set()


class _PdfRenderThread(QThread):
    """Blocking process supervision runs here; only the GUI constructs pixmaps."""

    def __init__(self, paths, path, page, parent):
        super().__init__(parent)
        self.paths, self.path, self.page = paths, path, page
        self.result = None
        self.error = ""
        self.cancel = Event()

    def run(self):
        try:
            self.result = render_pdf_page(self.paths, self.path, self.page, cancel_event=self.cancel)
        except (VaultError, OSError) as exc:
            self.error = str(exc)
        except Exception:  # A malformed result must not strand the viewer.
            self.error = "This PDF page could not be shown."


class _ImageView(QGraphicsView):
    def __init__(self, pixmap, parent=None):
        super().__init__(parent)
        scene = QGraphicsScene(self)
        scene.addPixmap(pixmap)
        self.setScene(scene)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.fit = True

    def set_image(self, pixmap):
        self.scene().clear()
        self.scene().addPixmap(pixmap)
        self.fit_image()

    def fit_image(self):
        self.fit = True
        self.fitInView(self.scene().itemsBoundingRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def zoom(self, factor):
        self.fit = False
        self.scale(factor, factor)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        if self.fit:
            self.fit_image()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        if self.fit:
            self.fit_image()


class FileViewer(QDialog):
    def __init__(self, paths: VaultPaths, path: Path, cards: CardStore | None = None, parent=None):
        super().__init__(parent)
        self.path = visible_file(paths, path)
        self.setWindowTitle(self.path.name)
        self.resize(900, 700)
        layout = QVBoxLayout(self)
        if content_kind(self.path) == "TEXT":
            text = QPlainTextEdit(self)
            text.setReadOnly(True)
            text.setPlainText(self.path.read_text(encoding="utf-8", errors="replace"))
            layout.addWidget(text)
        elif self.path.suffix.lower() in IMAGE_SUFFIXES:
            reader = QImageReader(str(self.path))
            reader.setAutoTransform(True)
            image = reader.read()
            if image.isNull():
                layout.addWidget(QLabel("This image cannot be decoded here.", self))
            else:
                picture = _ImageView(QPixmap.fromImage(image), self)
                layout.addWidget(picture, 1)
                controls = QHBoxLayout()
                for label, callback in (("Zoom in", lambda: picture.zoom(1.25)),
                                        ("Zoom out", lambda: picture.zoom(0.8)),
                                        ("Fit to window", picture.fit_image)):
                    button = QPushButton(label, self)
                    button.clicked.connect(callback)
                    controls.addWidget(button)
                controls.addStretch()
                layout.addLayout(controls)
        elif self.path.suffix.lower() == ".pdf":
            self._show_pdf(paths, layout)
        else:
            layout.addWidget(QLabel("no preview for this file type", self))
            digest = sha256_file(self.path)
            info = self.path.stat()
            card = cards.get(digest) if cards is not None else None
            details = QLabel(self)
            details.setTextFormat(Qt.TextFormat.PlainText)
            details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            details.setText(
                f"Name: {self.path.name}\nSize: {info.st_size:,} bytes\n"
                f"Modified: {datetime.fromtimestamp(info.st_mtime):%Y-%m-%d %H:%M:%S}\n"
                f"Shelf: {card.shelf if card else 'not assigned'}\nSHA-256: {digest[:12]}"
            )
            layout.addWidget(details)
            layout.addStretch()

    def _show_pdf(self, paths, layout):
        self._pdf_paths = paths
        self._pdf_thread = None
        self._pdf_discarded = False
        self._pdf_delete_when_finished = False
        self._pdf_page_index = 0
        self._pdf_page_count = 0
        self._pdf_warning = QLabel(self)
        self._pdf_warning.setTextFormat(Qt.TextFormat.PlainText)
        self._pdf_warning.setWordWrap(True)
        layout.addWidget(self._pdf_warning)
        self._pdf_picture = _ImageView(QPixmap(), self)
        layout.addWidget(self._pdf_picture, 1)
        zoom_controls = QHBoxLayout()
        self._pdf_zoom_buttons = []
        for label, callback in (("Zoom in", lambda: self._pdf_picture.zoom(1.25)),
                                ("Zoom out", lambda: self._pdf_picture.zoom(0.8)),
                                ("Fit to window", self._pdf_picture.fit_image)):
            button = QPushButton(label, self)
            button.clicked.connect(callback)
            zoom_controls.addWidget(button)
            self._pdf_zoom_buttons.append(button)
        zoom_controls.addStretch()
        layout.addLayout(zoom_controls)
        controls = QHBoxLayout()
        self._pdf_previous = QPushButton("Previous page", self)
        self._pdf_following = QPushButton("Next page", self)
        self._pdf_page_label = QLabel(self)
        self._pdf_previous.clicked.connect(lambda: self._turn_pdf(-1))
        self._pdf_following.clicked.connect(lambda: self._turn_pdf(1))
        for widget in (self._pdf_previous, self._pdf_page_label, self._pdf_following):
            controls.addWidget(widget)
        controls.addStretch()
        layout.addLayout(controls)
        self._start_pdf(0)

    def _update_pdf_controls(self):
        busy = self._pdf_thread is not None
        self._pdf_previous.setEnabled(not busy and self._pdf_page_index > 0)
        self._pdf_following.setEnabled(not busy and self._pdf_page_index + 1 < self._pdf_page_count)
        for button in self._pdf_zoom_buttons:
            button.setEnabled(not busy and self._pdf_page_count > 0)
        self._pdf_page_label.setText(
            f"Page {self._pdf_page_index + 1} of {self._pdf_page_count}"
            if self._pdf_page_count else "No page shown")

    def _turn_pdf(self, delta):
        page = self._pdf_page_index + delta
        if self._pdf_thread is None and 0 <= page < self._pdf_page_count:
            self._start_pdf(page)

    def _start_pdf(self, page):
        if self._pdf_discarded:
            return
        self._pdf_warning.setText("rendering…")
        self._pdf_warning.show()
        self._pdf_thread = _PdfRenderThread(self._pdf_paths, self.path, page, self)
        self._pdf_thread.finished.connect(self._pdf_finished)
        _ACTIVE_PDF_VIEWERS.add(self)
        self._update_pdf_controls()
        self._pdf_thread.start()

    def _pdf_finished(self):
        worker = self._pdf_thread
        self._pdf_thread = None
        result, error = worker.result, worker.error
        worker.deleteLater()
        _ACTIVE_PDF_VIEWERS.discard(self)
        if self._pdf_discarded:
            if self._pdf_delete_when_finished:
                self.deleteLater()
            return
        if result is not None:
            pixmap = QPixmap()
            if pixmap.loadFromData(result.png, "PNG"):
                self._pdf_picture.set_image(pixmap)
                self._pdf_page_index, self._pdf_page_count = result.page_index, result.page_count
                self._pdf_warning.setText(result.warning)
            else:
                self._pdf_warning.setText("This PDF page could not be shown.")
        else:
            self._pdf_warning.setText(error or "This PDF page could not be shown.")
        self._pdf_warning.setVisible(bool(self._pdf_warning.text()))
        self._update_pdf_controls()

    def _discard_pdf(self):
        if not hasattr(self, "_pdf_thread"):
            return
        self._pdf_discarded = True
        if self._pdf_thread is not None:
            # Pane previews request deletion on close. Keep the QObject parent
            # alive until supervision has killed/reaped its child and returned.
            self._pdf_delete_when_finished |= self.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
            self._pdf_thread.cancel.set()

    def closeEvent(self, event):  # noqa: N802
        self._discard_pdf()
        super().closeEvent(event)

    def done(self, result):
        self._discard_pdf()
        super().done(result)
