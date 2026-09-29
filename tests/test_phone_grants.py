"""Files from Staging to the phone: one grant, the same bytes, honest receipts."""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from vault_v2.api import ApiServer, VaultAPI
from vault_v2.cards import CardStore
from vault_v2.devices import LEGACY_ID
from vault_v2.health import HealthStore
from vault_v2.ops import VaultError, VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.phone_grants import GRANT_SECONDS, PhoneGrants
from vault_v2.tasks import TaskStore


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture()
def api(tmp_path: Path) -> VaultAPI:
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    return VaultAPI(ops, CardStore(ops.paths.root / ".cards", ops.log), TaskStore(ops.paths.root / ".tasks", ops.log),
                    HealthStore(ops.paths.root / ".health", ops.log))


def paired(api, name="Phone"):
    ticket = api.devices.new_ticket()
    claim = api.devices.claim(ticket, name, "app")
    device = api.devices.confirm(claim["claim_id"])
    return device, api.devices.status(claim["claim_id"])["token"]


def test_a_staging_file_goes_to_its_device_with_its_hash_and_one_receipt(api) -> None:
    (api.ops.paths.staging / "passport.pdf").write_bytes(b"%PDF synthetic")
    phone, _ = paired(api)
    g = api.phone.grant(phone, "passport.pdf", "offline")
    assert g["sha256"] == hashlib.sha256(b"%PDF synthetic").hexdigest() and g["size"] == 14
    data, name = api.phone.blob(phone, g["grant_id"])
    assert data == b"%PDF synthetic" and name == "passport.pdf"
    with pytest.raises(VaultError):
        api.phone.blob(phone, g["grant_id"]), "a grant delivers once"
    ops = [r["op"] for r in api.ops.log.tail(10)]
    assert ops.count("phone_offline_copy") == 1


def test_only_staging_and_only_visible_files(api) -> None:
    (api.ops.paths.documents / "tax.pdf").write_bytes(b"x")
    phone, _ = paired(api)
    documents = str(api.ops.paths.documents / "tax.pdf")
    rooted = "\\" + documents.split(":", 1)[-1].lstrip("\\")
    for rel in ("../documents/tax.pdf", "/etc/passwd", "", None, "missing.pdf", documents, rooted,
                "C:tax.pdf", "\\\\server\\share\\tax.pdf", "a//b.pdf", "./x.pdf"):
        with pytest.raises((VaultError, OSError)):
            api.phone.grant(phone, rel, "save")


def test_a_file_changed_since_the_phone_saw_it_is_refused(api) -> None:
    f = api.ops.paths.staging / "a.txt"
    f.write_bytes(b"one")
    phone, _ = paired(api)
    with pytest.raises(VaultError):
        api.phone.grant(phone, "a.txt", "share", sha256=hashlib.sha256(b"old").hexdigest())
    g = api.phone.grant(phone, "a.txt", "share")
    f.write_bytes(b"two")
    with pytest.raises(VaultError):
        api.phone.blob(phone, g["grant_id"])


def test_a_grant_belongs_to_one_device_and_expires(api) -> None:
    (api.ops.paths.staging / "a.txt").write_bytes(b"x")
    phone, _ = paired(api, "A")
    other, _ = paired(api, "B")
    clock = Clock()
    grants = PhoneGrants(api.ops, clock=clock)
    g = grants.grant(phone, "a.txt", "offline")
    with pytest.raises(VaultError):
        grants.blob(other, g["grant_id"])
    clock.t += GRANT_SECONDS + 1
    with pytest.raises(VaultError):
        grants.blob(phone, g["grant_id"])


def test_the_shared_key_cannot_take_files_to_a_phone(api) -> None:
    (api.ops.paths.staging / "a.txt").write_bytes(b"x")
    legacy = type("D", (), {"id": LEGACY_ID, "name": "shared"})()
    with pytest.raises(PermissionError):
        api.phone.grant(legacy, "a.txt", "save")


def test_the_outcome_is_the_phones_report_and_never_says_sent(api) -> None:
    (api.ops.paths.staging / "a.txt").write_bytes(b"x")
    phone, _ = paired(api)
    g = api.phone.grant(phone, "a.txt", "share")
    with pytest.raises(VaultError):
        api.phone.outcome(phone, g["grant_id"], "handed_to", "com.google.android.gm"), "nothing sent yet"
    api.phone.blob(phone, g["grant_id"])
    assert api.phone.outcome(phone, g["grant_id"], "handed_to", "com.google.android.gm; rm -rf") == {"recorded": True}
    assert api.phone.outcome(phone, g["grant_id"], "cancelled") == {"recorded": False}, "one report per grant"
    with pytest.raises(VaultError):
        api.phone.outcome(phone, g["grant_id"], "sent")
    row = api.ops.log.tail(1)[0]
    extra = row["extra"] if isinstance(row["extra"], dict) else json.loads(row["extra"])
    assert row["op"] == "phone_export_outcome" and extra["outcome"] == "handed_to"
    assert extra["target"] == "com.google.android.gmrm-rf" and "not observed" in row["note"]


# -- over the wire ------------------------------------------------------------------


@pytest.fixture()
def server(api, tmp_path: Path):
    page = tmp_path / "index.html"
    page.write_text("<html></html>", encoding="utf-8")
    srv = ApiServer(api, "", page, lambda t: "", lambda: {}, "127.0.0.1", 0)
    srv.start()
    yield srv
    srv.stop()


def call(srv, method, path, body=None, token=None):
    req = urllib.request.Request(srv.url.rstrip("/") + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.read()


def code(fn) -> int:
    try:
        fn()
    except urllib.error.HTTPError as exc:
        return exc.code
    return 200


def test_over_http_grant_blob_outcome_and_revoke(api, server) -> None:
    (api.ops.paths.staging / "note.txt").write_bytes(b"synthetic")
    phone, token = paired(api)
    g = json.loads(call(server, "POST", "/api/phone/grant", {"rel": "note.txt", "action": "offline"}, token))
    assert call(server, "GET", "/api/phone/blob?grant=" + g["grant_id"], token=token) == b"synthetic"
    assert code(lambda: call(server, "GET", "/api/phone/blob?grant=" + g["grant_id"])) == 401
    api.devices.revoke(phone.id)
    assert code(lambda: call(server, "POST", "/api/phone/grant", {"rel": "note.txt", "action": "offline"}, token)) == 401
    assert code(lambda: call(server, "GET", "/api/phone/blob?grant=" + g["grant_id"], token=token)) == 401


def test_two_requests_racing_for_one_grant_deliver_once_with_one_receipt(api) -> None:
    import threading
    (api.ops.paths.staging / "a.txt").write_bytes(b"x" * 1000)
    phone, _ = paired(api)
    g = api.phone.grant(phone, "a.txt", "save")
    results = []

    def take():
        try:
            results.append(api.phone.blob(phone, g["grant_id"])[0])
        except VaultError:
            results.append(None)

    threads = [threading.Thread(target=take) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r is not None for r in results) == 1
    assert [r["op"] for r in api.ops.log.tail(10)].count("phone_export_issued") == 1


def test_issue_and_outcome_share_the_grant_id(api) -> None:
    (api.ops.paths.staging / "a.txt").write_bytes(b"x")
    phone, _ = paired(api)
    g = api.phone.grant(phone, "a.txt", "share")
    api.phone.blob(phone, g["grant_id"])
    api.phone.outcome(phone, g["grant_id"], "unknown")
    rows = [r for r in api.ops.log.tail(5) if r["op"] in ("phone_export_issued", "phone_export_outcome")]
    extras = [r["extra"] if isinstance(r["extra"], dict) else json.loads(r["extra"]) for r in rows]
    assert {e["grant"] for e in extras} == {g["grant_id"]}
    assert extras[-1]["outcome"] == "unknown"
