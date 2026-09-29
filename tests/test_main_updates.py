"""The owner's menu press opens Updates only after a successful normal close."""
import json
import threading
from pathlib import Path

import pytest
from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication, QMessageBox

from vault_v2.runtime import RuntimeLease


@pytest.fixture
def ui(tmp_path, monkeypatch):
    import vault_v2.main as main

    app = QApplication.instance() or QApplication([])
    old_quit = app.quitOnLastWindowClosed()
    app.setQuitOnLastWindowClosed(False)
    root = tmp_path / "synthetic-archive"
    root.mkdir()
    (root / "vault.json").write_text(json.dumps({"agent_backend": "unsupported"}), encoding="utf-8")
    window = main.MainWindow(root, start_services=False)
    window.show()
    executable = tmp_path / "Programs with spaces" / "versions" / "7" / "python" / "pythonw.exe"
    monkeypatch.setattr(main, "updater_python", lambda: str(executable))
    launches, messages = [], []
    monkeypatch.setattr(main.subprocess, "Popen", lambda *a, **kw: launches.append((a, kw)))
    for name in ("warning", "information"):
        monkeypatch.setattr(main.QMessageBox, name, lambda *a: messages.append(a))
    monkeypatch.setattr(main.QMessageBox, "question", lambda *a: QMessageBox.No)
    try:
        yield app, window, executable, launches, messages, main
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()
        app.setQuitOnLastWindowClosed(old_quit)


def test_menu_is_explicit_and_no_is_the_default(ui, monkeypatch):
    app, window, executable, launches, messages, main = ui
    menu = next(a.menu() for a in window.menuBar().actions() if a.text() == "Vault")
    action = next(a for a in menu.actions() if a.text() == "Check for updates…")
    confirmations = []
    def decline(*args):
        confirmations.append(args)
        return QMessageBox.No
    monkeypatch.setattr(main.QMessageBox, "question", decline)
    assert launches == []
    action.trigger()
    assert confirmations[0][-1] == QMessageBox.No
    assert window.isVisible() and launches == []


def test_source_checkout_explains_unavailability_without_closing(ui, monkeypatch):
    _, window, _, launches, messages, main = ui
    monkeypatch.setattr(main, "updater_python", lambda: None)
    window.show_updates()
    assert window.isVisible() and launches == []
    assert "source checkout" in messages[-1][2]


def test_broken_installation_stays_open_and_scrubs_the_error(ui, monkeypatch):
    _, window, _, launches, messages, main = ui
    def broken():
        raise RuntimeError("private synthetic path")
    monkeypatch.setattr(main, "updater_python", broken)
    window.show_updates()
    assert window.isVisible() and launches == []
    assert "private synthetic path" not in str(messages)
    assert "Repair" in messages[-1][2]


def test_confirmed_transition_releases_root_before_launch_and_passes_no_root(ui, monkeypatch):
    _, window, executable, launches, _, main = ui
    monkeypatch.setattr(main.QMessageBox, "question", lambda *a: QMessageBox.Yes)
    def launch(argv, **kwargs):
        assert not window.isVisible()
        lease = RuntimeLease(window.paths.root)
        try:
            assert not lease.read_only, "normal close must release the root before launch"
        finally:
            lease.close()
        launches.append((argv, kwargs))
    monkeypatch.setattr(main.subprocess, "Popen", launch)
    window.show_updates()
    window.show_updates()  # retained closed object cannot replay a launch
    assert len(launches) == 1
    argv, options = launches[0]
    assert argv == [str(executable), "-I", "-B", "-m", "vault_v2.update_dialog"]
    assert options["cwd"] == executable.parent.parent
    assert options["shell"] is False and options["close_fds"] is True


def test_running_worker_refuses_close_and_does_not_launch_updater(ui, monkeypatch):
    _, window, _, launches, _, main = ui
    entered, finish = threading.Event(), threading.Event()
    class Work(QThread):
        def run(self):
            entered.set()
            finish.wait(8)
    worker = Work(window)
    worker.start()
    assert entered.wait(2)
    monkeypatch.setattr(main.QMessageBox, "question", lambda *a: QMessageBox.Yes)
    try:
        window.show_updates()
        assert window.isVisible() and launches == []
        assert "busy" in window.statusBar().currentMessage()
        other = RuntimeLease(window.paths.root)
        try:
            assert other.read_only
        finally:
            other.close()
    finally:
        finish.set()
        assert worker.wait(3000)


def test_launch_error_never_reopens_a_closed_writer(ui, monkeypatch):
    _, window, _, launches, messages, main = ui
    monkeypatch.setattr(main.QMessageBox, "question", lambda *a: QMessageBox.Yes)
    def broken(*args, **kwargs):
        launches.append(True)
        raise OSError("secret synthetic path")
    monkeypatch.setattr(main.subprocess, "Popen", broken)
    window.show_updates()
    window.show_updates()
    assert not window.isVisible() and launches == [True]
    assert messages[-1][0] is None
    assert "could not start" in messages[-1][2]
    assert "secret synthetic path" not in str(messages)


def test_transition_does_not_close_another_read_only_window(ui, monkeypatch):
    app, window, _, launches, _, main = ui
    peer = main.MainWindow(window.paths.root, start_services=False)
    peer.show()
    monkeypatch.setattr(main.QMessageBox, "question", lambda *a: QMessageBox.Yes)
    try:
        assert peer.read_only
        window.show_updates()
        assert not window.isVisible() and peer.isVisible()
        assert len(launches) == 1
    finally:
        peer.close()
        peer.deleteLater()
        app.processEvents()
