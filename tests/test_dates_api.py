"""Owner edits and grounded dates through the real synthetic HTTP boundary."""
from datetime import date
import json
from pathlib import Path
import threading
import urllib.error
import urllib.request

import pytest

from vault_v2.agent_api import AgentAPI
from vault_v2 import cards as card_catalog
from vault_v2.api import ApiServer, VaultAPI, TEXT_VIEW_MAX_BYTES
from vault_v2.cards import CardStore, load_shelves
from vault_v2.health import Entry, HealthStore
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.reader import StagingReader, READ_LIMIT_CHARS
from vault_v2.tasks import Task, TaskStore
from vault_v2.reminders import due_reminders, remind
from vault_v2.shelves import add_shelf


@pytest.fixture()
def env(tmp_path):
    load_shelves({"shelves": ["IMMIGRATION"]})
    ops = VaultOps(VaultPaths(tmp_path / "synthetic"))
    (ops.paths.staging / "note.txt").write_text("Refill prescription before October 15, 2026.", encoding="utf-8")
    cards = CardStore(ops.paths.root / ".cards", ops.log)
    tasks = TaskStore(ops.paths.root / ".tasks", ops.log)
    health = HealthStore(ops.paths.root / ".health", ops.log)
    reader = StagingReader(ops, cards)
    agent = AgentAPI(ops.paths.staging, reader, cards, tasks, health, lambda: None)
    api = VaultAPI(ops, cards, tasks, health, agent)
    tasks.add(Task("t1", "a"*64, "saved.txt", "Pay", "2026-10-15", "Pay by October 15, 2026", "AGENT"))
    health.add(Entry("h1", "a"*64, "saved.txt", "2026-10-15", "VISIT", "Visit", "Visit October 15, 2026", "AGENT"))
    page = tmp_path / "index.html"
    page.write_text("Synthetic only", encoding="utf-8")
    server = ApiServer(api, "synthetic-key", page, lambda _: "unused", lambda: {}, "127.0.0.1", 0)
    server.start()
    try:
        yield server, ops, api, reader
    finally:
        server.stop()
        load_shelves({})


def request(server, path, body=None, *, key="synthetic-key", method="POST"):
    data = json.dumps(body).encode() if method == "POST" and key == "synthetic-key" else None
    req = urllib.request.Request(server.url.rstrip("/")+path, data=data, method=method,
        headers={"Authorization": "Bearer "+key, "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=3) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


EDITS = [
    ("sort", "/api/card/shelf", {"pane":"staging", "rel":"note.txt", "shelf":"IMMIGRATION"}, "card_confirm"),
    ("tasks", "/api/task/due", {"id":"t1", "due":"2026-11-01"}, "task_due_set"),
    ("health", "/api/health/date", {"id":"h1", "date":"2026-11-01"}, "health_date_set"),
]


@pytest.mark.parametrize("family,endpoint,body,receipt", EDITS)
def test_owner_edit_is_receipted_and_invalidates_shown_batch(env, family, endpoint, body, receipt):
    server, ops, api, _ = env
    code, shown = request(server, "/api/agent/"+family, {})
    assert code == 200 and shown["proposals"]
    store = {"sort":api.cards,"tasks":api.tasks,"health":api.health}[family]
    generation = store.generation
    code, result = request(server, endpoint, body)
    assert code == 200
    assert store.generation == generation + 1
    assert ops.log.tail(1)[0]["op"] == receipt
    assert ops.log.verify() > 0
    if family == "sort":
        assert result["card"]["origin"] == "HUMAN"
        assert result["card"]["confirmed"] is True
        assert result["card"]["topics"] == shown["proposals"][0]["topics"]
        accept = "/api/agent/sort/confirm"
        payload = {"items":[{"id":shown["proposals"][0]["id"]}]}
    else:
        assert result["due_source"] == "owner"
        accept = "/api/agent/"+family+"/add"
        payload = {"ids":[shown["proposals"][0]["id"]]}
    assert request(server, accept, payload)[0] == 409


@pytest.mark.parametrize("read_only", [False, True])
def test_shelves_read_tracks_live_catalog_without_writes(env, read_only):
    server, ops, api, _ = env
    load_shelves({})
    before = ops.log.verify()
    code, result = request(server, "/api/shelves", method="GET")
    assert code == 200
    assert ops.log.verify() == before
    assert result == {"shelves": list(card_catalog.DEFAULT_SHELVES),
                      "standard": list(card_catalog.DEFAULT_SHELVES)}
    add_shelf(ops.paths, "IMMIGRATION", ops.log)
    add_shelf(ops.paths, "WORK", ops.log)
    receipts = ops.log.verify()
    settings = ops.paths.settings_file.read_bytes()
    generations = (api.cards.generation, api.tasks.generation, api.health.generation)
    ops.log.read_only = read_only

    code, result = request(server, "/api/shelves", method="GET")

    assert code == 200
    assert result == {"shelves": [*card_catalog.DEFAULT_SHELVES, "IMMIGRATION", "WORK"],
                      "standard": list(card_catalog.DEFAULT_SHELVES)}
    assert result["shelves"] == list(card_catalog.SHELVES)
    assert ops.log.verify() == receipts
    assert ops.paths.settings_file.read_bytes() == settings
    assert (api.cards.generation, api.tasks.generation, api.health.generation) == generations


@pytest.mark.parametrize("key", ["", "wrong-key"])
def test_shelves_read_requires_existing_api_authentication(env, key):
    server, ops, _, _ = env
    receipts = ops.log.verify()
    code, result = request(server, "/api/shelves", key=key, method="GET")
    assert code == 401 and "shelves" not in result and "standard" not in result
    assert ops.log.verify() == receipts


@pytest.mark.parametrize("value", ["2026-11-01", None])
@pytest.mark.parametrize("family,endpoint,key,identifier,receipt", [
    ("tasks", "/api/task/due", "due", "t1", "task_due_set"),
    ("health", "/api/health/date", "date", "h1", "health_date_set"),
])
def test_repeated_owner_date_save_is_receipted_and_invalidates_shown_batch(
        env, family, endpoint, key, identifier, receipt, value):
    server, ops, api, _ = env
    body = {"id": identifier, key: value}
    assert request(server, endpoint, body)[0] == 200
    code, shown = request(server, "/api/agent/" + family, {})
    assert code == 200 and shown["proposals"]
    store = api.tasks if family == "tasks" else api.health
    generation = store.generation
    receipts = ops.log.verify()

    code, result = request(server, endpoint, body)

    assert code == 200 and result[key] == value and result["due_source"] == "owner"
    assert ops.log.verify() == receipts + 1
    assert ops.log.tail(1)[0]["op"] == receipt
    assert store.generation == generation + 1
    payload = {"ids": [shown["proposals"][0]["id"]]}
    assert request(server, "/api/agent/" + family + "/add", payload)[0] == 409


@pytest.mark.parametrize("family,endpoint,body,receipt", EDITS)
def test_owner_edit_authentication_and_busy_guard_precede_writes(env, family, endpoint, body, receipt):
    server, ops, api, _ = env
    before = ops.log.tail(1000)
    assert request(server, endpoint, body, key="wrong")[0] == 401
    held, release = threading.Event(), threading.Event()
    def hold():
        with ops.log.write("synthetic held guard"):
            held.set()
            release.wait(5)
    worker = threading.Thread(target=hold)
    worker.start()
    assert held.wait(2)
    try:
        assert request(server, endpoint, body)[0] == 503
    finally:
        release.set()
        worker.join(3)
    assert ops.log.tail(1000) == before


@pytest.mark.parametrize("family,endpoint,body,receipt", EDITS)
def test_owner_edit_refuses_damaged_journal_before_effect(env, family, endpoint, body, receipt):
    server, ops, api, _ = env
    before = (api.cards.all(), api.tasks.all(), api.health.all())
    ops.log.file.write_text("damaged synthetic journal", encoding="utf-8")
    assert request(server, endpoint, body)[0] in (400, 503)
    assert (api.cards.all(), api.tasks.all(), api.health.all()) == before


@pytest.mark.parametrize("endpoint,key,tid", [("/api/task/due","due","t1"),("/api/health/date","date","h1")])
def test_explicit_null_is_an_owner_choice_and_reloads(env, endpoint, key, tid):
    server, ops, api, _ = env
    assert request(server, endpoint, {"id":tid,key:None})[0] == 200
    rows = (TaskStore(api.tasks.dir, ops.log).all() if key == "due" else HealthStore(api.health.dir, ops.log).all())
    row = next(row for row in rows if row.id == tid)
    assert row.due_source == "owner" and getattr(row, key) is None
    assert "date_not_in_quote" not in row.flags


@pytest.mark.parametrize("value", ["2026-02-30", "20261015", "next week", "2026-10", 2026, True, {}, []])
@pytest.mark.parametrize("endpoint,key,tid", [("/api/task/due","due","t1"),("/api/health/date","date","h1")])
def test_invalid_manual_dates_do_not_write(env, endpoint, key, tid, value):
    server, ops, _, _ = env
    before = ops.log.tail(1000)
    assert request(server, endpoint, {"id":tid,key:value})[0] == 400
    assert ops.log.tail(1000) == before


@pytest.mark.parametrize("payload", [None, [], "wrong", {"id":[]}, {"id":"missing", "due":None}, {"id":"t1"}])
def test_malformed_edit_does_not_kill_handler(env, payload):
    server, ops, _, _ = env
    before = ops.log.tail(1000)
    assert request(server, "/api/task/due", payload)[0] == 400
    assert ops.log.tail(1000) == before


def test_api_exposes_grounding_fields_and_code_arithmetic(env, monkeypatch):
    server, _, api, _ = env
    import vault_v2.api as module
    original = module.days_left
    monkeypatch.setattr(module, "days_left", lambda value: original(value, date(2026,9,22)))
    code, result = request(server, "/api/tasks", method="GET")
    row = result["tasks"][0]
    assert code == 200 and row["due_source"] == "quote"
    assert row["days_left"] == 23 and row["overdue"] is False
    assert "flags" in row and "quote" in row
    code, result = request(server, "/api/health", method="GET")
    row = result["years"][0]["entries"][0]
    assert code == 200 and row["due_source"] == "quote" and "flags" in row


def test_owner_text_view_is_full_and_model_still_bounded(env):
    server, ops, api, reader = env
    text = "a"*20000
    (ops.paths.staging / "long.md").write_text(text, encoding="utf-8")
    before = len(ops.log.tail(1000))
    code, result = request(server, "/api/file?pane=staging&rel=long.md", method="GET")
    assert code == 200 and result["text"] == text and result["truncated"] is False
    assert len(ops.log.tail(1000)) == before
    assert len(reader.read_text("long.md")) == READ_LIMIT_CHARS == 6000
    assert ops.log.tail(1)[0]["op"] == "agent_read"


@pytest.mark.parametrize("size,truncated", [(2*1024*1024,False),(3*1024*1024,True)])
def test_owner_text_byte_ceiling(env, size, truncated):
    _, ops, api, _ = env
    (ops.paths.documents / "large.md").write_bytes(b"a"*size)
    result = api.read_file("documents", "large.md")
    assert len(result["text"]) == TEXT_VIEW_MAX_BYTES
    assert result["truncated"] is truncated


def test_reminder_uses_quote_date_and_never_foreign_date(env, monkeypatch):
    _, ops, api, _ = env
    api.tasks.add(Task("bad", "b"*64, "foreign.txt", "Foreign", "2026-09-22", "Pay within 30 days", "AGENT"))
    due = due_reminders(api.tasks.all(), date(2026,10,14))
    assert [r.task_id for r in due] == ["t1"]
    assert due[0].days_left == 1 and due[0].when == "due tomorrow"
    door = ops.paths.root / ".door"
    door.mkdir()
    (door/"config.json").write_text(json.dumps({"address":"http://100.64.0.1:8779","key":"synthetic"}), encoding="utf-8")
    sent = []
    def knock(address,key,items):
        sent.extend(items)
        return {"shown":True}
    monkeypatch.setattr("vault_v2.reminders.knock",knock)
    assert remind(ops.paths.root,api.tasks,ops.log,date(2026,10,14)) == "reminders: 1 sent to the phone"
    assert [r.task_id for r in sent] == ["t1"]
