"""Health completeness and model-output limits at the real HTTP boundary."""

import json

import pytest

from test_proposal_model_failure import model_server, post, accept, valid_reply


LIMIT_NOTE = "Health lists at most 12 entries per document; a long record may have more"
REFILL_QUOTE = "Refill prescription before 2026-10-16."


@pytest.mark.parametrize("use_model", [True, False])
def test_health_success_puts_completeness_note_before_long_text_coverage(model_server, use_model):
    server, api, backend, ops = model_server
    text = (REFILL_QUOTE + "\n" + "Ordinary synthetic record. " * 2000)[:48210]
    (ops.paths.staging / "Prescription.txt").write_text(text, encoding="utf-8", newline="\n")
    backend.reply = valid_reply("health")
    if not use_model:
        api.get_backend = lambda: None

    code, body = post(server, "/api/agent/health", {})

    assert code == 200
    assert body["notes"][0] == LIMIT_NOTE
    assert any("read 6 000 of 48 210 characters — the rest was not checked" in note
               for note in body["notes"][1:]), body["notes"]


def test_empty_health_request_still_reports_limited_completeness(model_server):
    server, _api, _backend, ops = model_server
    (ops.paths.staging / "Prescription.txt").unlink()

    code, body = post(server, "/api/agent/health", {})

    assert code == 200 and body["proposals"] == []
    assert body["notes"][0] == LIMIT_NOTE


@pytest.mark.parametrize("violation", ["thirteen-entries", "quote-201"])
def test_health_limit_refusal_keeps_previous_batch_and_never_publishes_baseline(model_server, violation):
    server, api, backend, ops = model_server
    oversized_quote = "Synthetic grounded quote ".ljust(201, "a")
    (ops.paths.staging / "Prescription.txt").write_text(
        REFILL_QUOTE + "\n" + oversized_quote, encoding="utf-8")
    (ops.paths.staging / "Second.txt").write_text("Additional synthetic source.", encoding="utf-8")
    backend.reply = valid_reply("health")
    code, first = post(server, "/api/agent/health", {})
    assert code == 200
    previous = next(row["id"] for row in first["proposals"] if row["origin"] == "AGENT")
    saved_before = api.health.all()
    before = [row for row in ops.log.tail(1000) if row["op"] != "agent_read"]
    if violation == "thirteen-entries":
        rows = [{"doc": "doc-001", "label": f"Distinct entry {index:02d}", "date": None,
                 "kind": "OTHER", "quote": REFILL_QUOTE} for index in range(1, 14)]
    else:
        rows = [{"doc": "doc-001", "label": "Oversized quote", "date": None,
                 "kind": "OTHER", "quote": oversized_quote}]
    backend.reply = json.dumps(rows)

    code, body = post(server, "/api/agent/health", {})

    assert code == 422
    assert body["documents"] == ["doc-001 — Prescription.txt", "doc-002 — Second.txt"]
    assert "proposals" not in body
    assert api.health.all() == saved_before
    assert [row for row in ops.log.tail(1000) if row["op"] != "agent_read"] == before
    assert accept(server, "health", previous) == (200, {"added": 1})


def test_twelve_grounded_model_entries_with_200_character_quotes_are_accepted(model_server):
    server, _api, backend, ops = model_server
    quotes = [f"Record {index:02d}: synthetic statement ".ljust(200, "x") for index in range(1, 13)]
    (ops.paths.staging / "Prescription.txt").write_text("\n".join(quotes), encoding="utf-8")
    backend.reply = json.dumps([
        {"doc": "doc-001", "label": f"Distinct entry {index:02d}", "date": None,
         "kind": "OTHER", "quote": quote} for index, quote in enumerate(quotes, 1)
    ])

    code, body = post(server, "/api/agent/health", {})

    assert code == 200
    assert body["notes"][0] == LIMIT_NOTE
    assert len(body["proposals"]) == 12
    assert all(row["origin"] == "AGENT" and len(row["quote"]) == 200 for row in body["proposals"])
    assert {row["quote"] for row in body["proposals"]} == set(quotes)
