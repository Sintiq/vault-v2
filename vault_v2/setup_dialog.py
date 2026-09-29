"""Setup… — the wizard's window: five facts, one button that changes something.

Checks and the one change both run off the window's thread (Tailscale can take
seconds to answer); while one runs, the buttons wait and a second press does
nothing. Checks repeat every few seconds while the window is open, so a step
turns green by itself when the owner finishes it elsewhere.

Contract: docs/CONTRACT-devices-pin-export-setup.md §5.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QTimer, QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .phone_dialog import qr_pixmap
from .setup import DONE, NEEDS_YOU, NOT_STARTED, UNKNOWN, Setup, SetupConflict, Step

MARK = {DONE: "✓", NEEDS_YOU: "→", UNKNOWN: "?", NOT_STARTED: "·"}
COLOUR = {DONE: "#2e9e55", NEEDS_YOU: "#d97757", UNKNOWN: "#b08a2e", NOT_STARTED: "#9b948a"}


class SetupDialog(QDialog):
    def __init__(self, setup: Setup, *, on_add_phone, on_served, on_unserved, parent: QWidget | None = None):
        super().__init__(parent)
        self.setup = setup
        self.on_add_phone, self.on_served, self.on_unserved = on_add_phone, on_served, on_unserved
        self.setWindowTitle("Setup")
        self.setMinimumSize(700, 520)
        self._job: str | None = None            # "check" | "apply" | "undo" while one runs
        self._outcome: tuple | None = None
        self._buttons: list[QPushButton] = []

        head = QLabel("Set up Vault for your phone")
        head.setObjectName("chatTitle")
        sub = QLabel("Each line turns green when it is true, not when a button is pressed. "
                     "Nothing is published: everything stays inside your own Tailscale network.")
        sub.setObjectName("chatSub")
        sub.setWordWrap(True)

        self.grid = QGridLayout()
        self.grid.setColumnStretch(1, 1)
        self.grid.setVerticalSpacing(12)
        self.status = QLabel("Checking…")
        self.status.setObjectName("chatSub")

        self.refresh_btn = QPushButton("Check again")
        self.refresh_btn.setObjectName("ghostBtn")
        self.refresh_btn.clicked.connect(self.check)
        self.undo_btn = QPushButton("Stop serving over HTTPS")
        self.undo_btn.setObjectName("ghostBtn")
        self.undo_btn.setToolTip("Remove the route this wizard added, if nothing about it changed since. "
                                 "Phones then cannot reach the vault; this PC still can.")
        self.undo_btn.clicked.connect(self._undo)
        self.undo_btn.setVisible(False)
        close = QPushButton("Close")
        close.setObjectName("sendBtn")
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addWidget(self.refresh_btn)
        row.addWidget(self.undo_btn)
        row.addStretch(1)
        row.addWidget(close)

        lay = QVBoxLayout(self)
        lay.addWidget(head)
        lay.addWidget(sub)
        lay.addLayout(self.grid)
        lay.addStretch(1)
        lay.addWidget(self.status)
        lay.addLayout(row)

        self.timer = QTimer(self)
        self.timer.setInterval(300)
        self.timer.timeout.connect(self._collect)
        self.timer.start()
        self.repeat = QTimer(self)
        self.repeat.setInterval(6000)
        self.repeat.timeout.connect(self.check)
        self.repeat.start()
        self.check()

    # -- work off the window's thread; one job at a time ------------------------------

    def _run(self, job: str, fn) -> bool:
        if self._job is not None:
            return False
        self._job = job
        self._set_busy(True)

        def work():
            try:
                self._outcome = (job, fn(), None)
            except Exception as exc:  # noqa: BLE001 — shown to the owner as it is
                self._outcome = (job, None, exc)

        threading.Thread(target=work, daemon=True).start()
        return True

    def _set_busy(self, busy: bool) -> None:
        self.refresh_btn.setEnabled(not busy)
        self.undo_btn.setEnabled(not busy)
        for b in self._buttons:
            b.setEnabled(not busy)
        if busy:
            self.status.setText({"check": "Checking…", "apply": "Asking Tailscale…",
                                 "undo": "Asking Tailscale to stop…"}.get(self._job, "Working…"))

    def _collect(self) -> None:
        if self._outcome is None:
            return
        job, value, error = self._outcome
        self._outcome = None
        self._job = None
        self._set_busy(False)
        if job == "check":
            if error is not None:
                self.status.setText(f"Could not check: {error}")
                return
            self.show_steps(value)
            self.status.setText(f"{sum(s.state == DONE for s in value)} of {len(value)} done")
            return
        if error is not None:
            QMessageBox.warning(self, "Setup", str(error))
        elif job == "apply" and value in ("applied", "already"):
            self.on_served(self.setup.last_name)
        elif job == "apply" and value == "removed":
            self.on_unserved()
        elif job == "undo" and value:
            self.on_unserved()
        self.check()

    def check(self) -> None:
        if self.setup.running is not None:
            self.status.setText("Tailscale is being asked (started earlier) — waiting for it…")
        self._run("check", self.setup.check)

    # -- the view ------------------------------------------------------------------------

    def show_steps(self, steps: list[Step]) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._buttons = []
        for row, step in enumerate(steps):
            mark = QLabel(MARK.get(step.state, "·"))
            mark.setStyleSheet(f"font-size: 16pt; color: {COLOUR.get(step.state, '#9b948a')};")
            mark.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
            text = QLabel(f"<b>{step.title}</b><br>{step.detail}" if step.detail else f"<b>{step.title}</b>")
            text.setWordWrap(True)
            text.setTextFormat(Qt.TextFormat.RichText)
            self.grid.addWidget(mark, row, 0)
            cell = QVBoxLayout()
            cell.addWidget(text)
            if step.qr:
                pm = qr_pixmap(step.qr, 180)
                if pm is not None:
                    code = QLabel()
                    code.setPixmap(pm)
                    code.setToolTip(step.qr)
                    cell.addWidget(code)
                link = QLabel(step.qr)
                link.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                link.setObjectName("chatSub")
                cell.addWidget(link)
            holder_text = QWidget()
            holder_text.setLayout(cell)
            self.grid.addWidget(holder_text, row, 1)
            buttons = QHBoxLayout()
            if step.link:
                open_btn = QPushButton("Open page")
                open_btn.setObjectName("ghostBtn")
                open_btn.clicked.connect(lambda _=False, url=step.link: QDesktopServices.openUrl(QUrl(url)))
                buttons.addWidget(open_btn)
            if step.action:
                act = QPushButton(step.action)
                act.setObjectName("sendBtn")
                act.clicked.connect(lambda _=False, s=step: self._act(s))
                buttons.addWidget(act)
                self._buttons.append(act)
            holder = QWidget()
            holder.setLayout(buttons)
            self.grid.addWidget(holder, row, 2, Qt.AlignmentFlag.AlignTop)
        self.undo_btn.setVisible(self.setup.can_undo)

    # -- the change ------------------------------------------------------------------------

    def _act(self, step: Step) -> None:
        if self._job is not None:
            return
        if step.id == "phone":
            self.on_add_phone()
            self.check()
            return
        if step.id == "served":
            self._run("apply", lambda: self.setup.apply("served"))

    def _undo(self) -> None:
        if self._job is not None:
            return
        if QMessageBox.question(self, "Stop serving",
                                "Phones will no longer reach the vault until you serve it again; this PC "
                                "still can. Continue?") != QMessageBox.StandardButton.Yes:
            return
        self._run("undo", lambda: self.setup.undo("served"))

    def done(self, result: int) -> None:  # noqa: D401 - Qt override
        # A running Tailscale command finishes on its own; its outcome is recorded by the
        # wizard itself (pending → applied/removed), not by this window.
        self.timer.stop()
        self.repeat.stop()
        super().done(result)


__all__ = ["SetupDialog", "SetupConflict"]
