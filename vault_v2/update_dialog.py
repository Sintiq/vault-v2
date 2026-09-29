"""Separate installed updater, with no Vault root or runtime ownership.

No network on open. Each check and each installer handoff requires a human press.
The caller closes normal Vault windows first; this module never terminates them.
"""
from __future__ import annotations

import queue
import sys
import threading

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication, QDialog, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout

from .release_handoff import InstallerStarted
from .update_service import ReadyUpdate, UpdateError, UpdateService


class UpdateDialog(QDialog):
    def __init__(self, service=None, parent=None):
        super().__init__(parent)
        self.service = service if service is not None else UpdateService()
        self.busy = False
        self._ready = None
        self._finished = False
        self._results = queue.SimpleQueue()
        self._thread = None
        self.setWindowTitle("Vault V2 — Updates")
        self.resize(680, 460)
        self.heading = QLabel("Vault updates — nothing installs automatically")
        self.heading.setTextFormat(Qt.PlainText)
        self.configuration_label = QLabel()
        self.configuration_label.setTextFormat(Qt.PlainText)
        self.configuration_label.setWordWrap(True)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlaceholderText("The exact verified release will appear here after you check.")
        self.status = QLabel("Close all Vault windows before installing. Windows may display a publisher or reputation warning.")
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        self.check_btn = QPushButton("Check and download update")
        self.install_btn = QPushButton("Install this release…")
        self.close_btn = QPushButton("Close")
        self.check_btn.clicked.connect(self.check)
        self.install_btn.clicked.connect(self.install)
        self.close_btn.clicked.connect(self.reject)
        row = QHBoxLayout()
        for button in (self.check_btn, self.install_btn, self.close_btn):
            row.addWidget(button)
        layout = QVBoxLayout(self)
        for widget in (self.heading, self.configuration_label, self.details, self.status):
            layout.addWidget(widget)
        layout.addLayout(row)
        self.install_btn.setEnabled(False)
        self._refresh_configuration()
        self._timer = QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._collect)
        self._timer.start()

    def _refresh_configuration(self):
        try:
            configuration = self.service.configuration()
        except Exception:
            configuration = UpdateError(code="configuration_failed", message="Update configuration could not be read. Repair the installation and reopen this window.")
        if isinstance(configuration, UpdateError):
            self.configuration_label.setText(configuration.message)
            self.check_btn.setEnabled(False)
        else:
            self.configuration_label.setText(
                f"Installed: {configuration.version} (code {configuration.version_code}, API {configuration.api_version})\n"
                f"Release channel: {configuration.channel}")
            self.check_btn.setEnabled(not self.busy and not self._finished)

    def _start(self, kind, work):
        if self.busy or self._finished:
            return
        self.busy = True
        self.check_btn.setEnabled(False)
        self.install_btn.setEnabled(False)
        self.close_btn.setEnabled(False)
        self.status.setText("Checking and downloading verified bytes…" if kind == "check"
                            else "Rechecking the approved release and asking the installer to start…")
        results = self._results
        def run():
            try:
                result = work()
            except Exception:
                result = UpdateError(code="request_failed", message="The update request failed. Nothing is reported as installed. Check again before retrying.")
            results.put((kind, result))
        self._thread = threading.Thread(target=run, daemon=False, name="Vault-update-" + kind)
        try:
            self._thread.start()
        except RuntimeError:
            results.put((kind, UpdateError(code="worker_unavailable", message="The update worker could not start. Close other applications and try again.")))

    def check(self):
        if self.busy or self._finished or not self.check_btn.isEnabled():
            return
        self._ready = None
        self.details.clear()
        self._start("check", self.service.check)

    def install(self):
        if self.busy or self._finished or self._ready is None:
            return
        ready = self._ready
        release = ready.offer.release
        confirmation = QMessageBox(self)
        confirmation.setWindowTitle("Install the displayed release?")
        confirmation.setIcon(QMessageBox.Question)
        confirmation.setTextFormat(Qt.PlainText)
        confirmation.setText(f"Install {release.version} (code {release.version_code})?\n"
            "All Vault windows must be closed. The installer will ask you to continue; this does not report a completed installation.")
        confirmation.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        confirmation.setDefaultButton(QMessageBox.No)
        answer = confirmation.exec()
        if answer != QMessageBox.Yes:
            return
        self._ready = None
        self._start("install", lambda: self.service.install(ready, ready.offer.approval_digest))

    def _collect(self):
        try:
            kind, result = self._results.get_nowait()
        except queue.Empty:
            return
        self.busy = False
        self.close_btn.setEnabled(True)
        if isinstance(result, UpdateError):
            self._ready = None
            self.install_btn.setEnabled(False)
            self._refresh_configuration()
            self.status.setText(result.message)
        elif kind == "check" and isinstance(result, ReadyUpdate):
            self._ready = result
            release = result.offer.release
            self.details.setPlainText(
                f"Version: {release.version}\nVersion code: {release.version_code}\n"
                f"Platform: {release.platform}\nFile: {release.file}\nBytes: {release.size}\n"
                f"SHA256: {release.sha256}\nMinimum API: {release.min_api_version}")
            self.status.setText("Release-manifest signature and downloaded bytes verified. Review this exact release, then press Install if you want it.")
            self.check_btn.setEnabled(True)
            self.install_btn.setEnabled(True)
        elif kind == "install" and isinstance(result, InstallerStarted):
            self._finished = True
            self.status.setText("Installer process started; installation is NOT confirmed. Follow the installer, then reopen Vault from its shortcut.")
            self.check_btn.setEnabled(False)
            self.install_btn.setEnabled(False)
        else:
            self._ready = None
            self.install_btn.setEnabled(False)
            self._refresh_configuration()
            self.status.setText("Unexpected update result; no installation success is claimed. Check again.")

    def reject(self):
        if self.busy:
            self.status.setText("An update request is still running. Wait for it to finish before closing.")
            return
        self._timer.stop()
        super().reject()

    def closeEvent(self, event):
        if self.busy:
            self.status.setText("An update request is still running. Wait for it to finish before closing.")
            event.ignore()
            return
        self._timer.stop()
        super().closeEvent(event)


def main() -> int:
    # Deliberately no root/key/channel command-line overrides in the public entry.
    application = QApplication([sys.argv[0]])
    from .theme import LIGHT, stylesheet
    application.setStyleSheet(stylesheet(LIGHT))
    dialog = UpdateDialog()
    dialog.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
