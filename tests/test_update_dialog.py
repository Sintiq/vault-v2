"""The separate updater: explicit actions, exact display, no real installation."""
import threading
import time
import os
from pathlib import Path

import pytest
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from vault_v2.update_dialog import UpdateDialog
from vault_v2.update_service import ReadyUpdate, UpdateConfiguration, UpdateError
from vault_v2.release_handoff import InstallOffer, InstallerStarted
from vault_v2.releases import ReleaseDescription
from vault_v2.release_stage import StagedRelease


@pytest.fixture
def app():
    application = QApplication.instance() or QApplication([])
    yield application
    application.processEvents()


def settle(app, dialog):
    deadline = time.monotonic() + 3
    while dialog.busy and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)
    app.processEvents()
    assert not dialog.busy


class Service:
    def __init__(self):
        self.calls = []
        self.release = ReleaseDescription("vault-v2-release@1", "2.0-test", 2, "windows", "vault.exe", 3, "a" * 64, 2)
        self.ready = ReadyUpdate(StagedRelease(self.release, b"manifest", b"signature",
                                 Path("C:/synthetic/release-test/vault.exe"), "release-test"),
                                 InstallOffer(self.release, "b" * 64))
        self.config = UpdateConfiguration(version="1.0", version_code=1, api_version=2,
            channel="https://updates.example.invalid/windows", key=b"synthetic key",
            staging_parent=Path("C:/synthetic"), installed_directory=Path("C:/program"))
        self.check_result = self.ready
        self.install_result = InstallerStarted(123, self.release)
        self.wait = None

    def configuration(self):
        return self.config

    def check(self):
        self.calls.append(("check", threading.get_ident()))
        if self.wait is not None:
            assert self.wait.wait(3)
        if isinstance(self.check_result, Exception):
            raise self.check_result
        return self.check_result

    def install(self, ready, confirmed_digest):
        self.calls.append(("install", ready, confirmed_digest, threading.get_ident()))
        return self.install_result


def test_constructing_window_does_not_check_download_or_install(app):
    service = Service()
    dialog = UpdateDialog(service)
    assert service.calls == []
    assert not dialog.install_btn.isEnabled()
    assert dialog.check_btn.isEnabled()
    dialog.close()


def test_check_runs_off_ui_thread_and_displays_exact_verified_release(app):
    service = Service()
    dialog = UpdateDialog(service)
    dialog.check_btn.click()
    settle(app, dialog)
    assert service.calls == [("check", service.calls[0][1])]
    assert service.calls[0][1] != threading.get_ident()
    text = dialog.details.toPlainText()
    for value in ("2.0-test", "vault.exe", "a" * 64, "3", "windows"):
        assert value in text
    assert dialog.install_btn.isEnabled()
    dialog.close()


def test_install_requires_second_explicit_confirmation_and_quotes_displayed_digest(app, monkeypatch):
    service = Service()
    dialog = UpdateDialog(service)
    dialog.check_btn.click()
    settle(app, dialog)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.No)
    dialog.install_btn.click()
    assert [call[0] for call in service.calls] == ["check"]
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.Yes)
    dialog.install_btn.click()
    settle(app, dialog)
    call = service.calls[-1]
    assert call[:3] == ("install", service.ready, "b" * 64)
    assert call[3] != threading.get_ident()
    assert "NOT confirmed" in dialog.status.text()
    assert not dialog.check_btn.isEnabled()
    assert not dialog.install_btn.isEnabled()
    dialog.close()


def test_repeated_check_clears_old_offer_and_busy_window_does_not_abandon_worker(app):
    service = Service()
    dialog = UpdateDialog(service)
    dialog.show()
    dialog.check_btn.click()
    settle(app, dialog)
    service.wait = threading.Event()
    dialog.check_btn.click()
    assert dialog.busy
    assert not dialog.details.toPlainText()
    assert not dialog.install_btn.isEnabled()
    dialog.check()
    dialog.install()
    dialog.reject()
    dialog.close()
    app.processEvents()
    assert dialog.isVisible() and dialog.busy
    service.wait.set()
    settle(app, dialog)
    assert [call[0] for call in service.calls] == ["check", "check"]
    dialog.close()


def test_missing_configuration_does_not_issue_network_or_install(app):
    service = Service()
    service.config = UpdateError(code="updates_disabled", message="No installed release key or channel; updates are disabled.")
    dialog = UpdateDialog(service)
    assert "disabled" in dialog.configuration_label.text()
    assert not dialog.check_btn.isEnabled()
    dialog.check()
    dialog.install()
    assert service.calls == []
    dialog.close()


def test_busy_close_does_not_promise_bounded_local_io(app):
    service = Service()
    service.wait = threading.Event()
    dialog = UpdateDialog(service)
    dialog.show()
    try:
        dialog.check_btn.click()
        dialog.reject()
        assert dialog.isVisible() and dialog.busy
        assert "bounded" not in dialog.status.text().casefold()
        assert "still running" in dialog.status.text().casefold()
    finally:
        service.wait.set()
        settle(app, dialog)
        dialog.close()


def test_failed_check_never_exposes_exception_text_or_retains_an_offer(app):
    service = Service()
    dialog = UpdateDialog(service)
    dialog.check_btn.click()
    settle(app, dialog)
    service.check_result = RuntimeError("PRIVATE DEBUG MATERIAL")
    dialog.check_btn.click()
    settle(app, dialog)
    assert "PRIVATE" not in dialog.status.text()
    assert not dialog.install_btn.isEnabled()
    assert not dialog.details.toPlainText()
    assert dialog.check_btn.isEnabled()
    dialog.close()


def test_install_refusal_requires_new_check_and_makes_no_success_claim(app, monkeypatch):
    service = Service()
    service.install_result = UpdateError(code="runtime_busy", message="Close all Vault windows first.")
    dialog = UpdateDialog(service)
    dialog.check_btn.click()
    settle(app, dialog)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.Yes)
    dialog.install_btn.click()
    settle(app, dialog)
    assert dialog.status.text() == "Close all Vault windows first."
    assert dialog.check_btn.isEnabled()
    assert not dialog.install_btn.isEnabled()
    dialog.install()
    assert [call[0] for call in service.calls] == ["check", "install"]
    dialog.close()


def test_manifest_display_is_literal_not_rich_text(app):
    from dataclasses import replace
    service = Service()
    release = replace(service.release, version="<b>NOT HTML</b>")
    service.check_result = replace(service.ready, offer=InstallOffer(release, "b" * 64))
    dialog = UpdateDialog(service)
    dialog.check_btn.click()
    settle(app, dialog)
    assert "<b>NOT HTML</b>" in dialog.details.toPlainText()
    dialog.close()


def test_worker_start_failure_restores_controls_without_raw_diagnostics(app, monkeypatch):
    service = Service()
    dialog = UpdateDialog(service)
    def fail(_):
        raise RuntimeError("PRIVATE THREAD DIAGNOSTIC")
    monkeypatch.setattr(threading.Thread, "start", fail)
    dialog.check()
    settle(app, dialog)
    assert "PRIVATE" not in dialog.status.text()
    assert dialog.check_btn.isEnabled() and dialog.close_btn.isEnabled()
    assert service.calls == []
    dialog.close()


def test_confirmation_surface_uses_literal_version_and_no_is_default(app, monkeypatch):
    from dataclasses import replace
    service = Service()
    release = replace(service.release, version="<b>NOT HTML</b>")
    service.check_result = replace(service.ready, offer=InstallOffer(release, "b" * 64))
    dialog = UpdateDialog(service)
    dialog.check()
    settle(app, dialog)
    def forbidden_static(*args):
        pytest.fail("Static question cannot force plain-text display")
    checked = []
    def inspect(box):
        assert box.textFormat() == Qt.PlainText
        assert "<b>NOT HTML</b>" in box.text()
        assert box.defaultButton() == box.button(QMessageBox.No)
        checked.append(True)
        return QMessageBox.No
    monkeypatch.setattr(QMessageBox, "question", forbidden_static)
    monkeypatch.setattr(QMessageBox, "exec", inspect)
    dialog.install()
    assert checked == [True]
    assert [call[0] for call in service.calls] == ["check"]
    dialog.close()


def test_render_synthetic_verified_offer_offscreen(app, tmp_path):
    from PySide6.QtGui import QFont, QFontDatabase
    from vault_v2.theme import LIGHT, stylesheet
    # Windows offscreen plugin does not discover native fonts automatically.
    if os.name == "nt":
        font = QFontDatabase.addApplicationFont(str(Path(os.environ["WINDIR"]) / "Fonts/segoeui.ttf"))
        assert font >= 0
        app.setFont(QFont(QFontDatabase.applicationFontFamilies(font)[0], 10))
    app.setStyleSheet(stylesheet(LIGHT))
    dialog = UpdateDialog(Service())
    dialog.show()
    dialog.check()
    settle(app, dialog)
    chosen = os.environ.get("VAULT_TEST_RENDER_OUT")
    output = Path(chosen) if chosen else tmp_path / "updater-verified-offer.png"
    assert not output.exists(), "a chosen qualification artifact must never be overwritten"
    assert dialog.grab().save(str(output))
    print("SYNTHETIC_UPDATER_IMAGE=" + str(output))
    dialog.close()
