"""The chat door's public lifecycle, audited in a synthetic Vault."""

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError

import pytest

from vault_v2.agent_door import AgentCommand, AgentDoor, DoorError
from vault_v2.receipts import ReceiptLog


def test_door_defaults_closed_and_requires_explicit_owner_selection(tmp_path):
    log = ReceiptLog(tmp_path / ".receipts")
    door = AgentDoor(log, {})
    assert door.agent is None
    with pytest.raises(DoorError, match="door is closed"):
        door.request("system", [{"role": "user", "content": "synthetic question"}])
    assert log.tail() == []
    door.select("claude")
    assert door.agent == "claude"
    assert "Anthropic" in door.label
    door.close()
    assert door.agent is None
    assert [row["op"] for row in log.tail()] == ["agent_door_open", "agent_door_close"]
    assert log.verify() == 2


def test_request_is_stdin_only_and_audited_without_body_or_agent_self_claim(tmp_path):
    script = tmp_path / "answer.py"
    script.write_text(
        "import json,sys,os\n"
        "packet=json.load(sys.stdin)\n"
        "assert os.listdir('.') == []\n"
        "assert packet == {'system':'files: made-up.txt','messages':[{'role':'user','content':'count'}]}\n"
        "print(json.dumps({'type':'result','subtype':'success','is_error':False,'result':'One file.',"
        "'agent':'pretend-provider'}))\n", encoding="utf-8")
    log = ReceiptLog(tmp_path / ".receipts")
    door = AgentDoor(log, {}, command=lambda *_: AgentCommand([sys.executable, str(script)]))
    door.select("claude")
    assert door.request("files: made-up.txt", [{"role": "user", "content": "count"}], files_count=1) == "One file."
    rows = log.tail()
    assert [r["op"] for r in rows] == ["agent_door_open", "agent_door_request", "agent_door_result"]
    assert rows[1]["extra"]["files_count"] == 1
    assert rows[2]["extra"]["status"] == "accepted"
    assert all(r["extra"]["agent"] == "claude" for r in rows)
    assert "made-up.txt" not in json.dumps(rows)
    assert "One file." not in json.dumps(rows)
    assert len(rows[1]["sha256"]) == len(rows[2]["sha256"]) == 64
    assert not door.busy


@pytest.mark.parametrize("close_method", ["close", "select-local"])
def test_close_wins_over_an_open_waiting_for_its_audit_write(tmp_path, close_method):
    log = ReceiptLog(tmp_path / ".receipts")
    door = AgentDoor(log, {})
    with ThreadPoolExecutor() as pool:
        with log.write("synthetic concurrent writer"):
            future = pool.submit(door.select, "claude")
            deadline = time.monotonic() + 3
            while door.generation == 0 and time.monotonic() < deadline:
                time.sleep(.01)
            assert door.generation > 0
            if close_method == "close":
                door.close()
            else:
                door.select(None)
        with pytest.raises(DoorError, match="cancelled"):
            future.result(timeout=5)
    assert door.agent is None


@pytest.mark.parametrize("output", [
    '{"type":"result","subtype":"success","is_error":false,"result":"safe","result":"other"}',
    '{"type":"result","subtype":"success","is_error":false,"result":"safe","cost":NaN}',
    json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "x" * 65537}),
], ids=["duplicate-key", "non-finite-json", "oversized-text"])
def test_ambiguous_or_unbounded_result_is_refused_without_raw_output(tmp_path, output):
    script = tmp_path / "invalid.py"
    script.write_text("import sys\nsys.stdin.read()\nprint(" + repr(output) + ")\n", encoding="utf-8")
    log = ReceiptLog(tmp_path / ".receipts")
    door = AgentDoor(log, {}, command=lambda *_: AgentCommand([sys.executable, str(script)]))
    door.select("claude")
    with pytest.raises(DoorError, match="^unusable reply$"):
        door.request("synthetic", [])
    assert log.tail()[-1]["extra"]["status"] == "unusable"


def test_queued_request_cannot_borrow_a_reopened_door(tmp_path):
    log = ReceiptLog(tmp_path / ".receipts")
    door = AgentDoor(log, {})
    door.select("claude")
    old_generation = door.generation
    door.close()
    door.select("claude")
    with pytest.raises(DoorError, match="cancelled"):
        door.request("old", [], expected_generation=old_generation)
    assert not any(row["op"] == "agent_door_request" for row in log.tail())


def _wait_file(path):
    deadline = time.monotonic() + 5
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(.01)
    assert path.exists(), "synthetic subprocess never reached its marker"


def _slow_door(tmp_path, *, timeout_s=180):
    marker, release = tmp_path / "started", tmp_path / "release"
    script = tmp_path / "slow.py"
    script.write_text(
        "import sys,time,json\nfrom pathlib import Path\nsys.stdin.read()\n"
        f"Path({str(marker)!r}).touch()\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(.01)\n"
        "print(json.dumps({'type':'result','subtype':'success','is_error':False,'result':'late result'}))\n",
        encoding="utf-8")
    log = ReceiptLog(tmp_path / ".receipts")
    door = AgentDoor(log, {}, command=lambda *_: AgentCommand([sys.executable, str(script)]), timeout_s=timeout_s)
    door.select("claude")
    return door, log, marker, release


def test_cancel_reopen_does_not_accept_old_answer_or_allow_a_second_process(tmp_path):
    door, log, marker, release = _slow_door(tmp_path)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(door.request, "synthetic", [])
        _wait_file(marker)
        with pytest.raises(DoorError, match="door is busy"):
            door.request("second", [])
        door.close()
        door.select("claude")
        release.touch()
        with pytest.raises(DoorError, match="cancelled"):
            future.result(timeout=5)
    assert len([r for r in log.tail() if r["op"] == "agent_door_request"]) == 1
    assert log.tail()[-1]["extra"]["status"] == "cancelled"
    assert not door.busy


def test_request_receipt_failure_prevents_launch(tmp_path):
    door, log, marker, _ = _slow_door(tmp_path)
    log.read_only = True
    with pytest.raises(DoorError, match="receipt unavailable"):
        door.request("synthetic", [])
    assert not marker.exists()
    assert not door.busy


def test_http_initial_audit_remains_fail_fast_under_a_busy_root(tmp_path):
    door, log, marker, _ = _slow_door(tmp_path)

    def http_request():
        with log.nonblocking():
            return door.request("synthetic", [])

    with ThreadPoolExecutor(1) as pool:
        with log.write("synthetic concurrent startup read or writer"):
            future = pool.submit(http_request)
            with pytest.raises(DoorError, match="receipt unavailable"):
                future.result(timeout=2)
    assert not marker.exists()
    assert not any(r["op"] == "agent_door_request" for r in log.tail())
    assert not door.busy


def test_result_receipt_failure_prevents_returning_answer(tmp_path):
    door, log, marker, release = _slow_door(tmp_path)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(door.request, "synthetic", [])
        _wait_file(marker)
        log.read_only = True
        release.touch()
        with pytest.raises(DoorError, match="receipt unavailable"):
            future.result(timeout=5)
    assert not any(r["op"] == "agent_door_result" for r in log.tail())
    assert not door.busy


@pytest.mark.parametrize("cancelled", [False, True])
def test_started_request_completes_audit_after_transient_http_guard_contention(tmp_path, cancelled):
    door, log, marker, release = _slow_door(tmp_path)

    def http_request():
        with log.nonblocking():  # The real HTTP handler's admission policy.
            return door.request("synthetic", [])

    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(http_request)
        _wait_file(marker)
        with log.write("synthetic concurrent close or writer"):
            if cancelled:
                door.close()
            release.touch()
            # A completed model/process must await its outcome receipt rather
            # than confuse normal root contention with journal failure.
            with pytest.raises(TimeoutError):
                future.result(timeout=.5)
        if cancelled:
            with pytest.raises(DoorError, match="cancelled"):
                future.result(timeout=5)
        else:
            assert future.result(timeout=5) == "late result"
    rows = [r for r in log.tail() if r["op"] == "agent_door_result"]
    assert len(rows) == 1
    assert rows[0]["extra"]["status"] == ("cancelled" if cancelled else "accepted")
    assert not door.busy
