"""Small runtime UX regressions using synthetic roots and gated model replies."""

import gc
import json
import os
import socket
import subprocess
import sys
import threading
import weakref
from pathlib import Path

import pytest

from vault_v2.runtime import RuntimeLease


def test_read_only_reason_identifies_same_process_owner_pid(tmp_path: Path) -> None:
    first = RuntimeLease(tmp_path / "synthetic-vault")
    second = RuntimeLease(first.root)
    try:
        assert second.read_only
        assert f"owner PID {os.getpid()}" in second.reason
    finally:
        second.close()
        first.close()


@pytest.mark.parametrize("damaged", [False, True])
def test_recovery_reason_shows_recorded_pid_without_changing_ownership(tmp_path: Path, damaged: bool) -> None:
    root = tmp_path / "synthetic-vault"
    root.mkdir()
    path = root / ".vault.lock"
    path.write_text(json.dumps({"pid": os.getpid(), "machine": socket.gethostname(),
                                "started_at": "damaged" if damaged else "2026-09-22T18:00:00+00:00",
                                "state": "active"}), encoding="utf-8")
    before = path.read_bytes()
    lease = RuntimeLease(root)
    try:
        assert lease.read_only
        assert f"owner PID {os.getpid()}" in lease.reason
        assert path.read_bytes() == before
    finally:
        lease.close()


def test_read_only_reason_identifies_owner_in_another_process(tmp_path: Path) -> None:
    first = RuntimeLease(tmp_path / "synthetic-vault")
    script = (
        "import json,sys; from pathlib import Path; from vault_v2.runtime import RuntimeLease; "
        "lease=RuntimeLease(Path(sys.argv[1])); "
        "print(json.dumps({'read_only':lease.read_only, 'reason':lease.reason})); lease.close()"
    )
    try:
        reply = subprocess.run([sys.executable, "-B", "-c", script, str(first.root)],
                               capture_output=True, text=True, timeout=5, check=True,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        result = json.loads(reply.stdout)
        assert result["read_only"]
        assert f"owner PID {os.getpid()}" in result["reason"]
    finally:
        first.close()


@pytest.fixture()
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    application = QApplication.instance() or QApplication([])
    application.setQuitOnLastWindowClosed(False)
    return application


@pytest.fixture()
def window(tmp_path: Path, app):
    from vault_v2.main import MainWindow
    root = tmp_path / "synthetic-vault"
    root.mkdir()
    (root / "vault.json").write_text(json.dumps({"agent_backend": "unsupported"}), encoding="utf-8")
    window = MainWindow(root, start_services=False)
    try:
        yield window
    finally:
        window.close()
        app.processEvents()


@pytest.mark.parametrize("cancel", ["reject", "accept", "close"])
@pytest.mark.parametrize("model_fails", [False, True])
def test_cancel_running_sort_closes_now_and_discards_late_draft(window, app, cancel, model_fails) -> None:
    from PySide6.QtCore import QThread
    from PySide6.QtTest import QTest
    from vault_v2.agent import Backend, BackendInfo
    from vault_v2.sort_dialog import SortDialog
    from vault_v2.sorting import collect_inputs

    source = window.paths.staging / "synthetic-insurance.txt"
    source.write_bytes(b"Synthetic insurance renewal.")
    entered, release = threading.Event(), threading.Event()

    class SlowBackend(Backend):
        info = BackendInfo("synthetic", "synthetic", "synthetic")

        def chat(self, system, messages, on_chunk):
            entered.set()
            assert release.wait(8)
            if model_fails:
                raise OSError("synthetic model failure")
            return "[]"

    dialog = SortDialog(window.cards, collect_inputs(window.paths.staging, window.cards), SlowBackend(), window)
    dialog.show()
    assert entered.wait(3)
    before = {p.relative_to(window.paths.root): p.read_bytes()
              for p in window.paths.root.rglob("*") if p.is_file()}
    try:
        getattr(dialog, cancel)()
        assert not dialog.isVisible(), "cancel must close before the model returns"
        window.close()
        assert json.loads((window.paths.root / ".vault.lock").read_text())["state"] == "active"
    finally:
        release.set()
        for _ in range(150):
            QTest.qWait(20)
            if not any(t.isRunning() for t in dialog.findChildren(QThread)):
                break
        assert not any(t.isRunning() for t in dialog.findChildren(QThread))
        dialog.close()
    assert window.cards.all() == []
    assert window.ops.log.tail() == []
    assert {p.relative_to(window.paths.root): p.read_bytes()
            for p in window.paths.root.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("action", ["sort_staging", "ask_staging"])
@pytest.mark.parametrize("model_fails", [False, True])
def test_modal_cancel_keeps_worker_owned_after_call_returns(window, app, action, model_fails, monkeypatch) -> None:
    from PySide6.QtCore import QThread, QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from vault_v2.agent import Backend, BackendInfo
    from vault_v2.ask_dialog import AskDialog
    unhandled = []
    monkeypatch.setattr(sys, "excepthook", lambda *args: unhandled.append(args))

    (window.paths.staging / "synthetic-insurance.txt").write_bytes(b"Synthetic insurance renewal.")
    entered, release = threading.Event(), threading.Event()
    references = []
    polls = []

    class SlowBackend(Backend):
        info = BackendInfo("synthetic", "synthetic", "synthetic")

        def chat(self, system, messages, on_chunk):
            entered.set()
            assert release.wait(8)
            if model_fails:
                raise OSError("synthetic late model failure")
            return "[]"

    def cancel_modal():
        dialog = QApplication.activeModalWidget()
        if isinstance(dialog, AskDialog) and not dialog.phrase.text():
            dialog.phrase.setText("insurance")
            dialog.propose_btn.click()
        polls.append(None)
        if entered.is_set() or len(polls) >= 200:
            references.append(weakref.ref(dialog))
            dialog.reject()
        else:
            QTimer.singleShot(10, cancel_modal)

    window.chat.backend = SlowBackend()
    QTimer.singleShot(0, cancel_modal)
    try:
        getattr(window, action)()
        assert entered.is_set(), "the real modal path must start the gated model"
        gc.collect()  # MainWindow's local dialog variable has already gone
        assert references[0]() is not None
        assert not references[0]().isVisible()
        before_completion = window.ops.log.tail()  # Sort already receipted its input read
        window.close()
        assert json.loads((window.paths.root / ".vault.lock").read_text())["state"] == "active"
    finally:
        release.set()
        for _ in range(150):
            QTest.qWait(20)
            if not any(t.isRunning() for t in window.findChildren(QThread)):
                break
        assert not any(t.isRunning() for t in window.findChildren(QThread))
    assert window.cards.all() == []
    assert window.ops.log.tail() == before_completion
    assert unhandled == []
    window.close()
    assert json.loads((window.paths.root / ".vault.lock").read_text())["state"] == "released"


@pytest.mark.parametrize("cancel", ["reject", "accept", "close"])
@pytest.mark.parametrize("model_fails", [False, True])
def test_cancel_running_ask_closes_now_and_discards_late_selection(window, app, cancel, model_fails) -> None:
    from PySide6.QtCore import QThread, Qt
    from PySide6.QtTest import QTest
    from vault_v2.agent import Backend, BackendInfo
    from vault_v2.ask import collect_docs
    from vault_v2.ask_dialog import AskDialog

    (window.paths.staging / "synthetic-insurance.txt").write_bytes(b"Synthetic insurance renewal.")
    entered, release = threading.Event(), threading.Event()

    class SlowBackend(Backend):
        info = BackendInfo("synthetic", "synthetic", "synthetic")

        def chat(self, system, messages, on_chunk):
            entered.set()
            assert release.wait(8)
            if model_fails:
                raise OSError("synthetic model failure")
            return '{"documents": ["doc-001"], "recipient": "INSURANCE"}'

    dialog = AskDialog(window.gate, collect_docs(window.paths.staging, window.cards), SlowBackend(), window)
    dialog.show()
    dialog.phrase.setText("insurance")
    dialog.propose_btn.click()
    assert entered.wait(3)
    before = {p.relative_to(window.paths.root): p.read_bytes()
              for p in window.paths.root.rglob("*") if p.is_file()}
    try:
        getattr(dialog, cancel)()
        assert not dialog.isVisible(), "cancel must close before the model returns"
        window.close()
        assert json.loads((window.paths.root / ".vault.lock").read_text())["state"] == "active"
    finally:
        release.set()
        for _ in range(150):
            QTest.qWait(20)
            if not any(t.isRunning() for t in dialog.findChildren(QThread)):
                break
        assert not any(t.isRunning() for t in dialog.findChildren(QThread))
        dialog.close()
    assert dialog.result is None
    assert dialog.list.item(0).checkState() == Qt.CheckState.Unchecked
    assert not dialog.export_btn.isEnabled()
    assert window.ops.log.tail() == []
    assert {p.relative_to(window.paths.root): p.read_bytes()
            for p in window.paths.root.rglob("*") if p.is_file()} == before
