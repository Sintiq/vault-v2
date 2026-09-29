"""Export dialog — preview → explicit approval → write → verified result."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .gatekeeper import ExportRequest, ExportResult, GateError, Gatekeeper


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _default_destination() -> Path:
    desktop = Path.home() / "Desktop"
    base = desktop if desktop.is_dir() else Path.home()
    return base / f"Vault export {datetime.now().strftime('%Y-%m-%d %H-%M')}"


def _open_in_explorer(path: Path) -> None:
    target = path if path.is_dir() else path.parent
    if sys.platform.startswith("win"):
        os.startfile(str(target))  # noqa: S606 - opening a folder the owner just approved
    else:
        subprocess.Popen(["xdg-open", str(target)])


class ExportDialog(QDialog):
    def __init__(self, gate: Gatekeeper, sources: list[Path], parent: QWidget | None = None):
        super().__init__(parent)
        self.gate = gate
        self.sources = sources
        self.request: ExportRequest | None = None
        self.result: ExportResult | None = None
        self.setWindowTitle("Export from Staging")
        self.resize(820, 520)

        self.head = QLabel("")
        self.head.setObjectName("chatTitle")
        self.sub = QLabel("Exactly these bytes leave the vault. Nothing else.")
        self.sub.setObjectName("paneSub")

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["File", "Size", "SHA-256"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)

        self.dest = QLineEdit(str(_default_destination()))
        browse = QPushButton("Browse…")
        browse.setObjectName("ghostBtn")
        browse.clicked.connect(self._browse)
        self.r_folder = QRadioButton("Folder")
        self.r_zip = QRadioButton("ZIP")
        self.r_folder.setChecked(True)
        self.r_folder.toggled.connect(self._rebuild)

        dest_row = QHBoxLayout()
        dest_row.addWidget(QLabel("Destination:"))
        dest_row.addWidget(self.dest, 1)
        dest_row.addWidget(browse)
        dest_row.addWidget(self.r_folder)
        dest_row.addWidget(self.r_zip)

        self.status = QLabel("")
        self.status.setObjectName("paneSub")
        self.status.setWordWrap(True)

        self.approve_btn = QPushButton("Approve and write")
        self.approve_btn.setObjectName("sendBtn")
        self.approve_btn.clicked.connect(self._approve_and_write)
        self.open_btn = QPushButton("Open folder")
        self.open_btn.setObjectName("ghostBtn")
        self.open_btn.clicked.connect(self._open)
        self.open_btn.hide()
        self.close_btn = QPushButton("Cancel")
        self.close_btn.setObjectName("ghostBtn")
        self.close_btn.clicked.connect(self.reject)

        btns = QHBoxLayout()
        btns.addWidget(self.approve_btn)
        btns.addWidget(self.open_btn)
        btns.addStretch(1)
        btns.addWidget(self.close_btn)

        lay = QVBoxLayout(self)
        lay.addWidget(self.head)
        lay.addWidget(self.sub)
        lay.addWidget(self.table, 1)
        lay.addLayout(dest_row)
        lay.addWidget(self.status)
        lay.addLayout(btns)

        self.dest.textChanged.connect(self._rebuild)
        self._rebuild()

    # -- preview --------------------------------------------------------------

    def _mode(self) -> str:
        return "zip" if self.r_zip.isChecked() else "folder"

    def _rebuild(self) -> None:
        try:
            self.request = self.gate.prepare(self.sources, Path(self.dest.text().strip()), self._mode())
        except GateError as exc:
            self.request = None
            self.head.setText("Cannot export")
            self.status.setText(str(exc))
            self.table.setRowCount(0)
            self.approve_btn.setEnabled(False)
            return
        req = self.request
        self.head.setText(f"{len(req.items)} file(s), {_human(req.total_size)} → {req.mode}")
        self.table.setRowCount(len(req.items))
        for r, it in enumerate(req.items):
            self.table.setItem(r, 0, QTableWidgetItem(it.rel))
            self.table.setItem(r, 1, QTableWidgetItem(_human(it.size)))
            self.table.setItem(r, 2, QTableWidgetItem(it.sha256[:16] + "…"))
        self.status.setText(f"snapshot {req.digest[:16]}… · approval is one-use and expires in 3 minutes")
        self.approve_btn.setEnabled(True)

    def _browse(self) -> None:
        if self._mode() == "zip":
            path, _ = QFileDialog.getSaveFileName(self, "Save ZIP as", self.dest.text(), "ZIP archive (*.zip)")
        else:
            path = QFileDialog.getExistingDirectory(self, "Choose an empty destination folder", str(Path(self.dest.text()).parent))
        if path:
            self.dest.setText(path)

    # -- approve + write ------------------------------------------------------

    def _approve_and_write(self) -> None:
        if self.request is None:
            return
        self.approve_btn.setEnabled(False)
        self.gate.approve(self.request)
        try:
            self.result = self.gate.execute(self.request)
        except (GateError, OSError) as exc:
            self.status.setText(f"REFUSED: {exc}")
            self.approve_btn.setEnabled(True)
            return
        res = self.result
        for w in (self.dest, self.r_folder, self.r_zip):
            w.setEnabled(False)
        if res.verified:
            self.head.setText(f"VERIFIED — {len(res.written)} file(s) written")
            self.status.setText(f"{res.destination}\nEvery file hashed after writing and matched the preview. Receipts recorded.")
        else:
            self.head.setText("WRITTEN WITH PROBLEMS")
            self.status.setText(f"{res.destination}\n" + "\n".join(res.problems))
        self.open_btn.show()
        self.close_btn.setText("Close")
        self.close_btn.clicked.disconnect()
        self.close_btn.clicked.connect(self.accept)

    def _open(self) -> None:
        if self.result is not None:
            _open_in_explorer(self.result.destination)
