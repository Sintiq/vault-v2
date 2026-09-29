"""Health timeline — quotes only, no interpretation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vault_v2.agent import Backend, BackendInfo
from vault_v2.cards import CardStore
from vault_v2.health import HealthStore, baseline_entries, collect_docs, propose
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.reader import StagingReader


class FakeBackend(Backend):
    def __init__(self, reply: str):
        self.reply, self.seen = reply, []
        self.info = BackendInfo("fake", "fake", "fake")

    def chat(self, system, messages, on_chunk):
        self.seen.append(messages)
        return self.reply


@pytest.fixture()
def env(tmp_path: Path):
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    cards = CardStore(ops.paths.root / ".cards", ops.log)
    return ops, StagingReader(ops, cards, purpose="test")


def _mk(p: Path, content: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_entry_must_quote_the_document(env) -> None:
    ops, reader = env
    _mk(ops.paths.staging / "visit.txt",
        "Visit on 2026-03-15 at Bay Neurology Clinic.\nAssessment: migraine without aura.\nSumatriptan 50 mg prescribed.")
    reply = json.dumps([
        {"doc": "doc-001", "date": "2026-03-15", "kind": "VISIT", "label": "Visit at Bay Neurology Clinic",
         "quote": "Visit on 2026-03-15 at Bay Neurology Clinic."},
        {"doc": "doc-001", "date": "2026-03-15", "kind": "DIAGNOSIS", "label": "Migraine without aura",
         "quote": "Assessment: migraine without aura."},
        {"doc": "doc-001", "date": "2026-03-15", "kind": "DIAGNOSIS", "label": "Hypertension",
         "quote": "Blood pressure elevated, hypertension noted"},
    ])
    entries, notes = propose(reader, FakeBackend(reply))
    agent = {e.label: e for e in entries if e.origin == "AGENT"}
    assert "Visit at Bay Neurology Clinic" in agent and agent["Visit at Bay Neurology Clinic"].date == "2026-03-15"
    assert "Migraine without aura" in agent
    assert "Hypertension" not in agent, "a fact the document does not state is dropped"
    assert any("quote not in document" in n for n in notes)


def test_dates_are_validated_and_partial_dates_allowed(env) -> None:
    ops, reader = env
    _mk(ops.paths.staging / "note.txt", "Flu shot given in 2025-11. Booster due later.")
    reply = json.dumps([
        {"doc": "doc-001", "date": "2025-11", "kind": "VACCINATION", "label": "Flu shot", "quote": "Flu shot given in 2025-11."},
        {"doc": "doc-001", "date": "sometime in autumn", "kind": "OTHER", "label": "Booster", "quote": "Booster due later."},
        {"doc": "doc-001", "date": "2025-13-40", "kind": "NOPE", "label": "Bad kind and date", "quote": "Flu shot given in 2025-11."},
    ])
    entries, _notes = propose(reader, FakeBackend(reply))
    by = {e.label: e for e in entries if e.origin == "AGENT"}
    assert by["Flu shot"].date == "2025-11" and by["Flu shot"].year == "2025"
    assert by["Booster"].date is None and by["Booster"].year == "undated"
    assert by["Bad kind and date"].date is None and by["Bad kind and date"].kind == "OTHER"


def test_baseline_and_store_round_trip(env) -> None:
    ops, reader = env
    _mk(ops.paths.staging / "lab.txt", "Lab result 2026-01-20: metabolic panel within range.")
    entries, notes = propose(reader, None)
    assert entries and entries[0].origin == "BASELINE" and entries[0].kind == "TEST" and entries[0].date == "2026-01-20"
    store = HealthStore(ops.paths.root / ".health", ops.log)
    store.add(entries[0])
    again = HealthStore(ops.paths.root / ".health", ops.log)
    assert again.has(entries[0].id) and again.by_year()[0][0] == "2026"
    assert "1 entries: 1 test" in again.summary()
    assert [r["op"] for r in ops.log.tail(1)] == ["health_add"]
    again.remove(entries[0].id)
    assert HealthStore(ops.paths.root / ".health", ops.log).all() == []
    assert baseline_entries([]) == []


def test_agent_only_sees_staging_text(env) -> None:
    ops, reader = env
    _mk(ops.paths.staging / "s.txt", "Visit on 2026-02-02.")
    _mk(ops.paths.personal / "private.txt", "SECRET-DIAGNOSIS")
    fb = FakeBackend("[]")
    propose(reader, fb)
    assert "SECRET" not in json.dumps(fb.seen)
    assert len(collect_docs(reader)) == 1


def test_undated_group_comes_last(env) -> None:
    ops, reader = env
    _mk(ops.paths.staging / "mixed.txt", "Visit on 2026-03-15 at the clinic.\nFollow-up appointment to be scheduled.")
    reply = json.dumps([
        {"doc": "doc-001", "date": "2026-03-15", "kind": "VISIT", "label": "Clinic visit", "quote": "Visit on 2026-03-15 at the clinic."},
        {"doc": "doc-001", "date": None, "kind": "VISIT", "label": "Follow-up", "quote": "Follow-up appointment to be scheduled."},
    ])
    entries, _ = propose(reader, FakeBackend(reply))
    store = HealthStore(ops.paths.root / ".health", ops.log)
    for e in entries:
        if e.origin == "AGENT":
            store.add(e)
    assert [year for year, _ in store.by_year()] == ["2026", "undated"]
