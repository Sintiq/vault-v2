"""Chat-door integration through real Qt, HTTP, receipts and a fake executable."""
import io
import concurrent.futures
from contextlib import contextmanager
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from vault_v2.chat import Bubble, ChatPane
from vault_v2.receipts import ReceiptLog


@pytest.fixture(scope="module")
def application():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def local_http(monkeypatch):
    state = {"online": True, "calls": []}
    def open_http(_opener, request, *args, **kwargs):
        url = request.full_url if hasattr(request, "full_url") else request
        if not url.startswith("http://127.0.0.1:11434/api/"):
            return original(_opener, request, *args, **kwargs)
        state["calls"].append(url)
        if not state["online"]:
            raise ConnectionRefusedError("synthetic unavailable")
        if url.endswith("/tags"):
            return io.BytesIO(b'{"models":[{"name":"llama3.1:8b"}]}')
        return io.BytesIO(b'{"message":{"content":"local synthetic answer"},"done":true}\n')
    original = urllib.request.OpenerDirector.open
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", open_http)
    return state


def make_door(tmp_path, *, delay=0, timeout=180):
    from vault_v2.agent_door import AgentCommand, AgentDoor
    captured = tmp_path / "captured.json"
    executable = tmp_path / "synthetic_provider.py"
    executable.write_text(
        "import json, pathlib, sys, time\n"
        f"pathlib.Path({str(captured)!r}).write_text(sys.stdin.read(), encoding='utf-8')\n"
        f"time.sleep({delay!r})\n"
        "print(json.dumps({'type':'result','subtype':'success','is_error':False,"
        "'result':'<b>synthetic cloud answer</b>'}))\n", encoding="utf-8")
    return AgentDoor(ReceiptLog(tmp_path / "vault" / ".receipts"), {},
                     command=lambda _agent, _settings: AgentCommand([sys.executable, str(executable)]),
                     timeout_s=timeout), captured


def test_owner_opens_new_branch_shared_with_phone_without_local_history(application, local_http, tmp_path):
    door, captured = make_door(tmp_path)
    staging = tmp_path / "vault" / "Staging"
    staging.mkdir(parents=True)
    (staging / "synthetic.txt").write_text("BODY MUST NOT LEAVE", encoding="utf-8")
    pane = ChatPane(staging, {}, door=door)
    try:
        assert pane.remote_send("old local question") == "local synthetic answer"
        assert pane.sub.styleSheet() == ""
        pane.mode.setCurrentIndex(pane.mode.findData("claude"))
        assert "Claude" in pane.remote_state()["backend"]
        assert "font-size: 12pt" in pane.sub.styleSheet()
        assert "font-weight: 600" in pane.sub.styleSheet()
        assert pane.remote_send("new cloud question") == "<b>synthetic cloud answer</b>"
        application.processEvents()
        packet = captured.read_text(encoding="utf-8")
        assert "old local question" not in packet
        assert "local synthetic answer" not in packet
        assert "new cloud question" in packet and "synthetic.txt" in packet
        assert "BODY MUST NOT LEAVE" not in packet
        assert all(b.textFormat() == Qt.TextFormat.PlainText for b in pane.findChildren(Bubble))
        assert pane.backend.info.kind == "ollama"
        pane.mode.setCurrentIndex(0)
        assert pane.sub.styleSheet() == ""
    finally:
        pane.close()
        pane.deleteLater()
        application.processEvents()


def test_tasks_door_is_chat_only_without_changing_closed_door_baseline(application, local_http, tmp_path):
    from vault_v2.main import MainWindow
    local_http["online"] = False
    window = MainWindow(tmp_path / "vault", start_services=False)
    try:
        (window.paths.staging / "synthetic.txt").write_text("Remember to refill on 2026-10-01.", encoding="utf-8")
        window.chat.mode.setCurrentIndex(window.chat.mode.findData("claude"))
        window.tasks_pane.find_btn.click()
        assert "local model unavailable — the agent door covers chat only" in window.tasks_pane.prop_label.text()
        assert not any(row["op"] == "agent_read" for row in window.ops.log.tail(100))
        window.chat.mode.setCurrentIndex(0)
        window.tasks_pane.find_btn.click()
        deadline = time.monotonic() + 3
        while not window.tasks_pane.find_btn.isEnabled() and time.monotonic() < deadline:
            QTest.qWait(10)
        assert "baseline" in window.tasks_pane.prop_label.text()
    finally:
        deadline = time.monotonic() + 3
        while window.tasks_pane is not None and not window.tasks_pane.find_btn.isEnabled() and time.monotonic() < deadline:
            QTest.qWait(10)
        window.close()
        window.deleteLater()
        application.processEvents()


def wait_until(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        QTest.qWait(10)
    assert predicate()


@pytest.mark.parametrize("vault_writable", [True, False])
def test_desk_request_blocks_phone_and_switch_revokes_late_answer(application, local_http, tmp_path, vault_writable):
    from vault_v2.agent_door import DoorError
    door, captured = make_door(tmp_path, delay=30)
    staging = tmp_path / "vault" / "Staging"
    staging.mkdir(parents=True)
    pane = ChatPane(staging, {}, door=door)
    try:
        pane.mode.setCurrentIndex(pane.mode.findData("claude"))
        pane.input.setPlainText("desktop door question")
        pane.send.click()
        wait_until(captured.exists)
        pane.set_write_enabled(vault_writable)
        with pytest.raises(DoorError, match="door is busy"):
            pane.remote_send("second phone question")
        pane.set_write_enabled(True)
        pane.mode.setCurrentIndex(0)
        wait_until(lambda: not door.busy)
        wait_until(lambda: pane.send.isEnabled())
        assert pane.remote_state()["history"] == []
        assert not any("synthetic cloud answer" in b.text() for b in pane.findChildren(Bubble))
    finally:
        door.close()
        wait_until(lambda: not door.busy)
        QTest.qWait(50)
        pane.close()
        pane.deleteLater()
        application.processEvents()


def test_closing_window_revokes_door_before_waiting_for_worker(application, local_http, tmp_path):
    from vault_v2.main import MainWindow
    door, captured = make_door(tmp_path, delay=30)
    window = MainWindow(tmp_path / "vault", start_services=False,
                        door_factory=lambda _log, _settings: door)
    window.show()
    try:
        window.chat.mode.setCurrentIndex(window.chat.mode.findData("claude"))
        window.chat.input.setPlainText("close while pending")
        window.chat.send.click()
        wait_until(captured.exists)
        window.close()
        assert door.agent is None
        wait_until(lambda: not door.busy)
        wait_until(lambda: not window.chat.has_pending_work())
        window.close()
        assert not window.isVisible()
        rows = window.ops.log.tail(100)
        assert any(r["op"] == "agent_door_result" and r["extra"].get("status") == "cancelled" for r in rows)
        reopened = MainWindow(tmp_path / "vault", start_services=False)
        try:
            assert not reopened.read_only
            assert reopened.chat.mode.currentData() is None
        finally:
            reopened.close()
            reopened.deleteLater()
    finally:
        door.close()
        wait_until(lambda: not door.busy)
        QTest.qWait(50)
        window.close()
        window.deleteLater()
        application.processEvents()


def start_http(window):
    from vault_v2.agent_api import AgentAPI
    from vault_v2.api import ApiServer, VaultAPI
    agent = AgentAPI(window.paths.staging, window.reader, window.cards, window.tasks, window.health,
                     window.chat.get_local_backend)
    api = VaultAPI(window.ops, window.cards, window.tasks, window.health, agent)
    server = ApiServer(api, "synthetic-token", Path(__file__), window.chat.remote_send,
                        window.chat.remote_state, "127.0.0.1", 0)
    server.start()
    return server


def http(server, path, payload=None):
    request = urllib.request.Request(server.url + path,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"Authorization": "Bearer synthetic-token", "Content-Type": "application/json"})
    try:
        response = urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, json.loads(response.read())


def test_phone_http_busy_and_cancel_are_safe_responses(application, local_http, tmp_path):
    from vault_v2.main import MainWindow
    door, captured = make_door(tmp_path, delay=30)
    window = MainWindow(tmp_path / "vault", start_services=False,
                        door_factory=lambda _log, _settings: door)
    server = start_http(window)
    try:
        # This case exercises an admitted running request, not fail-fast
        # admission against the window's zero-delay pending-evidence read.
        application.processEvents()
        window.chat.mode.setCurrentIndex(window.chat.mode.findData("claude"))
        assert "Claude" in http(server, "/api/chat")[1]["backend"]
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            answer = pool.submit(http, server, "/api/chat", {"text": "phone request"})
            wait_until(lambda: captured.exists() or answer.done())
            assert captured.exists(), f"first request finished before launch: {answer.result(timeout=0)}"
            try:
                code, body = http(server, "/api/chat", {"text": "second request"})
                assert code == 503 and body["error"] == "door is busy"
            finally:
                window.chat.mode.setCurrentIndex(0)
            code, body = answer.result(timeout=5)
            assert code == 503 and "cancelled" in body["error"]
        assert window.chat.remote_state()["history"] == []
    finally:
        door.close()
        wait_until(lambda: not door.busy)
        server.stop()
        window.close()
        window.deleteLater()
        application.processEvents()


@pytest.mark.parametrize("operation", ["sort", "ask", "tasks", "health"])
def test_phone_nonchat_refuses_before_document_reads_when_door_open(application, local_http, tmp_path, operation):
    from vault_v2.main import MainWindow
    local_http["online"] = False
    window = MainWindow(tmp_path / "vault", start_services=False)
    server = start_http(window)
    try:
        (window.paths.staging / "synthetic.txt").write_text("Refill medication by 2026-10-01.", encoding="utf-8")
        window.chat.mode.setCurrentIndex(window.chat.mode.findData("claude"))
        code, body = http(server, "/api/agent/" + operation, {"phrase": "doctor"})
        assert code == 503 and body["error"] == "local model unavailable — the agent door covers chat only"
        assert not any(row["op"] in {"agent_read", "agent_door_request"} for row in window.ops.log.tail(100))
    finally:
        server.stop()
        window.close()
        window.deleteLater()
        application.processEvents()


def test_cancel_switch_remains_usable_when_receipt_guard_is_busy(application, local_http, tmp_path):
    from vault_v2.main import MainWindow
    door, captured = make_door(tmp_path, delay=30)
    window = MainWindow(tmp_path / "vault", start_services=False,
                        door_factory=lambda _log, _settings: door)
    entered, release = threading.Event(), threading.Event()
    def hold_guard():
        with window.ops.log.write("synthetic concurrent write"):
            entered.set()
            release.wait(5)
    guard_thread = threading.Thread(target=hold_guard)
    try:
        window.chat.mode.setCurrentIndex(window.chat.mode.findData("claude"))
        window.chat.input.setPlainText("cancel under busy guard")
        window.chat.send.click()
        wait_until(captured.exists)
        guard_thread.start()
        assert entered.wait(2)
        QTest.qWait(150)
        assert window.chat.mode.isEnabled()
        window.chat.mode.setCurrentIndex(0)
        assert door.agent is None
    finally:
        release.set()
        if guard_thread.ident:
            guard_thread.join(2)
        door.close()
        wait_until(lambda: not window.chat.has_pending_work())
        window.close()
        window.deleteLater()
        application.processEvents()


def test_clear_discards_phone_reply_and_new_branch_stays_empty(application, local_http, tmp_path):
    from vault_v2.agent_door import DoorError
    door, captured = make_door(tmp_path, delay=30)
    staging = tmp_path / "vault" / "Staging"
    staging.mkdir(parents=True)
    pane = ChatPane(staging, {}, door=door)
    try:
        pane.mode.setCurrentIndex(pane.mode.findData("claude"))
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            answer = pool.submit(pane.remote_send, "cancel through Clear")
            wait_until(captured.exists)
            pane.clear_btn.click()
            with pytest.raises(DoorError, match="cancelled"):
                answer.result(timeout=5)
        application.processEvents()
        assert pane.remote_state()["history"] == []
        assert not any("synthetic cloud answer" in b.text() for b in pane.findChildren(Bubble))
    finally:
        door.close()
        wait_until(lambda: not pane.has_pending_work())
        pane.close()
        pane.deleteLater()
        application.processEvents()


@pytest.mark.parametrize("operation", ["sort", "ask", "health"])
def test_desktop_nonchat_missing_local_shows_scope_notice(application, local_http, tmp_path, operation):
    from vault_v2.main import MainWindow
    local_http["online"] = False
    window = MainWindow(tmp_path / "vault", start_services=False)
    try:
        window.chat.mode.setCurrentIndex(window.chat.mode.findData("claude"))
        if operation == "health":
            window.health_pane.read_btn.click()
            notice = window.health_pane.prop_label.text()
        else:
            action = next(a for a in window.actions() if a.text() == operation.title() + "…")
            action.trigger()
            notice = window.statusBar().currentMessage()
        assert notice == "local model unavailable — the agent door covers chat only"
        assert not any(row["op"] in {"agent_read", "agent_door_request"} for row in window.ops.log.tail(100))
    finally:
        window.close()
        window.deleteLater()
        application.processEvents()


def test_receipt_file_count_is_bound_to_the_one_listing_snapshot(application, local_http, tmp_path, monkeypatch):
    door, captured = make_door(tmp_path)
    staging = tmp_path / "vault" / "Staging"
    staging.mkdir(parents=True)
    (staging / "first.txt").write_text("synthetic", encoding="utf-8")
    original = os.scandir
    changed = False
    @contextmanager
    def changed_listing(path):
        nonlocal changed
        with original(path) as entries:
            existing = list(entries)
        yield iter(existing)
        changed = True
        (staging / "later.txt").write_text("synthetic added later", encoding="utf-8")
    def changing_filesystem(path):
        if isinstance(path, (str, os.PathLike)) and Path(path) == staging and not changed:
            return changed_listing(path)
        return original(path)
    monkeypatch.setattr(os, "scandir", changing_filesystem)
    pane = ChatPane(staging, {}, door=door)
    try:
        pane.mode.setCurrentIndex(pane.mode.findData("claude"))
        pane.remote_send("count this snapshot")
        assert "later.txt" not in captured.read_text(encoding="utf-8")
        row = next(row for row in door.log.tail(100) if row["op"] == "agent_door_request")
        assert row["extra"]["files_count"] == 1
    finally:
        pane.close()
        pane.deleteLater()
        application.processEvents()


def test_ask_keeps_scope_notice_when_local_model_dies_after_discovery(application, local_http, tmp_path):
    from vault_v2.main import MainWindow
    from vault_v2.ask_dialog import AskDialog
    window = MainWindow(tmp_path / "vault", start_services=False)
    (window.paths.staging / "synthetic.txt").write_text("synthetic doctor note", encoding="utf-8")
    window.chat.mode.setCurrentIndex(window.chat.mode.findData("claude"))
    local_http["online"] = False
    observed = []
    deadline = time.monotonic() + 3
    def inspect_dialog():
        dialogs = window.findChildren(AskDialog)
        if not dialogs:
            QTimer.singleShot(10, inspect_dialog)
            return
        dialog = dialogs[-1]
        if not dialog.phrase.text():
            dialog.phrase.setText("doctor")
            dialog.propose_btn.click()
        if dialog.propose_btn.isEnabled() or time.monotonic() > deadline:
            observed.append(dialog.status.text())
            dialog.reject()
        else:
            QTimer.singleShot(10, inspect_dialog)
    try:
        QTimer.singleShot(0, inspect_dialog)
        window.ask_staging()
        assert observed and "local model unavailable — the agent door covers chat only" in observed[0]
        assert not any(row["op"] == "agent_door_request" for row in window.ops.log.tail(100))
    finally:
        window.close()
        window.deleteLater()
        application.processEvents()


def test_listing_failure_never_exposes_raw_filesystem_error_or_launches(application, local_http, tmp_path, monkeypatch):
    from vault_v2.agent_door import DoorError
    door, captured = make_door(tmp_path)
    staging = tmp_path / "vault" / "Staging"
    staging.mkdir(parents=True)
    target = staging / "synthetic.txt"
    target.write_text("synthetic", encoding="utf-8")
    pane = ChatPane(staging, {}, door=door)
    original = Path.stat
    def stat(path, *args, **kwargs):
        if path == target:
            raise OSError("SYNTHETIC_PRIVATE_ERROR")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "stat", stat)
    try:
        pane.mode.setCurrentIndex(pane.mode.findData("claude"))
        with pytest.raises(DoorError, match="^unusable reply$"):
            pane.remote_send("question")
        assert not captured.exists()
        assert pane.remote_state()["history"] == []
    finally:
        pane.close()
        pane.deleteLater()
        application.processEvents()


@pytest.mark.parametrize("excluded", ["hidden", "junction"])
def test_listing_never_descends_into_hidden_or_linked_directories(application, local_http, tmp_path, monkeypatch, excluded):
    door, captured = make_door(tmp_path)
    staging = tmp_path / "vault" / "Staging"
    visible = staging / "visible"
    visible.mkdir(parents=True)
    (visible / "ordinary.txt").write_text("synthetic ordinary", encoding="utf-8")
    if excluded == "junction":
        if os.name != "nt":
            pytest.skip("Windows junction regression")
        outside = tmp_path / "outside-staging"
        outside.mkdir()
        (outside / "SYNTHETIC_OUTSIDE_NAME.txt").write_text("synthetic outside", encoding="utf-8")
        forbidden = staging / "linked"
        created = subprocess.run(["cmd", "/c", "mklink", "/J", str(forbidden), str(outside)],
                                 capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        assert created.returncode == 0 and forbidden.is_junction()
    else:
        forbidden = staging / ".private"
        forbidden.mkdir()
        (forbidden / "SYNTHETIC_OUTSIDE_NAME.txt").write_text("synthetic hidden", encoding="utf-8")
    original = os.scandir
    descended = []
    def scandir(path):
        if Path(path) == forbidden:
            descended.append(str(path))
        return original(path)
    monkeypatch.setattr(os, "scandir", scandir)
    pane = ChatPane(staging, {}, door=door)
    try:
        pane.mode.setCurrentIndex(pane.mode.findData("claude"))
        pane.remote_send("what is in staging")
        packet = captured.read_text(encoding="utf-8")
        assert "SYNTHETIC_OUTSIDE_NAME" not in packet
        assert "ordinary.txt" in packet
        assert descended == []
        row = next(row for row in door.log.tail(100) if row["op"] == "agent_door_request")
        assert row["extra"]["files_count"] == 1
    finally:
        pane.close()
        pane.deleteLater()
        application.processEvents()
        if excluded == "junction" and forbidden.is_junction():
            assert forbidden.resolve() == outside.resolve()
            forbidden.rmdir()
            assert outside.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse regression")
@pytest.mark.parametrize("replace_during_walk", [False, True])
def test_reparse_root_or_changed_directory_refuses_before_launch(application, local_http, tmp_path, monkeypatch, replace_during_walk):
    from vault_v2.agent_door import DoorError
    door, captured = make_door(tmp_path)
    staging = tmp_path / "vault" / "Staging"
    staging.parent.mkdir(parents=True)
    outside = tmp_path / "outside-staging"
    outside.mkdir()
    (outside / "SYNTHETIC_OUTSIDE_NAME.txt").write_text("synthetic", encoding="utf-8")
    link = staging / "visible" if replace_during_walk else staging
    def make_junction():
        created = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                                 capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        assert created.returncode == 0 and link.is_junction()
    if replace_during_walk:
        link.mkdir(parents=True)
        (link / "ordinary.txt").write_text("synthetic", encoding="utf-8")
        original = os.scandir
        @contextmanager
        def replace_child():
            with original(staging) as entries:
                yield entries
            # Both targets are generated under this exact pytest root. Move only
            # this synthetic folder; install a junction before queued descent.
            parked = tmp_path / "parked-visible"
            assert link.resolve().is_relative_to(tmp_path.resolve())
            assert parked.absolute().is_relative_to(tmp_path.absolute())
            link.rename(parked)
            make_junction()
        def scandir(path):
            return replace_child() if isinstance(path, (str, os.PathLike)) and Path(path) == staging else original(path)
        monkeypatch.setattr(os, "scandir", scandir)
    else:
        make_junction()
    pane = ChatPane(staging, {}, door=door)
    try:
        pane.mode.setCurrentIndex(pane.mode.findData("claude"))
        with pytest.raises(DoorError, match="^unusable reply$"):
            pane.remote_send("what is in staging")
        assert not captured.exists()
        assert not any(row["op"] == "agent_door_request" for row in door.log.tail(100))
    finally:
        pane.close()
        pane.deleteLater()
        application.processEvents()
        if link.is_junction():
            assert link.resolve() == outside.resolve()
            link.rmdir()
            assert outside.is_dir()
