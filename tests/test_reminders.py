"""Reminders: due dates become a knock on the phone, once a day, receipted."""

from __future__ import annotations

import json
import socket
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from vault_v2 import reminders
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.reminders import Reminder, RemindedLog, due_reminders, remind
from vault_v2.tasks import Task, TaskStore

TODAY = date(2026, 9, 21)


def task(tid: str, title: str, due: str | None, done: bool = False) -> Task:
    return Task(tid, "a" * 64, "doc.txt", title, due, f"Due {due}" if due else "No date", "AGENT", done)


def test_what_counts_as_due() -> None:
    tasks = [
        task("t1", "Pay the bill", "2026-09-22"),        # tomorrow
        task("t2", "Refill", "2026-09-21"),              # today
        task("t3", "Renew passport", "2026-09-10"),      # overdue
        task("t4", "Later", "2026-10-30"),               # not yet
        task("t5", "Done already", "2026-09-21", done=True),
        task("t6", "No date", None),
        task("t7", "Bad date", "someday"),
    ]
    due = due_reminders(tasks, TODAY)
    assert [r.title for r in due] == ["Renew passport", "Refill", "Pay the bill"], "most urgent first"
    assert [r.when for r in due] == ["11 day(s) overdue", "due today", "due tomorrow"]


def test_once_a_day_per_task(tmp_path: Path) -> None:
    seen = RemindedLog(tmp_path)
    assert not seen.already("t1", TODAY)
    seen.mark("t1", TODAY)
    assert seen.already("t1", TODAY)
    assert not seen.already("t1", date(2026, 9, 22)), "a new day is a new reminder"
    assert RemindedLog(tmp_path).already("t1", TODAY), "remembered on disk"


@pytest.fixture()
def vault(tmp_path: Path):
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    store = TaskStore(ops.paths.root / ".tasks", ops.log)
    store.add(task("t1", "Pay the bill", "2026-09-22"))
    store.add(task("t2", "Later", "2026-12-01"))
    return ops, store


def test_no_phone_paired_is_said_not_raised(vault) -> None:
    ops, store = vault
    assert remind(ops.paths.root, store, ops.log, TODAY) == "reminders: 1 due, but no phone is paired"
    assert not any(r["op"] == "remind" for r in ops.log.tail(5))


def test_a_reminder_reaches_the_phone_once_and_is_receipted(vault, monkeypatch) -> None:
    ops, store = vault
    door = ops.paths.root / ".door"
    door.mkdir()
    (door / "config.json").write_text(json.dumps({"address": "http://phone:8779", "key": "k"}), encoding="utf-8")

    knocks: list[dict] = []

    def fake_knock(address, key, items):
        knocks.append({"address": address, "key": key, "items": items})
        return {"shown": True}

    monkeypatch.setattr(reminders, "knock", fake_knock)

    assert remind(ops.paths.root, store, ops.log, TODAY) == "reminders: 1 sent to the phone"
    assert knocks[0]["address"] == "http://phone:8779" and knocks[0]["key"] == "k"
    assert [r.title for r in knocks[0]["items"]] == ["Pay the bill"]
    receipt = ops.log.tail(1)[0]
    assert receipt["op"] == "remind" and receipt["extra"]["task"] == "t1" and "due tomorrow" in receipt["note"]

    # Same day again: nothing knocks twice.
    assert remind(ops.paths.root, store, ops.log, TODAY) == "reminders: 1 due, already sent today"
    assert len(knocks) == 1


def test_a_silent_phone_does_not_mark_the_reminder_as_sent(vault, monkeypatch) -> None:
    ops, store = vault
    door = ops.paths.root / ".door"
    door.mkdir()
    (door / "config.json").write_text(json.dumps({"address": "http://phone:8779", "key": "k"}), encoding="utf-8")

    def dead(address, key, items):
        raise OSError("no route to host")

    monkeypatch.setattr(reminders, "knock", dead)
    status = remind(ops.paths.root, store, ops.log, TODAY)
    assert status.startswith("reminders: 1 due, phone did not answer")
    assert not RemindedLog(ops.paths.root / ".tasks").already("t1", TODAY), "try again next round"


def test_the_knock_carries_no_more_than_the_task(monkeypatch) -> None:
    sent = {}

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"shown": true}'

    def fake_open(self, req, data=None, timeout=0):
        sent["url"] = req.full_url
        sent["body"] = json.loads(req.data)
        sent["auth"] = req.get_header("Authorization")
        return Resp()

    monkeypatch.setattr(reminders.urllib.request.OpenerDirector, "open", fake_open)
    reminders.knock("http://100.64.0.1:8779", "k", [Reminder("t1", "Pay the bill", "2026-09-22", 1)])
    assert sent["url"] == "http://100.64.0.1:8779/door/remind" and sent["auth"] == "Bearer k"
    assert sent["body"] == {"count": 1, "items": [{"title": "Pay the bill", "due": "2026-09-22", "when": "due tomorrow"}]}


@pytest.mark.parametrize("address", [
    "http://192.168.1.5:8779", "http://100.63.255.255:8779", "http://100.128.0.0:8779",
    "http://127.0.0.1:8779", "http://phone:8779", "https://100.64.0.1:8779",
    "http://100.64.0.1", "http://100.64.0.1:0", "http://100.64.0.1:65536",
    "http://100.64.0.1:8779/path", "http://100.64.0.1:8779?secret=synthetic",
    "http://100.64.0.1:8779#fragment", "http://user@100.64.0.1:8779",
    "http://100.64.0.1:8779@192.0.2.1:8080", "http://100.64.0.1:8779\\evil",
    " http://100.64.0.1:8779", "http://100.64.0.1:8779\n", "http://100.64.0.1:\t8779",
    "http://100.064.0.1:8779", "http://[::ffff:100.64.0.1]:8779", "http://1681915905:8779",
    "http://100.64.0.1.:8779", "http://100.64.0.999:8779", "",
])
def test_knock_refuses_non_tailnet_before_any_request(monkeypatch, address) -> None:
    requests = []

    def capture_request(*args, **kwargs):
        requests.append((args, kwargs))
        raise AssertionError("an invalid destination reached the transport")

    monkeypatch.setattr(reminders.urllib.request.OpenerDirector, "open", capture_request)
    with pytest.raises(ValueError, match="100.64.0.0/10"):
        reminders.knock(address, "synthetic-key", [])
    assert requests == []


@pytest.fixture()
def wire(monkeypatch):
    """Actual HTTP processing, with every synthetic destination dialed on loopback."""
    records = []
    dials = []
    response = {"status": 200, "location": "http://192.0.2.1:8080/stolen"}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            records.append((self.command, self.path, dict(self.headers), body))
            self.send_response(response["status"])
            if response["status"] != 200:
                self.send_header("Location", response["location"])
            self.end_headers()
            self.wfile.write(b'{"shown": true}')

        do_GET = do_POST

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    real_connect = socket.create_connection

    def loopback_connect(address, *args, **kwargs):
        dials.append(address)
        return real_connect(server.server_address, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", loopback_connect)
    # Deliberately hostile proxy settings, never an actual external destination.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.2:9999")
    monkeypatch.setenv("http_proxy", "http://127.0.0.2:9999")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setattr(reminders.urllib.request, "_opener", None)
    try:
        yield records, dials, response
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_knock_dials_tailnet_directly_despite_environment_proxy(wire) -> None:
    records, dials, _ = wire
    assert reminders.knock("http://100.64.0.1:8779", "synthetic-key", []) == {"shown": True}
    assert dials == [("100.64.0.1", 8779)]
    assert len(records) == 1
    method, path, headers, body = records[0]
    assert (method, path) == ("POST", "/door/remind")
    assert headers["Authorization"] == "Bearer synthetic-key"
    assert b"synthetic-key" not in body and "synthetic-key" not in path


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("location", ["http://192.0.2.1:8080/stolen", "http://100.64.0.2:8779/other", "/same-phone"])
def test_knock_never_follows_redirects_or_forwards_the_key(wire, code, location) -> None:
    records, dials, response = wire
    response["status"] = code
    response["location"] = location
    with pytest.raises(reminders.urllib.error.HTTPError):
        reminders.knock("http://100.64.0.1:8779", "synthetic-key", [])
    assert dials == [("100.64.0.1", 8779)]
    assert len(records) == 1


def test_bad_pairing_is_reported_as_blocked_not_an_unanswered_phone(vault, monkeypatch) -> None:
    ops, store = vault
    door = ops.paths.root / ".door"
    door.mkdir()
    (door / "config.json").write_text(
        json.dumps({"address": "http://192.168.1.5:8779", "key": "synthetic-key"}), encoding="utf-8",
    )

    def forbidden_request(*args, **kwargs):
        raise AssertionError("invalid pairing reached the transport")

    monkeypatch.setattr(reminders.urllib.request.OpenerDirector, "open", forbidden_request)
    assert remind(ops.paths.root, store, ops.log, TODAY) == (
        "reminders: 1 due, blocked phone address (requires http://<100.64.0.0/10>:port)"
    )
    assert not RemindedLog(ops.paths.root / ".tasks").already("t1", TODAY)
    assert not any(row["op"] == "remind" for row in ops.log.tail(5))


@pytest.mark.parametrize("address", ["http://100.64.0.0:1", "http://100.127.255.255:65535/"])
def test_knock_accepts_exact_tailnet_edges_and_explicit_ports(wire, address) -> None:
    records, dials, _ = wire
    assert reminders.knock(address, "synthetic-key", []) == {"shown": True}
    assert len(dials) == len(records) == 1
    assert records[0][1] == "/door/remind"


@pytest.mark.parametrize("key", ["SYNTHETIC-KEY\nbad", "SYNTHETIC-KEY\rbad", "SYNTHETIC-KEY\x00bad", "SYNTHETIC-KEY secret", "ключ"])
def test_invalid_pairing_key_is_never_echoed_or_sent(vault, wire, key) -> None:
    ops, store = vault
    records, dials, _ = wire
    door = ops.paths.root / ".door"
    door.mkdir()
    (door / "config.json").write_text(
        json.dumps({"address": "http://100.64.0.1:8779", "key": key}), encoding="utf-8",
    )
    status = remind(ops.paths.root, store, ops.log, TODAY)
    assert status == "reminders: 1 due, invalid phone key — pair the phone again"
    assert "SYNTHETIC-KEY" not in status and "ключ" not in status
    assert records == [] and dials == []
    assert not RemindedLog(ops.paths.root / ".tasks").already("t1", TODAY)


def test_transport_error_details_cannot_echo_a_key_into_status(vault, monkeypatch) -> None:
    ops, store = vault
    door = ops.paths.root / ".door"
    door.mkdir()
    (door / "config.json").write_text(
        json.dumps({"address": "http://100.64.0.1:8779", "key": "SYNTHETIC-KEY"}), encoding="utf-8",
    )

    def transport_error(*args, **kwargs):
        raise OSError("transport diagnostic with SYNTHETIC-KEY")

    monkeypatch.setattr(reminders.urllib.request.OpenerDirector, "open", transport_error)
    assert remind(ops.paths.root, store, ops.log, TODAY) == "reminders: 1 due, phone did not answer (OSError)"
