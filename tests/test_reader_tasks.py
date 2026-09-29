"""Reader (allowlisted reads) and Tasks (quote-checked proposals)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vault_v2.agent import Backend, BackendInfo
from vault_v2.cards import CardStore
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.reader import ReadRefused, StagingReader
from vault_v2.tasks import TaskStore, baseline_tasks, collect_docs, propose


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
    return ops, cards, StagingReader(ops, cards, purpose="test")


def _mk(p: Path, content: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_reader_reads_staging_text_and_leaves_receipt(env) -> None:
    ops, _cards, reader = env
    _mk(ops.paths.staging / "a.txt", "hello world")
    assert [e.rel for e in reader.list_staging()] == ["a.txt"]
    assert reader.read_text("a.txt") == "hello world"
    last = ops.log.tail(1)[0]
    assert last["op"] == "agent_read" and last["extra"]["chars"] == 11 and last["extra"]["purpose"] == "test"


def test_reader_refuses_everything_else(env) -> None:
    ops, _cards, reader = env
    _mk(ops.paths.documents / "secret.txt", "SECRET")
    _mk(ops.paths.personal / "private.txt", "SECRET")
    (ops.paths.staging / "scan.pdf").write_bytes(b"%PDF")
    for bad in ("../documents/secret.txt", str(ops.paths.personal / "private.txt"), "missing.txt", "scan.pdf", "../.receipts/receipts.jsonl"):
        with pytest.raises(ReadRefused):
            reader.read_text(bad)
    assert all(r["op"] != "agent_read" for r in ops.log.tail(20)), "a refused read leaves no read receipt"


def test_task_quote_must_exist_in_document(env) -> None:
    ops, _cards, reader = env
    _mk(ops.paths.staging / "rx.txt", "Sumatriptan 50 mg. Refill before 2026-10-16. Take as needed.")
    docs = collect_docs(reader)
    reply = json.dumps([
        {"doc": "doc-001", "title": "Refill sumatriptan", "due": "2026-10-16", "quote": "Refill before 2026-10-16"},
        {"doc": "doc-001", "title": "Book an MRI", "due": None, "quote": "MRI recommended within a month"},
        {"doc": "doc-001", "title": "Bad date", "due": "next week", "quote": "Take as needed"},
    ])
    tasks, notes = propose(reader, FakeBackend(reply))
    titles = {t.title: t for t in tasks if t.origin == "AGENT"}
    assert "Refill sumatriptan" in titles and titles["Refill sumatriptan"].due == "2026-10-16"
    assert "Book an MRI" not in titles and any("quote not in document" in n for n in notes)
    assert titles["Bad date"].due is None
    assert docs[0].text.startswith("Sumatriptan")


def test_baseline_and_store_round_trip(env) -> None:
    ops, _cards, reader = env
    _mk(ops.paths.staging / "policy.txt", "Your policy expires 2026-12-01. Renew online.")
    tasks, notes = propose(reader, None)
    assert tasks and tasks[0].origin == "BASELINE" and tasks[0].due == "2026-12-01" and "baseline" in notes[0]
    store = TaskStore(ops.paths.root / ".tasks", ops.log)
    store.add(tasks[0])
    store.set_done(tasks[0].id, True)
    again = TaskStore(ops.paths.root / ".tasks", ops.log)
    assert again.all()[0].done and again.has(tasks[0].id)
    assert [r["op"] for r in ops.log.tail(2)] == ["task_add", "task_done"]
    assert baseline_tasks([]) == []
