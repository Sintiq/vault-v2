"""Dates are quoted evidence; owner edits are deliberate guarded overrides."""
from dataclasses import asdict, replace
from datetime import date
import json

import pytest

from vault_v2.dates import clean_date, days_left, find_dates, overdue, relative_status, resolve_date
from vault_v2.errors import InvalidJournal, VaultReadOnly
from vault_v2.health import Entry, HealthDoc, HealthStore, agent_entries, baseline_entries, view_entry
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.tasks import Task, TaskDoc, TaskStore, agent_tasks, baseline_tasks, view_task


@pytest.mark.parametrize("literal", ["2026-10-15", "10/15/2026", "15.10.2026", "October 15, 2026",
                                     "Oct 15 2026", "15 октября 2026"])
def test_every_requested_format(literal):
    result = resolve_date(f"Renew by {literal}.", "2026-10-15")
    assert (result.value, result.due_source, result.flags) == ("2026-10-15", "quote", ())
    inferred = resolve_date(f"Renew by {literal}.")
    assert inferred.value == "2026-10-15" and inferred.flags == ("date_from_quote",)


def test_ambiguous_us_date_is_visible_and_no_european_swap():
    result = resolve_date("Pay by 10/11/2026.")
    assert result.value == "2026-10-11" and "ambiguous_date" in result.flags
    wrong = resolve_date("Pay by 10/11/2026.", "2026-11-10")
    assert wrong.value is None and set(wrong.flags) == {"ambiguous_date", "date_not_in_quote"}
    assert resolve_date("Pay by 15/10/2026.").value is None


def test_two_distinct_dates_need_a_matching_model_selection():
    quote = "Visit 2026-10-01; return by October 15, 2026."
    assert resolve_date(quote).flags == ("several_dates",)
    assert resolve_date(quote).value is None
    assert resolve_date(quote, "2026-10-15").value == "2026-10-15"
    repeated = resolve_date("Due 2026-10-15 (October 15, 2026).")
    assert repeated.value == "2026-10-15" and "several_dates" not in repeated.flags


@pytest.mark.parametrize("quote", ["Return within 30 days.", "Вернуть в течение 30 дней.",
                                    "Repeat in 2 weeks.", "Ответить через 7 дней."])
def test_relative_deadlines_never_get_a_guessed_base(quote):
    assert resolve_date(quote).value is None
    assert "relative_deadline" in resolve_date(quote).flags
    guessed = resolve_date(quote, "2026-10-22")
    assert guessed.value is None
    assert set(guessed.flags) == {"relative_deadline", "date_not_in_quote"}


def test_relative_deadline_with_issue_date_does_not_assume_that_is_due():
    result = resolve_date("Issued 2026-09-22; reply within 30 days.")
    assert result.value is None and "relative_deadline" in result.flags


@pytest.mark.parametrize("literal", ["2026-02-29", "31.04.2026", "02/30/2026", "February 29, 2026",
                                     "2026-13-10", "2026-00-10", "0000-01-01"])
def test_impossible_dates_do_not_degrade_to_partial_year(literal):
    assert find_dates(literal, allow_partial=True) == ()
    assert resolve_date(literal, "2026-02-28").value is None


@pytest.mark.parametrize("quote,value", [("Visit in 2026.", "2026"), ("Visit in 2026-10.", "2026-10")])
def test_health_partial_date_requires_literal_quote(quote, value):
    assert resolve_date(quote, value, allow_partial=True).value == value
    assert resolve_date("Visit at the clinic.", value, allow_partial=True).value is None
    assert resolve_date("Visit on 2026-10-15.", value, allow_partial=True).value is None


@pytest.mark.parametrize("value", ["2026-1-01", "20260101", "2026-W40-4", "2026-02-29", 2026, True])
def test_model_iso_is_strict(value):
    assert clean_date(value) is None


@pytest.mark.parametrize("today,due,expected", [
    (date(2026,9,22), "2026-10-15", 23),
    (date(2026,12,31), "2027-01-01", 1),
    (date(2024,2,28), "2024-03-01", 2),
    (date(2026,2,28), "2026-03-01", 1),
    (date(2026,10,17), "2026-10-15", -2),
    (date(2026,10,15), "2026-10-15", 0),
])
def test_calendar_arithmetic(today, due, expected):
    assert days_left(due, today) == expected
    assert overdue(due, today) == (expected < 0)


def test_human_status_has_no_model_math():
    assert relative_status("2026-10-15", date(2026,9,22)) == "in 23 days"
    assert relative_status("2026-10-15", date(2026,10,17)) == "overdue 2 days"
    assert relative_status("2026-10-15", date(2026,10,15)) == "today"
    assert days_left(None) is None and days_left("2026-10") is None
    assert not overdue(None)


class Reply:
    def __init__(self, rows):
        self.rows = rows

    def chat(self, *args):
        return json.dumps(self.rows)


def task(quote="Renew by 2026-10-15.", due="2026-10-15"):
    return Task("t1", "abc" * 22, "synthetic.txt", "Renew", due, quote, "AGENT")


def entry(quote="Visit on 2026-10-15.", value="2026-10-15"):
    return Entry("h1", "abc" * 22, "synthetic.txt", value, "VISIT", "Visit", quote, "AGENT")


@pytest.fixture
def stores(tmp_path):
    ops = VaultOps(VaultPaths(tmp_path / "synthetic"))
    return ops, TaskStore(ops.paths.root / ".tasks", ops.log), HealthStore(ops.paths.root / ".health", ops.log)


def test_wrong_model_date_keeps_task_and_remains_null_after_saving(stores):
    ops, tasks, _health = stores
    quote = "Renew by 2026-10-15."
    docs = [TaskDoc("doc-001", "synthetic.txt", "synthetic.txt", "abc"*22, quote)]
    accepted, dropped = agent_tasks(Reply([{"doc":"doc-001", "title":"Renew", "due":"2026-12-30", "quote":quote}]), docs)
    assert not dropped and len(accepted) == 1
    proposed = accepted[0]
    assert proposed.due is None and proposed.flags == ("date_not_in_quote",)
    tasks.add(proposed)
    reloaded = TaskStore(tasks.dir, ops.log).all()[0]
    assert reloaded.due is None and reloaded.due_source == "none"
    assert reloaded.flags == ("date_not_in_quote",)


def test_health_date_cannot_be_borrowed_from_another_sentence():
    quote = "Assessment: migraine without aura."
    docs = [HealthDoc("doc-001", "visit-2026-10-15.txt", "abc"*22, "Visit on 2026-10-15. " + quote)]
    accepted, dropped = agent_entries(Reply([{"doc":"doc-001", "kind":"DIAGNOSIS", "label":"Migraine",
                                             "date":"2026-10-15", "quote":quote}]), docs)
    assert not dropped and accepted[0].date is None
    assert "date_not_in_quote" in accepted[0].flags


def test_health_baseline_never_uses_filename_or_unquoted_truncated_tail():
    entries = baseline_entries([HealthDoc("doc-001", "visit-2026-10-15.txt", "abc"*22, "Visit at the clinic.")])
    assert entries[0].date is None
    entries = baseline_entries([HealthDoc("doc-001", "visit.txt", "abc"*22, "Visit " + "x"*250 + " 2026-10-15")])
    assert entries[0].date is None


def test_task_baseline_parses_european_dots_without_cutting_the_quote():
    rows = baseline_tasks([TaskDoc("doc-001", "x.txt", "x.txt", "abc"*22, "Renew by 15.10.2026.")])
    assert len(rows) == 1 and rows[0].due == "2026-10-15" and "15.10.2026" in rows[0].quote


@pytest.mark.parametrize("kind", ["task", "health"])
def test_old_invalid_record_projected_not_verified_without_disk_write(stores, kind):
    _ops, tasks, health = stores
    store, record, collection = (tasks, task(due="2026-12-30"), "tasks") if kind == "task" else (health, entry(value="2026-12-30"), "entries")
    raw = asdict(record)
    raw.pop("due_source")
    raw.pop("flags")
    store.dir.mkdir(parents=True)
    store.file.write_text(json.dumps({"schema":store.SCHEMA, collection:{record.id:raw}}), encoding="utf-8")
    before = store.file.read_bytes()
    store._load()
    projected = store.all()[0]
    assert (projected.due if kind == "task" else projected.date) is None
    assert {"not_verified", "date_not_in_quote"} <= set(projected.flags)
    assert store.file.read_bytes() == before
    assert store.generation == 0


@pytest.mark.parametrize("kind", ["task", "health"])
@pytest.mark.parametrize("owner_value", ["2027-01-02", None])
def test_owner_override_survives_reload_and_duplicate_proposal(stores, kind, owner_value):
    ops, tasks, health = stores
    store, record, edit = (tasks, task(), tasks.set_due) if kind == "task" else (health, entry(), health.set_date)
    record = replace(record, flags=("date_not_in_quote", "ambiguous_date", "not_verified"))
    store.add(record)
    if kind == "task":
        tasks.set_done(record.id, True)
    generation = store.generation
    edit(record.id, owner_value)
    assert store.generation == generation + 1
    row = store.all()[0]
    assert row.due_source == "owner" and row.flags == ()
    assert (row.due if kind == "task" else row.date) == owner_value
    assert ops.log.tail(1)[0]["op"] == ("task_due_set" if kind == "task" else "health_date_set")
    reloaded = type(store)(store.dir, ops.log)
    reloaded.add(record)
    row = reloaded.all()[0]
    assert row.due_source == "owner" and row.flags == ()
    assert (row.due if kind == "task" else row.date) == owner_value
    if kind == "task":
        assert row.done
    assert ops.log.verify() > 0


@pytest.mark.parametrize("kind", ["task", "health"])
@pytest.mark.parametrize("value", ["2026-02-30", "2026-1-01", "2026", "", False, 2026])
def test_invalid_owner_edit_has_no_effect(stores, kind, value):
    ops, tasks, health = stores
    store, record, edit = (tasks, task(), tasks.set_due) if kind == "task" else (health, entry(), health.set_date)
    store.add(record)
    before, receipts, generation = store.file.read_bytes(), ops.log.file.read_bytes(), store.generation
    with pytest.raises(ValueError):
        edit(record.id, value)
    assert store.file.read_bytes() == before and ops.log.file.read_bytes() == receipts
    assert store.generation == generation


@pytest.mark.parametrize("kind", ["task", "health"])
def test_manual_edit_requires_existing_record_and_intact_root_journal(stores, kind):
    ops, tasks, health = stores
    store, record, edit = (tasks, task(), tasks.set_due) if kind == "task" else (health, entry(), health.set_date)
    with pytest.raises(KeyError):
        edit("missing", None)
    store.add(record)
    before, generation = store.file.read_bytes(), store.generation
    ops.log.read_only = True
    with pytest.raises(VaultReadOnly):
        edit(record.id, None)
    ops.log.read_only = False
    ops.log.file.write_text("broken journal\n", encoding="utf-8")
    with pytest.raises(InvalidJournal):
        edit(record.id, None)
    assert store.file.read_bytes() == before and store.generation == generation


def test_relative_deadline_does_not_accept_issue_date_hint():
    result = resolve_date("Notice issued 2026-09-22. Pay within 30 days.", "2026-09-22")
    assert result.value is None and result.due_source == "none"
    assert "relative_deadline" in result.flags
