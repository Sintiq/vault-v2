"""Per-device tokens: pairing confirmed at the PC, revoking one device, the old shared key."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from vault_v2.api import ApiServer, VaultAPI, load_or_create_key
from vault_v2.cards import CardStore
from vault_v2.devices import LEGACY_ID, TICKET_SECONDS, DeviceRegistry, PairingError
from vault_v2.health import HealthStore
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.tasks import TaskStore


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture()
def ops(tmp_path: Path) -> VaultOps:
    return VaultOps(VaultPaths(tmp_path / "vault"))


@pytest.fixture()
def reg(ops):
    clock = Clock()
    registry = DeviceRegistry(ops.paths.root, ops.log, clock=clock)
    registry.clock = clock
    return registry


def pair(reg, name="Galaxy S20", kind="app"):
    ticket = reg.new_ticket()
    claim = reg.claim(ticket, name, kind)
    device = reg.confirm(claim["claim_id"])
    status = reg.status(claim["claim_id"])
    return device, status["token"]


# -- the registry ------------------------------------------------------------------


def test_a_paired_device_opens_with_its_own_token_and_the_pc_keeps_only_a_hash(reg) -> None:
    device, token = pair(reg)
    assert reg.authenticate(token).id == device.id
    stored = reg.file.read_text(encoding="utf-8")
    assert token not in stored and device.token_sha256 in stored


def test_the_code_is_six_digits_and_the_token_is_handed_out_once(reg) -> None:
    ticket = reg.new_ticket()
    claim = reg.claim(ticket, "Phone", "app")
    assert len(claim["code"]) == 6 and claim["code"].isdigit()
    assert reg.status(claim["claim_id"]) == {"state": "waiting"}
    reg.confirm(claim["claim_id"])
    first = reg.status(claim["claim_id"])
    assert first["state"] == "confirmed" and first["token"]
    assert reg.status(claim["claim_id"]) == {"state": "expired"}, "the token is not handed out twice"


def test_a_ticket_is_single_use_and_expires(reg) -> None:
    ticket = reg.new_ticket()
    reg.claim(ticket, "Phone", "app")
    with pytest.raises(PairingError):
        reg.claim(ticket, "Someone else", "app")
    late = reg.new_ticket()
    reg.clock.t += TICKET_SECONDS + 1
    with pytest.raises(PairingError):
        reg.claim(late, "Phone", "app")


def test_a_rejected_claim_gives_no_token_and_changes_nothing(reg) -> None:
    before = reg.all()
    ticket = reg.new_ticket()
    claim = reg.claim(ticket, "Stranger", "app")
    reg.reject(claim["claim_id"])
    assert reg.status(claim["claim_id"]) == {"state": "rejected"}
    assert reg.all() == before
    with pytest.raises(PairingError):
        reg.confirm(claim["claim_id"])


def test_revoking_one_device_leaves_the_others_working(reg, ops) -> None:
    lost, lost_token = pair(reg, "Lost phone")
    kept, kept_token = pair(reg, "Tablet")
    reg.revoke(lost.id)
    assert reg.authenticate(lost_token) is None
    assert reg.authenticate(kept_token).id == kept.id
    ops_rows = [r["op"] for r in ops.log.tail(10)]
    assert ops_rows.count("device_paired") == 2 and "device_revoked" in ops_rows


def test_a_revoked_device_stays_revoked_after_a_restart(reg, ops) -> None:
    device, token = pair(reg)
    reg.revoke(device.id)
    again = DeviceRegistry(ops.paths.root, ops.log)
    assert again.authenticate(token) is None
    assert again.get(device.id).revoked_at is not None


def test_names_are_cleaned_and_bounded(reg) -> None:
    device, _ = pair(reg, "  Galaxy\n\x00 S20 " + "x" * 200)
    assert device.name.startswith("Galaxy S20") and len(device.name) == 60
    blank, _ = pair(reg, "   ", kind="browser")
    assert blank.name == "Browser" and blank.kind == "browser"


def test_the_shared_key_is_the_legacy_device_until_revoked(reg, ops) -> None:
    key = load_or_create_key(ops.paths.root, ops.log)
    assert reg.authenticate(key).id == LEGACY_ID
    assert reg.all()[0].id == LEGACY_ID
    reg.revoke(LEGACY_ID)
    assert reg.authenticate(key) is None
    assert not (ops.paths.root / ".api" / "key").exists()
    assert list((ops.paths.root / ".api").glob("key.revoked-*")), "kept aside, not deleted"
    assert all(d.id != LEGACY_ID for d in reg.all())


def test_revoking_the_device_that_opened_the_door_closes_the_door(reg, ops) -> None:
    device, _ = pair(reg)
    reg.pair_door(device, "http://100.1.2.3:8766", "k")
    door = ops.paths.root / ".door" / "config.json"
    assert reg.door_owner() == device.id
    reg.revoke(device.id)
    assert not door.exists()


def test_revoking_a_device_leaves_another_devices_door_alone(reg, ops) -> None:
    first, _ = pair(reg, "A")
    second, _ = pair(reg, "B")
    reg.pair_door(first, "http://100.1.2.3:8766", "k1")
    reg.pair_door(second, "http://100.1.2.4:8766", "k2")
    reg.revoke(first.id)
    assert reg.door_owner() == second.id, "B's door is not A's to close"


def test_a_revoked_device_cannot_pair_the_door(reg) -> None:
    device, _ = pair(reg)
    reg.revoke(device.id)
    with pytest.raises(PairingError):
        reg.pair_door(reg.get(device.id), "http://100.1.2.3:8766", "k")


def test_the_shared_key_does_not_come_back_after_revoke(reg, ops) -> None:
    key = load_or_create_key(ops.paths.root, ops.log)
    reg.use_legacy_key(key)
    reg.revoke(LEGACY_ID)
    assert load_or_create_key(ops.paths.root, ops.log) == "", "no new shared key is made"
    from vault_v2.api import existing_legacy_key
    assert existing_legacy_key(ops.paths.root) == ""
    reg.use_legacy_key("a-fresh-key-someone-passed")
    assert reg.legacy() is None and reg.authenticate("a-fresh-key-someone-passed") is None
    again = DeviceRegistry(ops.paths.root, ops.log)
    again.use_legacy_key(key)
    assert again.legacy() is None


def test_a_new_vault_gets_no_shared_key_from_the_window(ops) -> None:
    from vault_v2.api import existing_legacy_key
    assert existing_legacy_key(ops.paths.root) == ""
    assert not (ops.paths.root / ".api" / "key").exists()


def test_a_revoke_that_failed_to_save_does_not_look_done(reg, ops, monkeypatch) -> None:
    device, token = pair(reg)
    real = reg._save

    def broken(devices):
        raise OSError("disk full")

    monkeypatch.setattr(reg, "_save", broken)
    with pytest.raises(Exception):
        reg.revoke(device.id)
    assert reg.get(device.id).active, "memory still says what the disk says"
    ops.log.guard.blocked = False  # the owner cleared the blocked write at the PC
    monkeypatch.setattr(reg, "_save", real)
    reg.revoke(device.id)
    assert reg.authenticate(token) is None
    assert DeviceRegistry(ops.paths.root, ops.log).get(device.id).revoked_at is not None


def test_revoke_all_shuts_every_active_device(reg, ops) -> None:
    load_or_create_key(ops.paths.root, ops.log)
    _, a = pair(reg, "A")
    _, b = pair(reg, "B")
    assert reg.revoke_all() == 3
    assert reg.authenticate(a) is None and reg.authenticate(b) is None
    assert reg.legacy() is None


def test_the_vault_id_is_made_once(reg, ops) -> None:
    first = reg.vault_id()
    assert first == DeviceRegistry(ops.paths.root, ops.log).vault_id()


# -- over the wire -----------------------------------------------------------------


@pytest.fixture()
def server(ops, tmp_path: Path):
    cards = CardStore(ops.paths.root / ".cards", ops.log)
    tasks = TaskStore(ops.paths.root / ".tasks", ops.log)
    health = HealthStore(ops.paths.root / ".health", ops.log)
    api = VaultAPI(ops, cards, tasks, health)
    page = tmp_path / "index.html"
    page.write_text("<html>vault</html>", encoding="utf-8")
    srv = ApiServer(api, load_or_create_key(ops.paths.root, ops.log), page,
                    lambda text: "answer: " + text, lambda: {"backend": "fake", "ready": True, "history": []},
                    "127.0.0.1", 0)
    srv.start()
    yield srv, api
    srv.stop()


def call(srv, method: str, path: str, body=None, token: str | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(srv.url.rstrip("/") + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def status_code(fn) -> int:
    try:
        fn()
    except urllib.error.HTTPError as exc:
        return exc.code
    return 200


def test_pairing_end_to_end_over_http(server) -> None:
    srv, api = server
    ticket = api.devices.new_ticket()
    claim = call(srv, "POST", "/api/pair/claim", {"ticket": ticket, "name": "Galaxy S20", "kind": "app"})
    assert api.devices.waiting_claim(ticket)["code"] == claim["code"], "the PC shows the same code"
    assert call(srv, "POST", "/api/pair/status", {"claim_id": claim["claim_id"]}) == {"state": "waiting"}
    api.devices.confirm(claim["claim_id"])
    done = call(srv, "POST", "/api/pair/status", {"claim_id": claim["claim_id"]})
    token = done["token"]
    session = call(srv, "GET", "/api/session", token=token)
    assert session["device_id"] == done["device_id"] and session["api_version"] == 2
    assert session["vault_id"] and session["legacy"] is False
    assert call(srv, "GET", "/api/files?pane=staging", token=token)["pane"] == "staging"


def test_a_spent_or_unknown_ticket_is_refused_over_http(server) -> None:
    srv, api = server
    ticket = api.devices.new_ticket()
    call(srv, "POST", "/api/pair/claim", {"ticket": ticket, "name": "A"})
    assert status_code(lambda: call(srv, "POST", "/api/pair/claim", {"ticket": ticket, "name": "B"})) == 409
    assert status_code(lambda: call(srv, "POST", "/api/pair/claim", {"ticket": "made-up"})) == 409
    assert status_code(lambda: call(srv, "POST", "/api/pair/claim", ["not", "an", "object"])) == 409


def test_a_revoked_token_is_shut_out_of_every_protected_route(server) -> None:
    srv, api = server
    device, token = pair(api.devices)
    (api.ops.paths.staging / "a.txt").write_text("hello", encoding="utf-8")
    api.devices.revoke(device.id)
    for method, path, body in (("GET", "/api/session", None), ("GET", "/api/files?pane=staging", None),
                               ("GET", "/api/blob?pane=staging&rel=a.txt", None), ("GET", "/api/tasks", None),
                               ("GET", "/api/health", None), ("POST", "/api/door/pair",
                                                             {"address": "http://100.1.2.3:8766", "key": "k"}),
                               ("POST", "/api/device/self-wipe", {})):
        assert status_code(lambda: call(srv, method, path, body, token)) == 401, path


def test_a_device_token_never_opens_anything_from_the_url(server) -> None:
    srv, api = server
    _, token = pair(api.devices, kind="browser")
    (api.ops.paths.staging / "a.png").write_bytes(bytes([0x89]) + b"PNG fake")
    req = urllib.request.Request(srv.url.rstrip("/") + "/api/blob?pane=staging&rel=a.png",
                                 headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.read() == bytes([0x89]) + b"PNG fake"
    for bad in ("/api/blob?pane=staging&rel=a.png&t=" + token, "/api/files?pane=staging&t=" + token,
                "/api/files?pane=staging&key=" + token):
        assert status_code(lambda: urllib.request.urlopen(srv.url.rstrip("/") + bad, timeout=5)) == 401, bad


def test_the_old_shared_key_still_opens_until_the_owner_revokes_it(server) -> None:
    srv, api = server
    assert call(srv, "GET", "/api/session", token=srv.key)["legacy"] is True
    with urllib.request.urlopen(srv.url.rstrip("/") + "/api/files?pane=staging&key=" + srv.key, timeout=5) as r:
        assert r.status == 200
    api.devices.revoke(LEGACY_ID)
    assert status_code(lambda: call(srv, "GET", "/api/session", token=srv.key)) == 401
    assert status_code(lambda: urllib.request.urlopen(
        srv.url.rstrip("/") + "/api/files?pane=staging&key=" + srv.key, timeout=5)) == 401


def test_a_phone_that_says_it_wipes_itself_is_revoked_and_the_receipt_says_requested(server) -> None:
    srv, api = server
    device, token = pair(api.devices)
    assert call(srv, "POST", "/api/device/self-wipe", {}, token) == {"revoked": True}
    assert api.devices.get(device.id).revoked_at is not None
    assert "device_self_wipe_requested" in [r["op"] for r in api.ops.log.tail(5)]


def test_door_pairing_remembers_the_device(server) -> None:
    srv, api = server
    device, token = pair(api.devices)
    call(srv, "POST", "/api/door/pair", {"address": "http://100.1.2.3:8766", "key": "door-key"}, token)
    assert api.devices.door_owner() == device.id
    api.devices.revoke(device.id)
    assert not (api.ops.paths.root / ".door" / "config.json").exists()


def test_ping_says_only_that_something_answers_and_which_vault(server) -> None:
    srv, _api = server
    with urllib.request.urlopen(srv.url.rstrip("/") + "/api/ping", timeout=5) as r:
        body = json.loads(r.read())
    assert body == {"ok": True, "name": "Vault V2", "api_version": 2, "vault_id": ""}, "a ping writes nothing"
    made = _api.devices.vault_id()
    with urllib.request.urlopen(srv.url.rstrip("/") + "/api/ping", timeout=5) as r:
        assert json.loads(r.read())["vault_id"] == made


def raw_post(srv, path: str, head: bytes, body: bytes = b"", *, wait: float = 8.0) -> bytes:
    import socket
    crlf = bytes([13, 10])
    with socket.create_connection((srv.host, srv.port), timeout=wait) as sock:
        sock.sendall(b"POST " + path.encode() + b" HTTP/1.1" + crlf + b"Host: x" + crlf + head + crlf + body)
        try:
            return sock.recv(200)
        except (TimeoutError, OSError):
            return b""


def line(text: str) -> bytes:
    return text.encode() + bytes([13, 10])


def test_the_pairing_calls_refuse_a_bad_length_before_reading(server) -> None:
    srv, _api = server
    assert b" 411 " in raw_post(srv, "/api/pair/claim", line("Content-Length: -1"))
    assert b" 411 " in raw_post(srv, "/api/pair/claim", b"")
    assert b" 413 " in raw_post(srv, "/api/pair/claim", line("Content-Length: 5000"))


def test_a_pairing_body_that_never_arrives_does_not_hold_the_server(server) -> None:
    import time as _t
    srv, _api = server
    started = _t.monotonic()
    raw_post(srv, "/api/pair/claim", line("Content-Length: 100"), b"{", wait=10)
    assert _t.monotonic() - started < 9, "the handler gave up on the missing body"
    _t.sleep(0.3)
    assert srv._httpd.active_requests == 0


def test_a_door_left_behind_by_a_failed_cleanup_is_removed_on_the_next_revoke(reg, ops, monkeypatch) -> None:
    lost, _ = pair(reg, "Lost")
    reg.pair_door(lost, "http://100.1.2.3:8766", "k")
    real_drop = reg._drop_door_of
    monkeypatch.setattr(reg, "_drop_door_of", lambda device_id: (_ for _ in ()).throw(OSError("locked file")))
    with pytest.raises(Exception):
        reg.revoke(lost.id)
    ops.log.guard.blocked = False
    monkeypatch.setattr(reg, "_drop_door_of", real_drop)
    from vault_v2.reminders import door_settings
    assert door_settings(ops.paths.root) is None, "no reminder knocks on a revoked device's door"
    again = DeviceRegistry(ops.paths.root, ops.log)
    again.revoke(lost.id)
    assert not again.door_config.exists()


def test_a_pairing_body_that_trickles_in_is_cut_at_five_seconds_in_all(server) -> None:
    import socket
    import time as _t
    srv, _api = server
    with socket.create_connection((srv.host, srv.port), timeout=12) as sock:
        crlf = bytes([13, 10])
        sock.sendall(b"POST /api/pair/claim HTTP/1.1" + crlf + b"Host: x" + crlf +
                     b"Content-Length: 40" + crlf + crlf)
        started = _t.monotonic()
        closed = False
        for _ in range(20):
            try:
                sock.sendall(b" ")
            except OSError:
                closed = True
                break
            _t.sleep(0.8)
            if _t.monotonic() - started > 9:
                break
        elapsed = _t.monotonic() - started
    assert closed or elapsed < 9.5
    _t.sleep(0.5)
    assert srv._httpd.active_requests == 0, "the handler ended at the overall deadline"
