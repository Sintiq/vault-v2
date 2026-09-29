"""Phone proposals share desktop parsing; transport failures preserve authority."""

from dataclasses import asdict
import json
from pathlib import Path
import urllib.error
import urllib.request

import pytest

from vault_v2 import health as healthing, sorting, tasks as tasking
from vault_v2.agent import Backend, BackendInfo, LocalModelUnavailable, OllamaBackend
from vault_v2.agent_api import AgentAPI
from vault_v2.api import ApiServer, VaultAPI
from vault_v2.cards import CardStore, load_shelves
from vault_v2.health import HealthStore
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.reader import StagingReader
from vault_v2.tasks import TaskStore


class ReplyBackend(Backend):
    info = BackendInfo("synthetic", "synthetic", "synthetic")
    reply = "[]"
    error = None

    def chat(self, system, messages, on_chunk):
        if self.error is not None:
            raise self.error
        return self.reply


@pytest.fixture()
def model_server(tmp_path: Path):
    load_shelves({})
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    (ops.paths.staging / "Prescription.txt").write_text(
        "Refill prescription before 2026-10-16.\n", encoding="utf-8")
    cards = CardStore(ops.paths.root / ".cards", ops.log)
    tasks = TaskStore(ops.paths.root / ".tasks", ops.log)
    health = HealthStore(ops.paths.root / ".health", ops.log)
    backend = ReplyBackend()
    api = AgentAPI(ops.paths.staging, StagingReader(ops, cards, purpose="agent"),
                   cards, tasks, health, lambda: backend)
    page = tmp_path / "index.html"
    page.write_text("synthetic test only", encoding="utf-8")
    server = ApiServer(VaultAPI(ops, cards, tasks, health, api), "synthetic-key", page,
                       lambda _: "unused", lambda: {}, "127.0.0.1", 0)
    server.start()
    try:
        yield server, api, backend, ops
    finally:
        server.stop()


def post(server, path, payload):
    request = urllib.request.Request(server.url.rstrip("/") + path,
                                     data=json.dumps(payload).encode("utf-8"),
                                     headers={"Authorization": "Bearer synthetic-key",
                                              "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def accept(server, family, handle):
    if family == "sort":
        return post(server, "/api/agent/sort/confirm", {"items": [{"id": handle}]})
    return post(server, f"/api/agent/{family}/add", {"ids": [handle]})


def valid_reply(family):
    quote = "Refill prescription before 2026-10-16."
    rows = {
        "sort": {"id": "doc-001", "shelf": "INSURANCE", "topics": ["refill"],
                 "issuer": "UNCONFIRMED", "recipients": ["DOCTOR"], "reason": "synthetic choice"},
        "tasks": {"doc": "doc-001", "title": "Renew prescription", "due": "2026-10-16", "quote": quote},
        "health": {"doc": "doc-001", "label": "Prescription refill", "date": "2026-10-16",
                   "kind": "MEDICATION", "quote": quote},
    }
    return json.dumps([rows[family]])


def desktop_rows(api, backend, family):
    if family == "sort":
        proposals = sorting.propose(sorting.collect_inputs(api.staging, api.cards, reader=api.reader), backend)
        return [{key: value for key, value in asdict(p.card).items() if key != "confirmed"}
                for p in proposals]
    proposer = tasking.propose if family == "tasks" else healthing.propose
    rows, _notes = proposer(api.reader, backend)
    return [asdict(row) for row in rows]


def semantic_rows(rows, family):
    if family == "sort":
        keys = ("sha256", "name", "kind", "shelf", "topics", "issuer", "year", "recipients", "origin", "reason")
        return [{key: row[key] for key in keys} for row in rows]
    natural = "task_id" if family == "tasks" else "entry_id"
    return [{**{k: v for k, v in row.items() if k not in ("id", natural)}, "id": row[natural]}
            for row in rows]


@pytest.mark.parametrize("family", ["sort", "tasks", "health"])
@pytest.mark.parametrize("wrap", ["```json\n{}\n```", "Here are the proposals:\n{}\nEnd."])
def test_wrapped_model_reply_matches_desktop_proposals(model_server, family, wrap):
    server, api, backend, _ops = model_server
    backend.reply = wrap.format(valid_reply(family))
    expected = desktop_rows(api, backend, family)
    code, body = post(server, f"/api/agent/{family}", {})
    assert code == 200
    rows = body["proposals"]
    assert rows and rows[0]["origin"] == "AGENT"
    field = {"sort": "shelf", "tasks": "title", "health": "label"}[family]
    assert rows[0][field] == {"sort": "INSURANCE", "tasks": "Renew prescription",
                              "health": "Prescription refill"}[family]
    # Normalize tuples to their JSON representation, not the semantic values.
    assert semantic_rows(rows, family) == json.loads(json.dumps(expected))
    code, result = accept(server, family, rows[0]["id"])
    assert code == 200 and result["confirmed" if family == "sort" else "added"] == 1


def stores_snapshot(api):
    return [(store.generation, store.all()) for store in (api.cards, api.tasks, api.health)]


@pytest.mark.parametrize("family", ["sort", "tasks", "health"])
@pytest.mark.parametrize("garbage", ["not usable JSON", "null", "```json\n[broken]\n```"])
def test_garbage_reply_publishes_flagged_desktop_baseline_and_replaces_batch(model_server, family, garbage):
    server, api, backend, ops = model_server
    backend.reply = valid_reply(family)
    code, old = post(server, f"/api/agent/{family}", {})
    assert code == 200 and old["proposals"][0]["origin"] == "AGENT"
    backend.reply = garbage
    expected = desktop_rows(api, backend, family)
    code, body = post(server, f"/api/agent/{family}", {})
    assert code == 200 and body["proposals"]
    rows = body["proposals"]
    assert all(row["origin"] == "BASELINE" for row in rows)
    assert semantic_rows(rows, family) == json.loads(json.dumps(expected))
    flags = [flag for row in rows for flag in row["flags"]] if family == "sort" else body["notes"]
    assert any(flag.startswith("agent reply unusable") for flag in flags)
    before = stores_snapshot(api), ops.log.tail(1000)
    assert accept(server, family, old["proposals"][0]["id"]) == (
        409, {"error": "proposals changed — refresh the list"})
    assert (stores_snapshot(api), ops.log.tail(1000)) == before
    code, result = accept(server, family, rows[0]["id"])
    assert code == 200 and result["confirmed" if family == "sort" else "added"] == 1


@pytest.mark.parametrize("family", ["sort", "tasks", "health"])
@pytest.mark.parametrize("transport", ["typed_unavailable", "ollama_http_failure"])
def test_transport_503_keeps_old_batch_confirmable(model_server, family, transport, monkeypatch):
    server, api, backend, ops = model_server
    backend.reply = valid_reply(family)
    code, shown = post(server, f"/api/agent/{family}", {})
    assert code == 200 and shown["proposals"]
    before = stores_snapshot(api)
    mutation_receipts = [r for r in ops.log.tail(1000) if r["op"] != "agent_read"]
    if transport == "typed_unavailable":
        backend.error = LocalModelUnavailable("SYNTHETIC detail must not be transmitted")
    else:
        api.get_backend = lambda: OllamaBackend("synthetic:local")
        real_http = urllib.request.HTTPHandler.http_open

        def fail_model_http(handler, request):
            if request.host == "127.0.0.1:11434":
                assert request.full_url == "http://127.0.0.1:11434/api/chat"
                raise urllib.error.URLError("SYNTHETIC upstream connection failure")
            assert request.host == f"127.0.0.1:{server.port}"
            return real_http(handler, request)

        monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", fail_model_http)
    assert post(server, f"/api/agent/{family}", {}) == (
        503, {"error": "Local-only: selected model unavailable or blocked; "
                       "cloud fallback is disabled. Check Ollama and local settings."})
    assert stores_snapshot(api) == before
    assert [r for r in ops.log.tail(1000) if r["op"] != "agent_read"] == mutation_receipts
    # Confirm is independent of model availability and still accepts the shown handle.
    code, result = accept(server, family, shown["proposals"][0]["id"])
    assert code == 200 and result["confirmed" if family == "sort" else "added"] == 1
