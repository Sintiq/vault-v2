"""Health response limits through the public proposal and model boundaries."""

import json

import pytest

from vault_v2.agent import Backend, BackendInfo
from vault_v2.cards import CardStore
from vault_v2.document_budget import DocumentOutputIncomplete
from vault_v2.health import propose
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.reader import StagingReader


class ReplyBackend(Backend):
    def __init__(self, rows):
        self.rows = rows
        self.info = BackendInfo("fake", "fake", "fake")

    def chat(self, system, messages, on_chunk):
        return json.dumps(self.rows, ensure_ascii=False)


@pytest.fixture
def env(tmp_path):
    ops = VaultOps(VaultPaths(tmp_path / "synthetic-vault"))
    cards = CardStore(ops.paths.root / ".cards", ops.log)
    return ops, StagingReader(ops, cards)


def test_health_refuses_201_character_quote_without_clipping_and_keeps_reading_notes(env):
    ops, reader = env
    quote = "Visit " + "Я" * 195
    (ops.paths.staging / "long.txt").write_text(quote + "\n" + "Z" * 6000, encoding="utf-8", newline="\n")
    backend = ReplyBackend([{
        "doc": "doc-001", "date": None, "kind": "VISIT", "label": "Synthetic visit", "quote": quote,
    }])

    with pytest.raises(DocumentOutputIncomplete, match="200") as refused:
        propose(reader, backend)

    assert refused.value.documents == ("doc-001 — long.txt",)
    assert "long.txt: read 6 000 of 6 202 characters — the rest was not checked" in refused.value.reading_notes
    assert all(row["op"] != "health_add" for row in ops.log.tail())


def test_health_refuses_thirteen_rows_for_one_document_even_below_batch_limit(env):
    ops, reader = env
    (ops.paths.staging / "a.txt").write_text("Visit documented.", encoding="utf-8")
    (ops.paths.staging / "b.txt").write_text("Lab test documented.", encoding="utf-8")
    rows = [{"doc": "doc-001", "date": None, "kind": "VISIT",
             "label": f"Synthetic visit {index}", "quote": "Visit documented."}
            for index in range(13)]
    rows.append({"doc": "doc-002", "date": None, "kind": "TEST",
                 "label": "Synthetic test", "quote": "Lab test documented."})

    with pytest.raises(DocumentOutputIncomplete, match="12") as refused:
        propose(reader, ReplyBackend(rows))

    assert refused.value.documents == ("doc-001 — a.txt", "doc-002 — b.txt")
    assert "doc-001" in str(refused.value)
    assert all(row["op"] != "health_add" for row in ops.log.tail())


@pytest.mark.parametrize("with_model", [False, True])
def test_final_health_set_limits_baseline_to_remaining_places_and_discloses_cap(env, with_model):
    ops, reader = env
    lines_a = [f"Visit A {index:02d} documented." for index in range(15)]
    lines_b = [f"Visit B {index:02d} documented." for index in range(14)]
    (ops.paths.staging / "a.txt").write_text("\n".join(lines_a), encoding="utf-8", newline="\n")
    (ops.paths.staging / "b.txt").write_text("\n".join(lines_b), encoding="utf-8", newline="\n")
    rows = [{"doc": "doc-001", "date": None, "kind": "VISIT",
             "label": f"Model visit {index:02d}", "quote": lines_a[index]} for index in range(11)]

    entries, notes = propose(reader, ReplyBackend(rows) if with_model else None)

    assert len([entry for entry in entries if entry.doc_name == "a.txt"]) == 12
    assert len([entry for entry in entries if entry.doc_name == "b.txt"]) == 12
    assert {entry.quote for entry in entries if entry.doc_name == "a.txt"} == set(lines_a[:12])
    assert {entry.quote for entry in entries if entry.doc_name == "b.txt"} == set(lines_b[:12])
    assert len([entry for entry in entries if entry.origin == "AGENT"]) == (11 if with_model else 0)
    assert notes[0] == "Health lists at most 12 entries per document; a long record may have more"
    assert "Baseline additions limited by the same 12-entry-per-document cap." in notes


def test_health_accepts_twelve_rows_per_document_and_complete_200_character_unicode_quotes(env):
    ops, reader = env
    first_quote, second_quote = "Visit " + "🙂" * 194, "Test " + "Я" * 195
    (ops.paths.staging / "a.txt").write_text(first_quote, encoding="utf-8")
    (ops.paths.staging / "b.txt").write_text(second_quote, encoding="utf-8")
    rows = [
        {"doc": doc, "date": None, "kind": "VISIT", "label": f"Model row {index:02d}", "quote": quote}
        for doc, quote in (("doc-001", first_quote), ("doc-002", second_quote))
        for index in range(12)
    ]

    entries, notes = propose(reader, ReplyBackend(rows))

    assert len(entries) == 24
    assert all(entry.origin == "AGENT" and len(entry.quote) == 200 for entry in entries)
    assert {entry.quote for entry in entries} == {first_quote, second_quote}
    assert notes == ["Health lists at most 12 entries per document; a long record may have more"]
