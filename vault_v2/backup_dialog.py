"""Backup… — write the whole vault to a drive, encrypted, and prove it landed."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .backup import BackupError, BackupResult, MIN_PASSPHRASE, list_backups, make_backup, verify_backup
from .receipts import ReceiptLog
from .errors import VaultError

DEFAULT_DEST = Path("D:/VaultBackup")


class _Worker(QObject):
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, root: Path, dest: Path, passphrase: str, log: ReceiptLog, *, check: Path | None = None):
        super().__init__()
        self.root, self.dest, self.passphrase, self.log = root, dest, passphrase, log
        self.check = check

    def run(self) -> None:
        try:
            result = ((self.check, verify_backup(self.check, self.passphrase)) if self.check is not None
                      else make_backup(self.root, self.dest, self.passphrase, self.log))
            self.done.emit(result)
        except (BackupError, VaultError, OSError) as exc:
            self.failed.emit(str(exc))
        finally:
            self.passphrase = ""


class BackupDialog(QDialog):
    def __init__(self, root: Path, log: ReceiptLog, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Backup")
        self.setMinimumWidth(520)
        self.root, self.log = root, log
        self.result: BackupResult | None = None
        self._thread: QThread | None = None

        head = QLabel("Back up the whole vault to a drive")
        head.setObjectName("chatTitle")
        sub = QLabel(
            "Everything — documents, cards, tasks, health, receipts, trash — zipped and "
            "encrypted with AES-256 under your passphrase. The drive is useless without it, "
            "and nothing stores it: write it down somewhere that is not this PC and not "
            "the drive. A RESTORE.md and a small script go next to the file, so the "
            "documents come back even without this app."
        )
        sub.setObjectName("chatSub")
        sub.setWordWrap(True)

        self.dest = QLineEdit(str(DEFAULT_DEST))
        browse = QPushButton("Choose…")
        browse.setObjectName("ghostBtn")
        browse.clicked.connect(self._browse)
        dest_row = QHBoxLayout()
        dest_row.addWidget(self.dest, 1)
        dest_row.addWidget(browse)

        self.pass1 = QLineEdit()
        self.pass1.setEchoMode(QLineEdit.EchoMode.Password)
        self.pass1.setPlaceholderText(f"passphrase, at least {MIN_PASSPHRASE} characters")
        self.pass2 = QLineEdit()
        self.pass2.setEchoMode(QLineEdit.EchoMode.Password)
        self.pass2.setPlaceholderText("the same, again")

        self.status = QLabel("")
        self.status.setObjectName("chatSub")
        self.status.setWordWrap(True)

        self.write_btn = QPushButton("Write the backup")
        self.write_btn.setObjectName("sendBtn")
        self.write_btn.clicked.connect(self._write)
        self.write_btn.setEnabled(not log.read_only)
        self.check_btn = QPushButton("Check an existing backup…")
        self.check_btn.setObjectName("ghostBtn")
        self.check_btn.clicked.connect(self._check)
        close = QPushButton("Close")
        close.setObjectName("ghostBtn")
        close.clicked.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(self.check_btn)
        row.addStretch(1)
        row.addWidget(close)
        row.addWidget(self.write_btn)

        lay = QVBoxLayout(self)
        lay.setSpacing(8)
        lay.addWidget(head)
        lay.addWidget(sub)
        lay.addWidget(QLabel("Where"))
        lay.addLayout(dest_row)
        lay.addWidget(QLabel("Passphrase"))
        lay.addWidget(self.pass1)
        lay.addWidget(self.pass2)
        lay.addWidget(self.status)
        lay.addLayout(row)
        self._show_existing()

    def _show_existing(self) -> None:
        have = list_backups(Path(self.dest.text()))
        self.status.setText(
            f"{len(have)} backup(s) already there, newest {have[-1].name}" if have
            else "no backups there yet"
        )

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Backup folder", self.dest.text())
        if d:
            self.dest.setText(d)
            self._show_existing()

    def _write(self) -> None:
        if self._thread is not None:
            return
        if self.log.read_only:
            self.status.setText("read-only: this window cannot write a backup")
            return
        p1, p2 = self.pass1.text(), self.pass2.text()
        if p1 != p2:
            self.status.setText("the two passphrases differ")
            return
        if len(p1) < MIN_PASSPHRASE:
            self.status.setText(f"at least {MIN_PASSPHRASE} characters")
            return
        self.write_btn.setEnabled(False)
        self.check_btn.setEnabled(False)
        self.status.setText("writing, then reading it back…")
        self._thread = QThread(self)
        self._worker = _Worker(self.root, Path(self.dest.text()), p1, self.log)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.done.connect(self._done)
        self._worker.failed.connect(self._failed)
        self._thread.start()
        # The passphrase does not linger in the fields once used.
        self.pass1.clear()
        self.pass2.clear()

    def _stop(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(3000)
            self._thread = None
        self.write_btn.setEnabled(not self.log.read_only)
        self.check_btn.setEnabled(True)

    def _done(self, result) -> None:
        self._stop()
        if isinstance(result, tuple):
            path, count = result
            self.status.setText(f"OK · {path.name} decrypts and all {count} files match their digests")
            return
        self.result = result
        self.status.setText(
            f"VERIFIED · {result.files} files · {result.size // 1024} KB · "
            f"{result.path.name}\nsha256 {result.sha256[:16]}… — read back from the drive and decrypted"
        )

    def _failed(self, err: str) -> None:
        self._stop()
        self.status.setText(f"not completed: {err}")

    def _check(self) -> None:
        if self._thread is not None:
            return
        f, _ = QFileDialog.getOpenFileName(self, "Backup to check", self.dest.text(), "Vault backup (*.vault)")
        if not f:
            return
        p = self.pass1.text()
        if not p:
            self.status.setText("type the passphrase in the first field, then Check again")
            return
        self.write_btn.setEnabled(False)
        self.check_btn.setEnabled(False)
        self.status.setText("checking the encrypted backup…")
        self._thread = QThread(self)
        self._worker = _Worker(self.root, Path(self.dest.text()), p, self.log, check=Path(f))
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.done.connect(self._done)
        self._worker.failed.connect(self._failed)
        self._thread.start()
        self.pass1.clear()
        self.pass2.clear()

    def reject(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            self.status.setText("Backup is still running — please wait before closing.")
            return
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._thread is not None and self._thread.isRunning():
            self.status.setText("Backup is still running — please wait before closing.")
            event.ignore()
            return
        super().closeEvent(event)
