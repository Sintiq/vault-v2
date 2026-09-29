"""Proposal authorization at the public AgentAPI and real store boundaries."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vault_v2.agent import Backend, BackendInfo
from vault_v2.agent_api import AgentAPI
from vault_v2.cards import CardStore, load_shelves
from vault_v2.health import HealthStore
from vault_v2.ops import VaultError, VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.reader import StagingReader
from vault_v2.tasks import TaskStore
from vault_v2.tasks import Task
from vault_v2.health import Entry


CONFLICT = "proposals changed — refresh the list"


class Model(Backend):
    info = BackendInfo("fake", "fake", "fake")

    def __init__(self):
        self.reply = "[]"
        self.error = None

    def chat(self, system, messages, on_chunk):
        if self.error:
            raise self.error
        return self.reply


@pytest.fixture()
def vault(tmp_path):
    load_shelves({})
    ops = VaultOps(VaultPaths(tmp_path / "synthetic-vault"))
    source = ops.paths.staging / "Synthetic.txt"
    source.write_text("Refill before 2026-10-16.\nPrescription example 50 mg.\n", encoding="utf-8")
    cards = CardStore(ops.paths.root / ".cards", ops.log)
    tasks = TaskStore(ops.paths.root / ".tasks", ops.log)
    health = HealthStore(ops.paths.root / ".health", ops.log)
    model = Model()
    api = AgentAPI(ops.paths.staging, StagingReader(ops, cards, purpose="agent"),
                   cards, tasks, health, lambda: model)
    return ops, api, model, source


def test_task_handle_is_fresh_and_does_not_rebind_when_payload_changes(vault):
    ops, api, model, _source = vault
    model.reply = json.dumps([{"doc": "doc-001", "title": "Refill", "due": "2026-10-16",
                               "quote": "Refill before 2026-10-16."}])
    first = api.tasks_propose()["proposals"][0]
    model.reply = model.reply.replace("2026-10-16\",", "2026-10-17\",")
    second = api.tasks_propose()["proposals"][0]
    assert first["task_id"] == second["task_id"]
    assert first["id"] != second["id"] and first["id"] != first["task_id"]
    before = ops.log.tail(100)
    with pytest.raises(VaultError, match=CONFLICT):
        api.tasks_add([first["id"]])
    assert api.tasks.all() == [] and ops.log.tail(100) == before
    second["due"] = "2030-01-01"
    assert api.tasks_add([second["id"]]) == {"added": 1}
    assert api.tasks.all()[0].due is None
    assert "date_not_in_quote" in api.tasks.all()[0].flags


def propose(api, family):
    return getattr(api, f"{family}_propose")()["proposals"]


def accept(api, family, handles):
    if family == "sort":
        return api.sort_confirm([{"id": handle} for handle in handles])
    return getattr(api, f"{family}_add")(handles)


@pytest.mark.parametrize("family,natural,field,new_value", [
    ("sort", "sha256", "shelf", "TAXES"),
    ("health", "entry_id", "label", "Caller changed this"),
])
def test_sort_and_health_freeze_the_shown_row_and_issue_one_use_handles(vault, family, natural, field, new_value):
    _ops, api, _model, _source = vault
    first = propose(api, family)[0]
    second = propose(api, family)[0]
    assert first[natural] == second[natural]
    assert first["id"] != second["id"] and second["id"] != second[natural]
    for rejected in (first["id"], second[natural]):
        with pytest.raises(VaultError, match=CONFLICT):
            accept(api, family, [rejected])
    original = second[field]
    second[field] = new_value
    result = accept(api, family, [second["id"]])
    assert result["confirmed" if family == "sort" else "added"] == 1
    stored = api.cards.get(second[natural]) if family == "sort" else next(e for e in api.health.all() if e.id == second[natural])
    assert getattr(stored, field) == original
    with pytest.raises(VaultError, match=CONFLICT):
        accept(api, family, [second["id"]])


@pytest.mark.parametrize("family", ["sort", "tasks", "health"])
def test_successful_empty_publication_invalidates_previous_handles(vault, family):
    ops, api, _model, source = vault
    old = propose(api, family)[0]["id"]
    source.unlink()
    assert propose(api, family) == []
    before = ops.log.tail(100)
    with pytest.raises(VaultError, match=CONFLICT):
        accept(api, family, [old])
    assert ops.log.tail(100) == before


@pytest.mark.parametrize("family", ["sort", "tasks", "health"])
@pytest.mark.parametrize("empty", [False, True])
def test_recreated_api_publication_invalidates_previous_instance(vault, family, empty):
    ops, api, model, source = vault
    old = propose(api, family)[0]["id"]
    replacement = AgentAPI(api.staging, api.reader, api.cards, api.tasks, api.health, lambda: model)
    if empty:
        source.unlink()
    rows = propose(replacement, family)
    assert bool(rows) is not empty
    before = ops.log.tail(100)
    stores = api.cards.all(), api.tasks.all(), api.health.all()
    with pytest.raises(VaultError, match=CONFLICT):
        accept(api, family, [old])
    assert ops.log.tail(100) == before
    assert (api.cards.all(), api.tasks.all(), api.health.all()) == stores
    if rows:
        assert accept(replacement, family, [rows[0]["id"]])["confirmed" if family == "sort" else "added"] == 1


@pytest.mark.parametrize("family", ["sort", "tasks", "health"])
def test_desktop_store_mutation_invalidates_phone_batch_before_confirm(vault, family):
    ops, api, _model, source = vault
    old = propose(api, family)[0]["id"]
    if family == "sort":
        api.cards.propose(api.cards.for_path(source), source)
    elif family == "tasks":
        api.tasks.add(Task("desktop-task", "synthetic", "desktop.txt", "Desktop task", None, "quote", "BASELINE"))
    else:
        api.health.add(Entry("desktop-entry", "synthetic", "desktop.txt", None, "OTHER", "Desktop entry", "quote", "BASELINE"))
    before = ops.log.tail(100)
    with pytest.raises(VaultError, match=CONFLICT):
        accept(api, family, [old])
    assert ops.log.tail(100) == before


@pytest.mark.parametrize("family", ["sort", "tasks", "health"])
def test_invalid_mixed_selections_write_nothing_and_keep_valid_handle_usable(vault, family):
    ops, api, _model, _source = vault
    old = propose(api, family)[0]["id"]
    current = propose(api, family)[0]
    other = propose(api, "health" if family != "health" else "tasks")[0]["id"]
    natural = current[{"sort": "sha256", "tasks": "task_id", "health": "entry_id"}[family]]
    before = ops.log.tail(100)
    stores_before = (api.cards.all(), api.tasks.all(), api.health.all())
    for bad in (old, other, natural, "unknown", 42, None, {}, [], current["id"]):
        with pytest.raises(VaultError, match=CONFLICT):
            accept(api, family, [current["id"], bad])
        assert (api.cards.all(), api.tasks.all(), api.health.all()) == stores_before
        assert ops.log.tail(100) == before
    for malformed in (None, {}, "id", [None], [42]):
        with pytest.raises(VaultError, match=CONFLICT):
            if family == "sort":
                api.sort_confirm(malformed)
            else:
                getattr(api, f"{family}_add")(malformed)
        assert ops.log.tail(100) == before
    assert accept(api, family, [current["id"]])["confirmed" if family == "sort" else "added"] == 1


def test_all_sort_edits_are_validated_before_any_confirmation(vault):
    ops, api, _model, source = vault
    source.with_name("Second.txt").write_text("Synthetic second document", encoding="utf-8")
    rows = propose(api, "sort")
    assert len(rows) == 2
    before = ops.log.tail(100)
    drafts = api.cards.all()
    for bad_edit in ({"shelf": "BOGUS"}, {"topics": []}, {"recipients": 4}, {"year": {}}, {"issuer": "x" * 121}):
        with pytest.raises(VaultError, match=CONFLICT):
            api.sort_confirm([{"id": rows[0]["id"], "shelf": "TAXES"}, {"id": rows[1]["id"], **bad_edit}])
        assert api.cards.all() == drafts and ops.log.tail(100) == before
    assert api.sort_confirm([{"id": row["id"], "shelf": "TAXES"} for row in rows]) == {"confirmed": 2, "errors": []}


@pytest.mark.parametrize("family", ["sort", "tasks", "health"])
@pytest.mark.parametrize("failure_point", ["replace", "receipt_append"])
def test_failed_store_write_reports_partial_outcome_and_burns_entire_selection(vault, family, failure_point, monkeypatch):
    ops, api, _model, source = vault
    for number in (2, 3):
        source.with_name(f"Synthetic{number}.txt").write_text(
            f"Refill medicine {number} by 2026-10-16.\n", encoding="utf-8")
    rows = propose(api, family)[:3]
    assert len(rows) == 3
    handles = [row["id"] for row in rows]
    store = {"sort": api.cards, "tasks": api.tasks, "health": api.health}[family]
    replace = Path.replace
    open_file = Path.open
    writes = []
    appends = []

    def fail_second_replace(path, target):
        if Path(target) == store.file:
            writes.append(path)
            if failure_point == "replace" and len(writes) == 2:
                raise OSError("synthetic failure after in-memory mutation")
        return replace(path, target)

    def fail_second_receipt(path, mode="r", *args, **kwargs):
        if path == ops.log.file and mode == "a":
            appends.append(path)
            if failure_point == "receipt_append" and len(appends) == 2:
                raise OSError("synthetic failure after store persistence")
        return open_file(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "replace", fail_second_replace)
    monkeypatch.setattr(Path, "open", fail_second_receipt)
    result = accept(api, family, handles)
    assert result["confirmed" if family == "sort" else "added"] == 1
    assert result["outcome"] == {"completed": handles[:1], "failed_unknown": handles[1:2], "not_attempted": handles[2:]}
    assert len(writes) == 2
    if failure_point == "receipt_append":
        reloaded = type(store)(store.dir, ops.log)
        persisted = [entry for entry in reloaded.all() if entry.confirmed] if family == "sort" else reloaded.all()
        assert len(persisted) == 2, "failed_unknown can already have persisted; it is not a rollback claim"
    before = ops.log.tail(100)
    for handle in handles:
        with pytest.raises(VaultError, match="writes blocked until restart"):
            accept(api, family, [handle])
    assert ops.log.tail(100) == before


@pytest.mark.parametrize("family", ["sort", "tasks", "health"])
def test_one_batch_never_offers_two_handles_for_the_same_store_identity(vault, family):
    _ops, api, model, source = vault
    source.write_text("Synthetic evidence line.", encoding="utf-8")
    if family == "sort":
        source.with_name("SyntheticCopy.txt").write_bytes(source.read_bytes())
    else:
        # The stores truncate normalized titles/labels at 60 chars in their IDs.
        common = {"doc": "doc-001", "quote": "Synthetic evidence line.", "kind": "OTHER"}
        field = "title" if family == "tasks" else "label"
        model.reply = json.dumps([{**common, field: "x" * 60 + suffix} for suffix in (" first", " second")])
    rows = propose(api, family)
    assert len(rows) == 1
    assert accept(api, family, [rows[0]["id"]])["confirmed" if family == "sort" else "added"] == 1
    store = {"sort": api.cards, "tasks": api.tasks, "health": api.health}[family]
    assert len(store.all()) == 1


@pytest.mark.parametrize("family", ["sort", "tasks", "health"])
def test_thrown_model_error_preserves_previous_batch(vault, family):
    _ops, api, model, _source = vault
    old = propose(api, family)[0]["id"]
    model.error = RuntimeError("synthetic offline model")
    with pytest.raises(RuntimeError, match="synthetic offline"):
        propose(api, family)
    assert accept(api, family, [old])["confirmed" if family == "sort" else "added"] == 1


@pytest.mark.parametrize("family", ["sort", "tasks", "health"])
def test_no_model_baselines_use_the_same_handle_authorization(vault, family):
    _ops, api, _model, _source = vault
    api.get_backend = lambda: None
    result = getattr(api, f"{family}_propose")()
    assert "no agent" in " ".join(result["notes"])
    rows = result["proposals"]
    assert rows and all(row["origin"] == "BASELINE" for row in rows)
    assert accept(api, family, [rows[0]["id"]])["confirmed" if family == "sort" else "added"] == 1


@pytest.mark.parametrize("family", ["sort", "tasks", "health"])
def test_a_successful_subset_makes_unselected_rows_require_refresh(vault, family):
    _ops, api, _model, source = vault
    source.with_name("Another.txt").write_text("Refill another example by 2026-11-16.", encoding="utf-8")
    rows = propose(api, family)
    assert len(rows) >= 2
    assert accept(api, family, [rows[0]["id"]])["confirmed" if family == "sort" else "added"] == 1
    with pytest.raises(VaultError, match=CONFLICT):
        accept(api, family, [rows[1]["id"]])
