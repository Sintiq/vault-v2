"""Trash dialog — recover healthy items and keep damaged entries visible."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from .ops import VaultError, VaultOps
from .paths import PANE_NAMES


class TrashDialog(QDialog):
    def __init__(self, ops: VaultOps, parent=None):
        super().__init__(parent)
        self.ops = ops
        self.restored = 0
        self._summary: str | None = None
        self.setWindowTitle("Trash")
        self.resize(720, 420)

        self.info = QLabel("")
        self.info.setObjectName("paneSub")
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)

        self.restore_btn = QPushButton("Restore selected")
        self.restore_btn.setObjectName("sendBtn")
        self.restore_btn.clicked.connect(self._restore_selected)
        self.list.itemSelectionChanged.connect(self._selection_changed)
        self.purge_btn = QPushButton("Empty trash")
        self.purge_btn.setObjectName("ghostBtn")
        self.purge_btn.clicked.connect(self._purge)
        close_btn = QPushButton("Close")
        close_btn.setObjectName("ghostBtn")
        close_btn.clicked.connect(self.accept)

        row = QHBoxLayout()
        row.addWidget(self.restore_btn)
        row.addWidget(self.purge_btn)
        row.addStretch(1)
        row.addWidget(close_btn)

        lay = QVBoxLayout(self)
        lay.addWidget(self.info)
        lay.addWidget(self.list, 1)
        lay.addLayout(row)
        self._reload()

    def _reload(self) -> None:
        self.list.clear()
        try:
            items = self.ops.list_trash()
        except (VaultError, OSError) as exc:
            previous = f"{self._summary} · " if self._summary else ""
            self.info.setText(f"{previous}Cannot read Trash: {exc}")
            self.restore_btn.setEnabled(False)
            self.purge_btn.setEnabled(False)
            return
        for it in reversed(items):  # newest first
            error = it.get("error", "")
            if error:
                text = f"{Path(it['slot']).name}    —  {error} · kept for review"
            else:
                origin = Path(it["origin"])
                # Rendering metadata must not follow an old path or probe a
                # legacy UNC share. Restore performs its own authorization.
                try:
                    relative = origin.relative_to(self.ops.paths.root.resolve())
                except ValueError:
                    relative = None
                if relative is not None and len(relative.parts) >= 2 and relative.parts[0] in PANE_NAMES:
                    pane = relative.parts[0]
                    folder = Path(*relative.parts[1:]).parent
                else:
                    pane, folder = "?", origin.parent
                when = it["trashed_at"]
                text = f"{origin.name}    ←  {pane}/{folder}    {when[:8]} {when[9:15]}"
            li = QListWidgetItem(text)
            li.setData(Qt.ItemDataRole.UserRole, it["slot"])
            li.setData(Qt.ItemDataRole.UserRole + 1, error)
            self.list.addItem(li)
        self.info.setText(self._summary or f"in Trash: {len(items)} · nothing is ever erased automatically")
        self._selection_changed()
        self.purge_btn.setEnabled(bool(items))

    def _selection_changed(self) -> None:
        selected = self.list.selectedItems()
        self.restore_btn.setEnabled(bool(selected) and not any(
            item.data(Qt.ItemDataRole.UserRole + 1) for item in selected))

    def _purge(self) -> None:
        answer = QMessageBox.question(
            self, "Empty trash",
            "Permanently delete all items with a valid manifest in Trash? This cannot be undone. "
            "Damaged entries and files without a manifest will be kept.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            result = self.ops.purge_trash(note="owner confirmed Empty trash in the dialog")
            self._summary = f"{result.removed} removed, {result.skipped} damaged kept"
            if result.cache_deferred:
                self._summary += " · text cache cleanup deferred — manual review needed"
        except (VaultError, OSError) as exc:
            self._summary = f"Empty trash failed: {exc}"
            QMessageBox.warning(self, "Trash", self._summary)
        self._reload()

    def _restore_selected(self) -> None:
        sel = self.list.selectedItems()
        if not sel:
            QMessageBox.information(self, "Trash", "Select what to restore.")
            return
        if any(item.data(Qt.ItemDataRole.UserRole + 1) for item in sel):
            return
        n, failed = 0, []
        for li in sel:
            try:
                self.ops.restore(Path(li.data(Qt.ItemDataRole.UserRole)))
                n += 1
            except (VaultError, OSError) as exc:
                failed.append(str(exc))
        self.restored += n
        self._summary = None
        if failed:
            QMessageBox.warning(self, "Trash", "\n".join(failed))
        self._reload()
