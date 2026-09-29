"""Root write coordination at phone, export and backup public boundaries."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request

import pytest

from vault_v2.api import ApiServer, VaultAPI, load_or_create_key
from vault_v2.agent_api import AgentAPI
from vault_v2.agent import Backend, BackendInfo
from vault_v2.backup import make_backup, restore_backup
from vault_v2.cards import CardStore
from vault_v2.errors import InvalidJournal, MayHaveApplied, VaultWriteBlocked
from vault_v2.gatekeeper import Gatekeeper
from vault_v2.health import HealthStore
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.reader import StagingReader
from vault_v2.tasks import Task, TaskStore


@pytest.fixture()
def vault(tmp_path):
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    api = VaultAPI(ops, CardStore(ops.paths.root / ".cards", ops.log),
                   TaskStore(ops.paths.root / ".tasks", ops.log),
                   HealthStore(ops.paths.root / ".health", ops.log))
    return ops, api


def damage_chain(ops):
    ops.log.append("synthetic", "before", "after")
    rows = ops.log.file.read_text(encoding="utf-8")
    ops.log.file.write_text(rows.replace('"before"', '"tampered"'), encoding="utf-8")


@pytest.fixture()
def server(vault, tmp_path):
    ops, api = vault
    page = tmp_path / "index.html"
    page.write_text("synthetic only", encoding="utf-8")
    server = ApiServer(api, "synthetic-key", page, lambda _: "unused", lambda: {}, "127.0.0.1", 0)
    server.start()
    try:
        yield server, ops, api
    finally:
        server.stop()


def request(server, path, *, payload=None, raw=None, timeout=5):
    data = json.dumps(payload).encode() if payload is not None else raw
    req = urllib.request.Request(server.url.rstrip("/") + path, data=data,
                                 headers={"Authorization": "Bearer synthetic-key",
                                          "Content-Type": "application/json",
                                          "X-Filename": "synthetic.txt"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_upload_refuses_damaged_chain_before_any_landing_file(vault):
    ops, api = vault
    damage_chain(ops)
    with pytest.raises(InvalidJournal):
        api.upload("synthetic.txt", b"synthetic bytes")
    assert not (ops.paths.root / ".api" / "incoming").exists()
    assert list(ops.paths.staging.iterdir()) == []


@pytest.mark.parametrize("action, service", [("pair", ".door"), ("key", ".api")])
def test_pairing_and_key_creation_precheck_before_secret_files(vault, action, service):
    ops, api = vault
    damage_chain(ops)
    with pytest.raises(InvalidJournal):
        if action == "pair":
            api.devices.use_legacy_key("synthetic-key")
            api.pair_door("http://127.0.0.1:9876", "synthetic-secret", api.devices.legacy())
        else:
            load_or_create_key(ops.paths.root)
    assert not (ops.paths.root / service).exists()


@pytest.mark.parametrize("mode", ["folder", "zip"])
def test_export_prechecks_chain_without_burning_approval_or_creating_destination(vault, tmp_path, mode):
    ops, _api = vault
    source = ops.paths.staging / "synthetic.txt"
    source.write_bytes(b"synthetic export")
    gate = Gatekeeper(ops)
    request = gate.prepare([source], tmp_path / "outside" / "bundle", mode)
    approval = gate.approve(request)
    damage_chain(ops)
    with pytest.raises(InvalidJournal):
        gate.execute(request)
    assert not (tmp_path / "outside").exists()
    assert not approval.used


def test_http_upload_refuses_busy_root_without_waiting_or_writing(server):
    server, ops, _api = server
    entered, release = threading.Event(), threading.Event()

    def hold_snapshot():
        with ops.log.write("backup snapshot"):
            entered.set()
            assert release.wait(5)

    holder = threading.Thread(target=hold_snapshot)
    holder.start()
    assert entered.wait(2)
    try:
        code, body = request(server, "/api/upload", raw=b"synthetic bytes", timeout=0.5)
        assert code == 503 and body == {"error": "vault is busy — try again"}
        assert not (ops.paths.root / ".api" / "incoming").exists()
    finally:
        release.set()
        holder.join(3)


@pytest.mark.parametrize("family", ["sort", "tasks", "health"])
def test_proposal_publication_prechecks_before_empty_batch_mutation(vault, family):
    ops, api = vault
    agent = AgentAPI(ops.paths.staging, StagingReader(ops, api.cards), api.cards,
                     api.tasks, api.health, lambda: None)
    store = {"sort": api.cards, "tasks": api.tasks, "health": api.health}[family]
    generation = store.generation
    damage_chain(ops)
    with pytest.raises(InvalidJournal):
        getattr(agent, family + "_propose")()
    assert store.generation == generation


@pytest.mark.parametrize("include_log", [False, True])
def test_backup_prechecks_chain_before_destination_even_without_explicit_log(vault, tmp_path, include_log):
    ops, _api = vault
    (ops.paths.documents / "synthetic.txt").write_bytes(b"synthetic bytes")
    damage_chain(ops)
    with pytest.raises(InvalidJournal):
        make_backup(ops.paths.root, tmp_path / "backup", "synthetic-passphrase",
                    ops.log if include_log else None)
    assert not (tmp_path / "backup").exists()


def test_monitor_counts_pending_intentions_without_acting_or_disclosing_paths(server):
    server, ops, _api = server
    pending = ops.log.dir / "pending"
    pending.mkdir(parents=True)
    evidence = pending / "synthetic.json"
    contents = json.dumps({"op": "move", "paths": {"src": "synthetic-private-source"},
                           "expected_hashes": {}, "ts": "2026-09-22T00:00:00Z"})
    evidence.write_text(contents, encoding="utf-8")
    code, body = request(server, "/api/monitor")
    assert code == 200 and body["pending_intentions"] == 1
    assert "synthetic-private-source" not in json.dumps(body)
    assert evidence.read_text(encoding="utf-8") == contents


def test_stopping_server_reports_in_flight_request_until_it_finishes(vault, tmp_path):
    ops, api = vault
    entered, release = threading.Event(), threading.Event()

    def slow_reply(_text):
        entered.set()
        assert release.wait(5)
        api.upload("after-request.txt", b"synthetic finished request")
        return "done"

    page = tmp_path / "index.html"
    page.write_text("synthetic only", encoding="utf-8")
    server = ApiServer(api, "synthetic-key", page, slow_reply, lambda: {}, "127.0.0.1", 0)
    server.start()
    with ThreadPoolExecutor(max_workers=1) as pool:
        response = pool.submit(request, server, "/api/chat", payload={"text": "synthetic"})
        assert entered.wait(2)
        try:
            assert server.active_requests == 1
            assert server.stop() is False, "caller must retain the runtime lease while the request runs"
            assert not server.running
        finally:
            release.set()
        # The request helper allows five seconds for socket I/O. Do not fail
        # the outer wait earlier than a still-valid client response can arrive.
        assert response.result(timeout=6) == (200, {"reply": "done"})
        # Receiving the final TCP bytes can precede the handler's finally
        # block. Keep the lease until the public activity counter is drained.
        deadline = time.monotonic() + 2
        while server.active_requests and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.stop() is True
        assert server.active_requests == 0
    assert (ops.paths.staging / "after-request.txt").read_bytes() == b"synthetic finished request"


def test_snapshot_is_before_a_competing_move_not_half_of_each(vault, tmp_path, monkeypatch):
    ops, _api = vault
    source = ops.paths.documents / "synthetic.txt"
    source.write_bytes(b"whole synthetic source")
    (ops.paths.root / ".vault.lock").write_text("synthetic runtime owner", encoding="utf-8")
    reading, release, moving, moved = (threading.Event() for _ in range(4))
    original_open = Path.open

    def open_file(path, mode="r", *args, **kwargs):
        if path == source and mode == "rb" and not reading.is_set():
            reading.set()
            assert release.wait(5)
        return original_open(path, mode, *args, **kwargs)

    def move_source():
        moving.set()
        result = ops.move(source, ops.paths.personal)
        moved.set()
        return result

    monkeypatch.setattr(Path, "open", open_file)
    with ThreadPoolExecutor(max_workers=2) as pool:
        backup = pool.submit(make_backup, ops.paths.root, tmp_path / "backup", "synthetic-passphrase", ops.log)
        assert reading.wait(2)
        move = pool.submit(move_source)
        assert moving.wait(2)
        try:
            assert not moved.wait(0.1), "a root write must wait until the whole snapshot is captured"
        finally:
            release.set()
        result = backup.result(timeout=5)
        move.result(timeout=5)
    restored = tmp_path / "restored"
    restore_backup(result.path, "synthetic-passphrase", restored)
    assert (restored / "documents" / "synthetic.txt").read_bytes() == b"whole synthetic source"
    assert not (restored / "personal" / "synthetic.txt").exists()
    assert not (restored / ".vault.lock").exists()
    assert (ops.paths.personal / "synthetic.txt").read_bytes() == b"whole synthetic source"
    assert ops.log.verify() == 2


def test_upload_move_and_task_save_share_one_intact_receipt_chain(server):
    server, ops, api = server
    source = ops.paths.documents / "move-me.txt"
    source.write_bytes(b"synthetic move bytes")
    ready = threading.Barrier(3)

    def upload():
        ready.wait(timeout=3)
        return request(server, "/api/upload", raw=b"synthetic uploaded bytes")

    def move():
        ready.wait(timeout=3)
        return ops.move(source, ops.paths.personal)

    def save():
        ready.wait(timeout=3)
        api.tasks.add(Task("synthetic-task", "abc", "synthetic.txt", "Call the clinic", "",
                           "Call the clinic", "HUMAN"))

    with ThreadPoolExecutor(max_workers=3) as pool:
        uploads, moves, saves = pool.submit(upload), pool.submit(move), pool.submit(save)
        code, body = uploads.result(timeout=5)
        moves.result(timeout=5)
        saves.result(timeout=5)
    if code == 503:
        assert body == {"error": "vault is busy — try again"}
        code, body = request(server, "/api/upload", raw=b"synthetic uploaded bytes")
    assert code == 200
    assert (ops.paths.staging / "synthetic.txt").read_bytes() == b"synthetic uploaded bytes"
    assert (ops.paths.personal / "move-me.txt").read_bytes() == b"synthetic move bytes"
    assert api.tasks.all()[0].title == "Call the clinic"
    assert sorted(row["op"] for row in ops.log.tail()) == ["import", "move", "task_add"]
    assert ops.log.verify() == 3


@pytest.mark.parametrize("family", ["sort", "tasks", "health"])
def test_model_wait_does_not_hold_root_guard_against_phone_upload(server, family):
    server, ops, api = server
    source = ops.paths.staging / "Prescription.txt"
    source.write_text("Refill medicine before 2026-10-16.", encoding="utf-8")
    entered, release = threading.Event(), threading.Event()

    class SlowBackend(Backend):
        info = BackendInfo("synthetic", "synthetic", "synthetic")

        def chat(self, system, messages, on_chunk):
            entered.set()
            assert release.wait(5)
            return "[]"

    api.agent = AgentAPI(ops.paths.staging, StagingReader(ops, api.cards), api.cards,
                         api.tasks, api.health, lambda: SlowBackend())
    with ThreadPoolExecutor(max_workers=1) as pool:
        proposal = pool.submit(request, server, f"/api/agent/{family}", payload={})
        assert entered.wait(2)
        try:
            code, _body = request(server, "/api/upload", raw=b"synthetic concurrent bytes")
            assert code == 200
        finally:
            release.set()
        assert proposal.result(timeout=3)[0] == 200
    assert (ops.paths.staging / "synthetic.txt").read_bytes() == b"synthetic concurrent bytes"


@pytest.mark.parametrize("action", ["upload", "folder", "zip", "backup"])
def test_completed_files_with_failed_receipt_report_uncertainty_and_block_next_write(vault, tmp_path, monkeypatch, action):
    ops, api = vault
    source = ops.paths.staging / "original.txt"
    source.write_bytes(b"synthetic source")
    gate = Gatekeeper(ops)
    req = gate.prepare([source], tmp_path / "outside" / "bundle", action if action in ("folder", "zip") else "folder")
    gate.approve(req)
    original_open = Path.open

    def open_file(path, mode="r", *args, **kwargs):
        if path == ops.log.file and mode == "a":
            raise OSError("synthetic-secret receipt fault")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    with pytest.raises(MayHaveApplied, match="operation may have applied") as error:
        if action == "upload":
            api.upload("landed.txt", b"synthetic uploaded bytes")
        elif action == "backup":
            make_backup(ops.paths.root, tmp_path / "backup", "synthetic-passphrase", ops.log)
        else:
            gate.execute(req)
    assert "synthetic-secret" not in str(error.value)
    if action == "upload":
        assert (ops.paths.staging / "landed.txt").read_bytes() == b"synthetic uploaded bytes"
        assert not list((ops.paths.root / ".api" / "incoming").iterdir())
    elif action == "backup":
        assert len(list((tmp_path / "backup").glob("*.vault"))) == 1
    elif action == "folder":
        assert (tmp_path / "outside" / "bundle" / "original.txt").read_bytes() == b"synthetic source"
    else:
        assert (tmp_path / "outside" / "bundle.zip").exists()
    with pytest.raises(VaultWriteBlocked):
        api.upload("must-not-land.txt", b"refused")
    assert not (ops.paths.staging / "must-not-land.txt").exists()
