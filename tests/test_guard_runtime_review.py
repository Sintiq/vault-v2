"""Independent lifecycle regressions, synthetic UI and loopback HTTP only."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.request

import pytest

from vault_v2.agent import Backend, BackendInfo
from vault_v2.api import ApiServer, VaultAPI
from vault_v2.main import MainWindow
from vault_v2.sort_dialog import SortDialog
from vault_v2.sorting import collect_inputs
from vault_v2.tasks import Task


@pytest.fixture()
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    application = QApplication.instance() or QApplication([])
    application.setQuitOnLastWindowClosed(False)
    return application


@pytest.fixture()
def window(tmp_path, app):
    root = tmp_path / "synthetic-vault"
    root.mkdir()
    (root / "vault.json").write_text(json.dumps({"agent_backend": "unsupported", "front": "proxied"}), encoding="utf-8")
    window = MainWindow(root, start_services=False)
    try:
        yield window
    finally:
        window.close()
        app.processEvents()


def state(window):
    return json.loads((window.paths.root / ".vault.lock").read_text(encoding="utf-8"))["state"]


def test_real_second_process_cannot_write_while_window_holds_runtime_lease(window):
    script = (
        "import json,sys; from pathlib import Path; from vault_v2.runtime import RuntimeLease; "
        "lease=RuntimeLease(Path(sys.argv[1])); print(json.dumps({'read_only':lease.read_only})); lease.close()"
    )
    before = (window.paths.root / ".vault.lock").read_bytes()
    other = subprocess.run([sys.executable, "-B", "-c", script, str(window.paths.root)],
                           capture_output=True, text=True, timeout=5,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=True)
    assert json.loads(other.stdout) == {"read_only": True}
    assert (window.paths.root / ".vault.lock").read_bytes() == before
    window.close()
    after = subprocess.run([sys.executable, "-B", "-c", script, str(window.paths.root)],
                           capture_output=True, text=True, timeout=5,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=True)
    assert json.loads(after.stdout) == {"read_only": False}


def test_hidden_sort_dialog_cannot_write_after_main_window_releases_lease(window, app):
    from PySide6.QtTest import QTest

    source = window.paths.staging / "synthetic.txt"
    source.write_bytes(b"Synthetic insurance renewal.")
    docs = collect_inputs(window.paths.staging, window.cards)
    entered, release = threading.Event(), threading.Event()

    class SlowBackend(Backend):
        info = BackendInfo("synthetic", "synthetic", "synthetic")

        def chat(self, system, messages, on_chunk):
            entered.set()
            assert release.wait(8)
            return "[]"

    dialog = SortDialog(window.cards, docs, SlowBackend(), window)
    dialog.show()
    assert entered.wait(3)
    try:
        dialog.hide()  # hiding without cancellation still publishes the result
        window.close()
        assert state(window) == "active", "model callback may still publish drafts; retain the lease"
    finally:
        release.set()
        for _ in range(150):
            QTest.qWait(20)
            if dialog._thread is None:
                break
        dialog.close()
    assert window.cards.all(), "the pending Sort result eventually publishes its draft"


def test_cancelled_sort_completion_queued_for_gui_retains_lease_until_discarded(window, app):
    from PySide6.QtTest import QTest

    source = window.paths.staging / "synthetic.txt"
    source.write_bytes(b"Synthetic insurance renewal.")
    docs = collect_inputs(window.paths.staging, window.cards)
    entered, release, returned = (threading.Event() for _ in range(3))

    class SlowBackend(Backend):
        info = BackendInfo("synthetic", "synthetic", "synthetic")

        def chat(self, system, messages, on_chunk):
            entered.set()
            assert release.wait(8)
            returned.set()
            return "[]"

    dialog = SortDialog(window.cards, docs, SlowBackend(), window)
    thread = dialog._thread
    dialog.show()
    assert entered.wait(3)
    try:
        # Closing now discards the result, but the queued completion must still
        # be drained before releasing the lease or destroying its QThread.
        dialog.close()
        release.set()
        assert returned.wait(2)
        thread.wait(200)  # deliberately do not process queued GUI signals yet
        window.close()
        assert state(window) == "active", "queued Sort completion still needs safe disposal"
    finally:
        release.set()
        for _ in range(150):
            QTest.qWait(20)
            if dialog._thread is None:
                break
        dialog.close()
    assert window.cards.all() == []
    assert window.ops.log.tail() == []


def test_stop_start_phone_keeps_retired_request_attached_until_it_finishes(window, app, monkeypatch, tmp_path):
    entered, release = threading.Event(), threading.Event()
    api = VaultAPI(window.ops, window.cards, window.tasks, window.health)

    def reply(_text):
        entered.set()
        assert release.wait(8)
        api.upload("late.txt", b"synthetic late upload")
        return "done"

    page = tmp_path / "index.html"
    page.write_text("synthetic", encoding="utf-8")
    old = ApiServer(api, "synthetic-key", page, reply, lambda: {}, "127.0.0.1", 0)
    old.start()
    window.api_server = old
    settings = {"agent_backend": "unsupported", "front": "proxied", "api_port": old.port}
    window.paths.settings_file.write_text(json.dumps(settings), encoding="utf-8")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], 0, stdout="100.64.0.10\n"))

    def send():
        req = urllib.request.Request(old.url.rstrip("/") + "/api/chat",
                                     data=b'{"text":"synthetic"}',
                                     headers={"Authorization": "Bearer synthetic-key", "Content-Type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=10) as response:
            return response.status

    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(send)
        assert entered.wait(3)
        try:
            window._stop_api()
            window._start_api()
            window.close()
            assert state(window) == "active", "replaced phone server still has a request capable of writing"
        finally:
            release.set()
        assert result.result(timeout=4) == 200
        old.stop()
    assert (window.paths.staging / "late.txt").read_bytes() == b"synthetic late upload"


@pytest.mark.parametrize("action", ["show_pending", "show_receipts"])
def test_inspection_buttons_do_not_freeze_gui_during_snapshot(window, monkeypatch, action):
    from PySide6.QtWidgets import QMessageBox

    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: None)
    monkeypatch.setattr(QMessageBox, "critical", lambda *args, **kwargs: None)

    def snapshot():
        with window.ops.log.write("backup snapshot"):
            entered.set()
            release.wait(3)

    worker = threading.Thread(target=snapshot)
    worker.start()
    assert entered.wait(2)
    deadline = threading.Timer(0.65, release.set)
    deadline.start()
    try:
        start = time.monotonic()
        getattr(window, action)()
        elapsed = time.monotonic() - start
        assert elapsed < 0.3, "inspection must refuse busy admission without freezing the event loop"
    finally:
        release.set()
        deadline.cancel()
        worker.join(3)


def test_write_after_modal_dialog_refuses_newly_busy_root_without_freezing(window, monkeypatch):
    from PySide6.QtWidgets import QInputDialog, QMessageBox

    entered, release = threading.Event(), threading.Event()
    failures = []

    def snapshot():
        with window.ops.log.write("backup snapshot"):
            entered.set()
            release.wait(3)

    worker = threading.Thread(target=snapshot)
    deadline = threading.Timer(0.65, release.set)

    def choose_name(*args, **kwargs):
        # The root becomes busy while the user is answering the dialog.
        worker.start()
        assert entered.wait(2)
        deadline.start()
        return "must-not-land", True

    monkeypatch.setattr(QInputDialog, "getText", choose_name)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: failures.append(str(args[2])))
    try:
        start = time.monotonic()
        window.panes["staging"].new_folder()
        elapsed = time.monotonic() - start
        assert elapsed < 0.3, "a modal dialog must not turn an old busy check into a GUI wait"
        assert failures and "busy" in failures[0]
        assert not (window.paths.staging / "must-not-land").exists()
    finally:
        release.set()
        deadline.cancel()
        worker.join(3)


def test_busy_checkbox_refusal_restores_shown_task_to_saved_state(window, monkeypatch):
    from PySide6.QtCore import Qt

    window.tasks.add(Task("synthetic", "abc", "synthetic.txt", "Call the clinic", "",
                          "Call the clinic", "HUMAN"))
    window.tasks_pane.reload()
    entered, release = threading.Event(), threading.Event()
    unhandled = []
    # Qt routes uncaught slot errors here; they must instead be user-visible.
    monkeypatch.setattr(sys, "excepthook", lambda *args: unhandled.append(args))

    def snapshot():
        with window.ops.log.write("backup snapshot"):
            entered.set()
            release.wait(3)

    worker = threading.Thread(target=snapshot)
    worker.start()
    assert entered.wait(2)
    try:
        # Admission can become busy just before the 100 ms UI disable timer.
        window.tasks_pane.list.item(0).setCheckState(Qt.CheckState.Checked)
        assert window.tasks.all()[0].done is False
        assert window.tasks_pane.list.item(0).checkState() == Qt.CheckState.Unchecked
        assert "busy" in window.statusBar().currentMessage()
        assert not unhandled
    finally:
        release.set()
        worker.join(3)
