"""Archive Ask through the real HTTP boundary, with synthetic roots only."""
import json
import urllib.error
import urllib.request

import pytest

from vault_v2.agent import Backend, BackendInfo, LocalModelUnavailable
from vault_v2.agent_api import AgentAPI
from vault_v2.agent_scope import AgentTextScope
from vault_v2.api import ApiServer, VaultAPI
from vault_v2.cards import CardStore
from vault_v2.health import HealthStore
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.reader import StagingReader
from vault_v2.tasks import TaskStore


class LocalRecorder(Backend):
    info = BackendInfo("ollama", "synthetic", "local test adapter")

    def __init__(self):
        self.prompts = []

    def chat(self, system, messages, on_chunk):
        self.prompts.append((system, messages))
        records = json.loads(messages[0]["content"].split("Documents:\n", 1)[1])
        return json.dumps({"documents": [record["id"] for record in records],
                           "recipient": "PERSONAL", "reason": "synthetic selection"})


@pytest.fixture()
def archive_server(tmp_path):
    ops = VaultOps(VaultPaths(tmp_path / "synthetic"))
    cards = CardStore(ops.paths.root / ".cards", ops.log)
    tasks = TaskStore(ops.paths.root / ".tasks", ops.log)
    health = HealthStore(ops.paths.root / ".health", ops.log)
    reader = StagingReader(ops, cards)
    agent = AgentAPI(ops.paths.staging, reader, cards, tasks, health, lambda: None)
    api = VaultAPI(ops, cards, tasks, health, agent)
    page = tmp_path / "index.html"
    page.write_text("Synthetic only", encoding="utf-8")
    server = ApiServer(api, "synthetic-key", page, lambda _: "unused", lambda: {}, "127.0.0.1", 0)
    server.start()
    try:
        yield server, ops, agent
    finally:
        server.stop()


def post(server, path, body):
    request = urllib.request.Request(
        server.url.rstrip("/") + path, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": "Bearer synthetic-key", "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=3) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_phone_ask_returns_unambiguous_matches_from_all_panes(archive_server):
    server, ops, _ = archive_server
    for pane in ("staging", "documents", "personal"):
        folder = ops.paths.pane(pane) / "nested"
        folder.mkdir()
        (folder / "immigration.txt").write_text("synthetic only", encoding="utf-8")

    status, result = post(server, "/api/agent/ask", {"phrase": "immigration"})

    assert status == 200
    assert len(result["matches"]) == 3
    assert {(row["pane"], row["rel"]) for row in result["matches"]} == {
        ("staging", "nested/immigration.txt"),
        ("documents", "nested/immigration.txt"),
        ("personal", "nested/immigration.txt"),
    }
    assert all(row["name"] == "immigration.txt" and row["shelf"] is None
               and not row["added_by_agent"] and not row["omitted_by_agent"]
               for row in result["matches"])
    assert result["recipient"] is None
    assert result["export"] == "at the desk only"
    assert "cards everywhere" in " ".join(result["notes"])


def test_phone_ask_reports_an_empty_archive(archive_server):
    server, _, _ = archive_server

    status, result = post(server, "/api/agent/ask", {"phrase": "immigration"})

    assert status == 200
    assert result == {"phrase": "immigration", "matches": [], "notes": ["Archive is empty"]}


def test_phone_keeps_model_reason_separate_from_structured_sources(archive_server):
    class WrongLocation(Backend):
        info = BackendInfo("ollama", "synthetic", "local test adapter")

        def chat(self, system, messages, on_chunk):
            return json.dumps({"documents": [], "recipient": "PERSONAL",
                               "reason": "Document is in the personal pane.\nSecond line."})

    server, ops, agent = archive_server
    (ops.paths.staging / "migraine.txt").write_text("Synthetic only", encoding="utf-8")
    agent.get_backend = WrongLocation

    status, result = post(server, "/api/agent/ask", {"phrase": "migraine"})

    assert status == 200
    assert [row["pane"] for row in result["matches"]] == ["staging"]
    assert "Proposed sources: Staging (1)." in result["notes"]
    assert not any("personal pane" in note for note in result["notes"])
    assert result["agent_reason"] == "Document is in the personal pane. Second line."


def test_phone_ask_uses_granted_cache_only_and_refreshes_after_revoke(archive_server):
    from vault_v2.text_cache import DocumentTextCache

    server, ops, _ = archive_server
    folder = ops.paths.personal / "allowed"
    folder.mkdir()
    allowed, denied = folder / "one.txt", ops.paths.documents / "two.txt"
    for path in (allowed, denied):
        path.write_text("SYNTHETICVISATOKEN", encoding="utf-8")
    scope = AgentTextScope(ops.paths, ops.log)
    scope.set_folder(folder, True)
    assert scope.warm(folder) == ()

    status, result = post(server, "/api/agent/ask", {"phrase": "SYNTHETICVISATOKEN"})

    assert status == 200
    assert [(row["pane"], row["rel"]) for row in result["matches"]] == [("personal", "allowed/one.txt")]
    reads = [row for row in ops.log.tail() if row["op"] == "agent_read"]
    assert len(reads) == 1 and reads[0]["src"] == str(allowed)
    scope.set_folder(folder, False)

    status, result = post(server, "/api/agent/ask", {"phrase": "SYNTHETICVISATOKEN"})

    assert status == 200 and result["matches"] == []
    assert [row for row in ops.log.tail() if row["op"] == "agent_read"] == reads
    assert DocumentTextCache(ops.paths, ops.log).read_cached_snapshot(allowed).text == "SYNTHETICVISATOKEN"


def test_phone_ask_refuses_an_oversized_question_before_model_dispatch(archive_server):
    server, ops, agent = archive_server
    (ops.paths.documents / "one.txt").write_text("synthetic", encoding="utf-8")
    backend = LocalRecorder()
    agent.get_backend = lambda: backend

    status, result = post(server, "/api/agent/ask", {"phrase": "synthetic question " * 5000})

    assert status == 413
    assert result["documents"] == ["doc-001 — documents/one.txt"]
    assert backend.prompts == []


@pytest.mark.parametrize("endpoint,status", [
    ("/api/agent/scope/grant", 400),
    ("/api/agent/scope/revoke", 400),
    ("/api/agent/ask/copy", 400),
    ("/api/agent/ask/copy-to-staging", 400),
    ("/api/scope/grant", 404),
])
def test_phone_has_no_archive_scope_or_ask_copy_endpoint(archive_server, endpoint, status):
    server, ops, _ = archive_server
    folder = ops.paths.personal / "allowed"
    folder.mkdir()
    (folder / "one.txt").write_text("synthetic", encoding="utf-8")
    AgentTextScope(ops.paths, ops.log).set_folder(folder, True)
    settings, receipts = ops.paths.settings_file.read_bytes(), ops.log.tail()

    response_status, result = post(server, endpoint, {
        "pane": "personal", "rel": "allowed/one.txt", "folder": "personal/allowed", "enabled": True})

    assert response_status == status and "error" in result
    assert ops.paths.settings_file.read_bytes() == settings
    assert ops.log.tail() == receipts
    assert list(ops.paths.staging.iterdir()) == []


def test_phone_ask_keeps_the_local_getter_and_returns_model_notes(archive_server):
    server, ops, agent = archive_server
    folder = ops.paths.personal / "allowed"
    folder.mkdir()
    (folder / "one.txt").write_text("PERMITTED_EXCERPT", encoding="utf-8")
    (ops.paths.documents / "two.txt").write_text("FORBIDDEN_EXCERPT", encoding="utf-8")
    ops.paths.settings_file.write_text(json.dumps({
        "ollama_model": "synthetic", "agent_door_claude_model": "claude-opus-5-5"}), encoding="utf-8")
    scope = AgentTextScope(ops.paths, ops.log)
    scope.set_folder(folder, True)
    assert scope.warm(folder) == ()
    settings = ops.paths.settings_file.read_bytes()
    backend = LocalRecorder()
    agent.get_backend = lambda: backend

    status, result = post(server, "/api/agent/ask", {"phrase": "one"})

    assert status == 200 and result["recipient"] == "PERSONAL"
    assert len(result["matches"]) == 2
    assert "model saw 2 of 2 candidates" in result["notes"]
    assert result["agent_reason"] == "synthetic selection"
    assert "synthetic selection" not in result["notes"]
    assert result["export"] == "at the desk only"
    sent = json.dumps(backend.prompts)
    assert "PERMITTED_EXCERPT" in sent and "FORBIDDEN_EXCERPT" not in sent
    assert ops.paths.settings_file.read_bytes() == settings
    assert not any(row["op"] == "agent_door_request" for row in ops.log.tail())


def test_phone_ask_propagates_local_getter_refusal_before_archive_reads(archive_server):
    server, ops, agent = archive_server
    folder = ops.paths.personal / "allowed"
    folder.mkdir()
    (folder / "one.txt").write_text("PERMITTED_EXCERPT", encoding="utf-8")
    scope = AgentTextScope(ops.paths, ops.log)
    scope.set_folder(folder, True)
    assert scope.warm(folder) == ()
    settings, receipts = ops.paths.settings_file.read_bytes(), ops.log.tail()

    def unavailable_local():
        error = LocalModelUnavailable()
        error.public_message = "local model unavailable — the agent door covers chat only"
        raise error

    agent.get_backend = unavailable_local
    status, result = post(server, "/api/agent/ask", {"phrase": "one"})

    assert status == 503 and "chat only" in result["error"]
    assert "matches" not in result
    assert ops.paths.settings_file.read_bytes() == settings
    assert ops.log.tail() == receipts
