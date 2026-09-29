"""Acceptance conflicts across the real loopback HTTP boundary; synthetic stores."""

from concurrent.futures import ThreadPoolExecutor
import json
import threading
from pathlib import Path
import urllib.error
import urllib.request

import pytest

from vault_v2.agent_api import AgentAPI
from vault_v2.api import ApiServer, VaultAPI
from vault_v2.cards import CardStore, load_shelves
from vault_v2.health import HealthStore
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.reader import StagingReader
from vault_v2.tasks import TaskStore


@pytest.fixture()
def proposal_server(tmp_path: Path):
    load_shelves({})
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    (ops.paths.staging / "Prescription.txt").write_text(
        "Refill prescription before 2026-10-16.\n", encoding="utf-8")
    cards = CardStore(ops.paths.root / ".cards", ops.log)
    tasks = TaskStore(ops.paths.root / ".tasks", ops.log)
    health = HealthStore(ops.paths.root / ".health", ops.log)
    agent = AgentAPI(ops.paths.staging, StagingReader(ops, cards, purpose="agent"),
                     cards, tasks, health, lambda: None)
    page = tmp_path / "page.html"
    page.write_text("synthetic", encoding="utf-8")
    server = ApiServer(VaultAPI(ops, cards, tasks, health, agent), "synthetic-key",
                       page, lambda _: "unused", lambda: {}, "127.0.0.1", 0)
    server.start()
    try:
        yield server, ops, cards, tasks, health
    finally:
        server.stop()


def post(server, path, payload):
    request = urllib.request.Request(
        server.url.rstrip("/") + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer synthetic-key", "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def acceptance(family, handles):
    if family == "sort":
        return "/api/agent/sort/confirm", {"items": [{"id": handle} for handle in handles]}
    return f"/api/agent/{family}/add", {"ids": handles}


def state(env):
    _server, ops, cards, tasks, health = env
    return cards.all(), tasks.all(), health.all(), ops.log.tail(1000)


@pytest.mark.parametrize("family", ["sort", "tasks", "health"])
def test_old_shown_batch_gets_exact_http_409_without_acceptance_writes(proposal_server, family):
    server, *_ = proposal_server
    code, shown = post(server, f"/api/agent/{family}", {})
    assert code == 200 and shown["proposals"]
    code, replacement = post(server, f"/api/agent/{family}", {})
    assert code == 200 and replacement["proposals"]
    before = state(proposal_server)
    path, body = acceptance(family, [shown["proposals"][0]["id"]])

    assert post(server, path, body) == (409, {"error": "proposals changed — refresh the list"})
    assert state(proposal_server) == before


@pytest.mark.parametrize("family", ["sort", "tasks", "health"])
@pytest.mark.parametrize("bad", [None, "not-a-list", {}, [None], [False], [17], [[]]])
def test_invalid_selection_shapes_are_409_not_coerced_or_written(proposal_server, family, bad):
    server, *_ = proposal_server
    assert post(server, f"/api/agent/{family}", {})[0] == 200
    path, _ = acceptance(family, [])
    before = state(proposal_server)
    body = {"items" if family == "sort" else "ids": bad}
    assert post(server, path, body) == (409, {"error": "proposals changed — refresh the list"})
    assert state(proposal_server) == before


@pytest.mark.parametrize("family", ["sort", "tasks", "health"])
def test_duplicate_or_mixed_selection_is_rejected_before_first_write(proposal_server, family):
    server, *_ = proposal_server
    _, shown = post(server, f"/api/agent/{family}", {})
    handle = shown["proposals"][0]["id"]
    before = state(proposal_server)
    for handles in ([handle, handle], [handle, "expired-handle"]):
        path, body = acceptance(family, handles)
        assert post(server, path, body) == (409, {"error": "proposals changed — refresh the list"})
        assert state(proposal_server) == before
    # A malformed selection did not burn the otherwise valid handle.
    path, body = acceptance(family, [handle])
    assert post(server, path, body)[0] == 200
    before_replay = state(proposal_server)
    assert post(server, path, body) == (409, {"error": "proposals changed — refresh the list"})
    assert state(proposal_server) == before_replay


@pytest.mark.parametrize("family", ["sort", "tasks", "health"])
def test_simultaneous_http_confirmations_have_one_winner(proposal_server, family):
    server, ops, *_ = proposal_server
    _, shown = post(server, f"/api/agent/{family}", {})
    path, body = acceptance(family, [shown["proposals"][0]["id"]])
    before = len(ops.log.tail(1000))
    ready = threading.Barrier(2)

    def confirm():
        ready.wait(timeout=5)
        return post(server, path, body)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: confirm(), range(2)))
    assert sorted(code for code, _ in responses) in ([200, 409], [200, 503])
    for code, result in responses:
        if code == 503:
            assert result == {"error": "vault is busy — try again"}
        elif code == 409:
            assert result == {"error": "proposals changed — refresh the list"}
    # A busy rejection does not authorize a second write on a later retry.
    assert post(server, path, body) == (409, {"error": "proposals changed — refresh the list"})
    assert len(ops.log.tail(1000)) == before + 1


@pytest.mark.parametrize("body", [None, [], 17, False, "not-an-object"])
def test_non_object_confirmation_body_is_exact_409(proposal_server, body):
    server, *_ = proposal_server
    before = state(proposal_server)
    assert post(server, "/api/agent/tasks/add", body) == (
        409, {"error": "proposals changed — refresh the list"})
    assert state(proposal_server) == before


@pytest.mark.parametrize("action", ["sort", "tasks", "health", "ask"])
@pytest.mark.parametrize("body", [None, []])
def test_non_object_proposal_or_search_body_is_400_not_stale_approval(proposal_server, action, body):
    server, *_ = proposal_server
    before = state(proposal_server)
    assert post(server, f"/api/agent/{action}", body) == (
        400, {"error": "request body must be a JSON object"})
    assert state(proposal_server) == before


def test_desktop_sort_publication_makes_phone_confirmation_conflict(proposal_server):
    server, ops, cards, *_ = proposal_server
    _, shown = post(server, "/api/agent/sort", {})
    path = ops.paths.staging / "Prescription.txt"
    cards.propose(cards.for_path(path), path)  # Same store boundary as SortDialog.
    before = state(proposal_server)
    endpoint, body = acceptance("sort", [shown["proposals"][0]["id"]])
    assert post(server, endpoint, body) == (409, {"error": "proposals changed — refresh the list"})
    assert state(proposal_server) == before


def test_invalid_later_sort_edit_refuses_entire_http_selection(proposal_server):
    server, ops, *_ = proposal_server
    (ops.paths.staging / "Second.txt").write_text("Synthetic invoice", encoding="utf-8")
    _, shown = post(server, "/api/agent/sort", {})
    assert len(shown["proposals"]) == 2
    before = state(proposal_server)
    body = {"items": [{"id": shown["proposals"][0]["id"], "shelf": "TAXES"},
                      {"id": shown["proposals"][1]["id"], "shelf": "UNKNOWN-SHELF"}]}
    assert post(server, "/api/agent/sort/confirm", body) == (
        409, {"error": "proposals changed — refresh the list"})
    assert state(proposal_server) == before


@pytest.mark.parametrize("family", ["sort", "tasks", "health"])
def test_http_partial_write_result_names_all_outcomes_and_cannot_retry(proposal_server, family, monkeypatch):
    server, ops, cards, tasks, health = proposal_server
    for number in (2, 3):
        (ops.paths.staging / f"Prescription{number}.txt").write_text(
            f"Refill prescription {number} before 2026-10-16.\n", encoding="utf-8")
    _, shown = post(server, f"/api/agent/{family}", {})
    assert len(shown["proposals"]) == 3
    handles = [row["id"] for row in shown["proposals"]]
    store = {"sort": cards, "tasks": tasks, "health": health}[family]
    original_replace = Path.replace
    attempts = []

    def fail_second(path, target):
        if Path(target) == store.file:
            attempts.append(path)
            if len(attempts) == 2:
                raise OSError("synthetic failure, not a private path")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_second)
    path, body = acceptance(family, handles)
    code, result = post(server, path, body)
    assert code == 200, "a structured partial outcome is not a stale-handle 409"
    assert result["confirmed" if family == "sort" else "added"] == 1
    assert result["outcome"] == {
        "completed": handles[:1], "failed_unknown": handles[1:2], "not_attempted": handles[2:]}
    assert result["errors"] and "synthetic failure" not in json.dumps(result)
    assert len(attempts) == 2
    before = state(proposal_server)
    for handle in handles:
        path, body = acceptance(family, [handle])
        assert post(server, path, body) == (503, {
            "error": "an operation may have applied; writes blocked until restart"})
    assert state(proposal_server) == before and len(attempts) == 2
