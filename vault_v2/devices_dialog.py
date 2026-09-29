"""Phone… — the devices that may open this vault from outside the PC.

Each phone or browser has its own key, so a lost one can be shut out without
the others. Adding one is a QR on this screen plus one look: the device and
this window show the same six digits, and the owner confirms here. The PC is
the only place devices are added, renamed or revoked.

Contract: docs/CONTRACT-devices-pin-export-setup.md §1.
"""

from __future__ import annotations

import time
from datetime import datetime
from urllib.parse import quote

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .devices import LEGACY_ID, Device, DeviceRegistry, PairingError
from .errors import VaultBusy, VaultWriteBlocked
from .phone_dialog import qr_pixmap

WRITE_ERRORS = (VaultBusy, VaultWriteBlocked, PairingError, OSError)


def pairing_link(base_url: str, ticket: str, kind: str) -> str:
    """What the QR carries: a one-time ticket for this vault, never a key."""
    base = base_url.rstrip("/") + "/"
    if kind == "browser":
        return f"{base}#pair={ticket}"
    return f"vault-pair://pair?u={quote(base, safe='')}&t={ticket}"


def _when(iso: str) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


def _seen(ts: float | None) -> str:
    if ts is None:
        return "not since the window opened"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


class PairDialog(QDialog):
    """One pairing attempt: QR, then the device's name and code, then Confirm or Reject."""

    def __init__(self, registry: DeviceRegistry, base_url: str, kind: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.registry = registry
        self.kind = kind
        self.paired: Device | None = None
        self.used = False
        self._approved_at = 0.0
        self.ticket = registry.new_ticket()
        self._claim: dict | None = None
        self.setWindowTitle("Add a phone" if kind == "app" else "Add a browser")
        self.setMinimumWidth(460)

        link = pairing_link(base_url, self.ticket, kind)
        head = QLabel("Scan with the phone's camera" if kind == "app" else "Open on the phone's browser")
        head.setObjectName("chatTitle")
        how = QLabel(
            "Tailscale must be on on the phone. The camera offers to open Vault; the app then "
            "shows six digits — they must match the ones that appear here."
            if kind == "app" else
            "Scan with the camera and open the link in the browser (Tailscale on). The page shows "
            "six digits — they must match the ones that appear here."
        )
        how.setObjectName("chatSub")
        how.setWordWrap(True)
        self.qr = QLabel()
        pm = qr_pixmap(link)
        if pm is None:
            self.qr.setText("(install qrcode to see a QR)")
        else:
            self.qr.setPixmap(pm)
        self.qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        link_box = QLineEdit(link)
        link_box.setReadOnly(True)
        link_box.setToolTip("A one-time pairing code, valid five minutes. It is not the vault key.")

        self.state = QLabel("Waiting for the device… (this code is valid for five minutes)")
        self.state.setObjectName("chatSub")
        self.state.setWordWrap(True)
        self.code = QLabel("")
        self.code.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.code.setStyleSheet("font-size: 26pt; font-weight: 600; letter-spacing: 4px;")

        self.confirm_btn = QPushButton("Confirm — the digits match")
        self.confirm_btn.setObjectName("sendBtn")
        self.confirm_btn.setEnabled(False)
        self.confirm_btn.clicked.connect(self._confirm)
        self.reject_btn = QPushButton("Reject")
        self.reject_btn.setObjectName("ghostBtn")
        self.reject_btn.setEnabled(False)
        self.reject_btn.clicked.connect(self._reject)
        cancel = QPushButton("Close")
        cancel.setObjectName("ghostBtn")
        cancel.clicked.connect(self.reject)

        row = QHBoxLayout()
        row.addWidget(self.reject_btn)
        row.addStretch(1)
        row.addWidget(cancel)
        row.addWidget(self.confirm_btn)

        lay = QVBoxLayout(self)
        lay.addWidget(head)
        lay.addWidget(how)
        lay.addWidget(self.qr)
        lay.addWidget(link_box)
        lay.addWidget(self.state)
        lay.addWidget(self.code)
        lay.addLayout(row)

        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._poll)
        self.timer.start()

    def _poll(self) -> None:
        claim = self.registry.waiting_claim(self.ticket)
        if claim is None:
            if self._claim is not None or not self.registry.ticket_alive(self.ticket):
                if self.paired is None:
                    self.state.setText("This pairing code has expired. Close and press Add again.")
                    self.confirm_btn.setEnabled(False)
                    self.reject_btn.setEnabled(False)
                    self.timer.stop()
            return
        if claim["state"] != "waiting":
            return
        if self._claim is None:
            self._claim = claim
            self.qr.hide()
            what = "phone" if claim["kind"] == "app" else "browser"
            self.state.setText(f"«{claim['name']}» ({what}) wants to open this vault. "
                               "Confirm only if the device shows the same digits.")
            self.code.setText(f"{claim['code'][:3]} {claim['code'][3:]}")
            self.confirm_btn.setEnabled(True)
            self.reject_btn.setEnabled(True)

    def _confirm(self) -> None:
        if self._claim is None:
            return
        try:
            self.paired = self.registry.confirm(self._claim["claim_id"])
        except WRITE_ERRORS as exc:
            QMessageBox.warning(self, "Pairing", str(exc))
            return
        self.confirm_btn.setEnabled(False)
        self.reject_btn.setEnabled(False)
        self.state.setText("Approved. Waiting for the device to collect its key and open the vault…")
        self._approved_at = time.monotonic()
        self.timer.timeout.disconnect(self._poll)
        self.timer.timeout.connect(self._watch_first_use)

    def _watch_first_use(self) -> None:
        """Paired means the device has used its key once, not only that Confirm was pressed."""
        if self.paired is None:
            return
        if self.paired.id in self.registry.last_seen:
            self.used = True
            self.state.setText(f"«{self.paired.name}» is paired and has opened the vault.")
            self.code.setText("✓")
            self.timer.stop()
            return
        if time.monotonic() - self._approved_at > 5 * 60:
            self.state.setText("The device did not collect its key. Revoke it in the list and add it again.")
            self.timer.stop()

    def _reject(self) -> None:
        if self._claim is not None:
            self.registry.reject(self._claim["claim_id"])
        self.timer.stop()
        self.reject()

    def done(self, result: int) -> None:  # noqa: D401 - Qt override
        self.timer.stop()
        if self.paired is None:
            self.registry.cancel_ticket(self.ticket)
        super().done(result)


class DevicesDialog(QDialog):
    stop_requested = Signal()

    def __init__(self, url: str, registry: DeviceRegistry, reachable: bool, parent: QWidget | None = None):
        super().__init__(parent)
        self.url, self.registry = url, registry
        self.setWindowTitle("Phone and other devices")
        self.setMinimumSize(720, 460)

        head = QLabel("Devices that may open this vault")
        head.setObjectName("chatTitle")
        sub = QLabel(
            "Only inside your own Tailscale network. Each device has its own key; revoking one "
            "leaves the others working. Revoking cannot take back files already on a device."
            if reachable else
            "Tailscale is not up, so devices can only reach the vault from this PC. "
            "Start Tailscale and press Phone… again."
        )
        sub.setObjectName("chatSub")
        sub.setWordWrap(True)

        url_box = QLineEdit(url)
        url_box.setReadOnly(True)
        copy_url = QPushButton("Copy address")
        copy_url.setObjectName("ghostBtn")
        copy_url.clicked.connect(lambda: QGuiApplication.clipboard().setText(url))
        stop = QPushButton("Stop serving")
        stop.setObjectName("ghostBtn")
        stop.setToolTip("Close the door until the vault window is opened again")
        stop.clicked.connect(self._stop)
        addr = QHBoxLayout()
        addr.addWidget(url_box, 1)
        addr.addWidget(copy_url)
        addr.addWidget(stop)

        self.legacy_note = QLabel(
            "The old shared key still opens the vault. Add your phone and browser again with the "
            "buttons below, check they work, then revoke «Key shared by devices…»."
        )
        self.legacy_note.setObjectName("chatSub")
        self.legacy_note.setWordWrap(True)
        self.legacy_note.setStyleSheet("color: #b4542d;")

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Name", "Kind", "Paired", "Last seen", "Status"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()

        add_phone = QPushButton("Add phone…")
        add_phone.setObjectName("sendBtn")
        add_phone.clicked.connect(lambda: self._add("app"))
        add_browser = QPushButton("Add browser…")
        add_browser.setObjectName("ghostBtn")
        add_browser.clicked.connect(lambda: self._add("browser"))
        self.rename_btn = QPushButton("Rename…")
        self.rename_btn.setObjectName("ghostBtn")
        self.rename_btn.clicked.connect(self._rename)
        self.revoke_btn = QPushButton("Revoke")
        self.revoke_btn.setObjectName("ghostBtn")
        self.revoke_btn.clicked.connect(self._revoke)
        revoke_all = QPushButton("Revoke all")
        revoke_all.setObjectName("ghostBtn")
        revoke_all.setToolTip("Emergency: shut out every device at once")
        revoke_all.clicked.connect(self._revoke_all)
        close = QPushButton("Close")
        close.setObjectName("ghostBtn")
        close.clicked.connect(self.accept)

        buttons = QHBoxLayout()
        buttons.addWidget(add_phone)
        buttons.addWidget(add_browser)
        buttons.addWidget(self.rename_btn)
        buttons.addWidget(self.revoke_btn)
        buttons.addStretch(1)
        buttons.addWidget(revoke_all)
        buttons.addWidget(close)

        lay = QVBoxLayout(self)
        lay.setSpacing(8)
        lay.addWidget(head)
        lay.addWidget(sub)
        lay.addLayout(addr)
        lay.addWidget(self.legacy_note)
        lay.addWidget(self.table, 1)
        lay.addLayout(buttons)

        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.refresh()

    # -- view ---------------------------------------------------------------------

    def refresh(self) -> None:
        devices = self.registry.all()
        self.devices = devices
        self.table.setRowCount(len(devices))
        for row, d in enumerate(devices):
            kind = {"app": "Phone app", "browser": "Browser", "legacy": "Shared key"}.get(d.kind, d.kind)
            door = self.registry.door_owner() == d.id
            status = "Revoked " + _when(d.revoked_at) if d.revoked_at else ("Active · agent door" if door else "Active")
            cells = [d.name, kind, _when(d.paired_at), _seen(self.registry.last_seen.get(d.id)), status]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if d.revoked_at:
                    item.setForeground(Qt.GlobalColor.gray)
                self.table.setItem(row, col, item)
        self.legacy_note.setVisible(any(d.id == LEGACY_ID for d in devices))
        self._selection_changed()

    def selected(self) -> Device | None:
        rows = self.table.selectionModel().selectedRows()
        return self.devices[rows[0].row()] if rows else None

    def _selection_changed(self) -> None:
        d = self.selected()
        self.revoke_btn.setEnabled(d is not None and d.active)
        self.rename_btn.setEnabled(d is not None and d.active and d.id != LEGACY_ID)

    # -- actions ------------------------------------------------------------------

    def _add(self, kind: str) -> None:
        dlg = PairDialog(self.registry, self.url, kind, self)
        dlg.exec()
        self.refresh()
        if dlg.paired is not None and not dlg.used:
            QMessageBox.information(self, "Pairing",
                                    f"«{dlg.paired.name}» was approved but has not opened the vault yet. "
                                    "If it never does, revoke it and add it again.")

    def _rename(self) -> None:
        d = self.selected()
        if d is None:
            return
        name, ok = QInputDialog.getText(self, "Rename device", "Name:", text=d.name)
        if ok and name.strip():
            try:
                self.registry.rename(d.id, name)
            except WRITE_ERRORS as exc:
                QMessageBox.warning(self, "Rename", str(exc))
            self.refresh()

    def _revoke(self) -> None:
        d = self.selected()
        if d is None or not d.active:
            return
        what = ("Every device still using the old shared key — including old QR links — will be shut out."
                if d.id == LEGACY_ID else f"«{d.name}» will be shut out from its next request on.")
        if QMessageBox.question(self, "Revoke", what + "\n\nFiles already on it stay there. Revoke?") \
                != QMessageBox.StandardButton.Yes:
            return
        try:
            self.registry.revoke(d.id)
        except WRITE_ERRORS as exc:
            QMessageBox.warning(self, "Revoke", str(exc))
        self.refresh()

    def _revoke_all(self) -> None:
        if QMessageBox.question(self, "Revoke all",
                                "Every device, including the old shared key, will be shut out. "
                                "You will pair them again from here.\n\nRevoke all?") \
                != QMessageBox.StandardButton.Yes:
            return
        try:
            n = self.registry.revoke_all()
        except WRITE_ERRORS as exc:
            QMessageBox.warning(self, "Revoke all", str(exc))
            n = None
        self.refresh()
        if n is not None:
            QMessageBox.information(self, "Revoke all", f"{n} device(s) shut out.")

    def _stop(self) -> None:
        self.stop_requested.emit()
        self.accept()
