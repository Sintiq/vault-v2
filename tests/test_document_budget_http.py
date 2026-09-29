"""Budget refusal stays visible over the real authenticated phone HTTP seam."""

import pytest

from test_proposal_model_failure import model_server, post, accept, valid_reply
from vault_v2.document_budget import DocumentBudgetExceeded, DocumentOutputIncomplete


@pytest.mark.parametrize("family", ["sort", "tasks", "health"])
def test_http_budget_refusal_is_413_with_documents_and_keeps_previous_batch(model_server, family):
    server, api, backend, ops = model_server
    backend.reply = valid_reply(family)
    code, first = post(server, f"/api/agent/{family}", {})
    assert code == 200
    old = first["proposals"][0]["id"]
    before = [row for row in ops.log.tail(100) if row["op"] != "agent_read"]
    backend.error = DocumentBudgetExceeded(
        "request too large — the whole request was refused before model inference",
        documents=("doc-001 — Prescription.txt", "doc-002 — synthetic-second.txt"),
        reading_notes=("Prescription.txt: read 6 000 of 48 210 characters — the rest was not checked",))
    code, result = post(server, f"/api/agent/{family}", {})
    assert code == 413
    assert result["documents"] == ["doc-001 — Prescription.txt", "doc-002 — synthetic-second.txt"]
    assert "request too large" in result["error"]
    assert any("read 6 000 of 48 210" in note for note in result["notes"])
    assert "proposals" not in result
    assert [row for row in ops.log.tail(100) if row["op"] != "agent_read"] == before
    assert accept(server, family, old)[0] == 200, "refusal is not publication of a new batch"


@pytest.mark.parametrize("family", ["sort", "tasks", "health", "ask"])
def test_http_incomplete_output_is_distinct_and_names_documents(model_server, family):
    server, api, backend, ops = model_server
    before = [row for row in ops.log.tail(100) if row["op"] != "agent_read"]
    backend.error = DocumentOutputIncomplete("Document response incomplete: output limit")
    code, result = post(server, f"/api/agent/{family}", {"phrase": "prescription"})
    assert code == 422
    label = "staging/Prescription.txt" if family == "ask" else "Prescription.txt"
    assert result["documents"] == [f"doc-001 — {label}"]
    assert "incomplete" in result["error"] and "before model" not in result["error"]
    assert "proposals" not in result
    assert [row for row in ops.log.tail(100) if row["op"] != "agent_read"] == before


@pytest.mark.parametrize("family", ["sort", "tasks", "health", "ask"])
def test_http_real_aggregate_guard_makes_zero_model_calls(model_server, family):
    server, api, backend, ops = model_server
    old = None
    if family != "ask":
        backend.reply = valid_reply(family)
        code, initial = post(server, f"/api/agent/{family}", {})
        assert code == 200
        old = initial["proposals"][0]["id"]
    for index in range(20):
        (ops.paths.staging / f"long-{index:02d}.txt").write_text("ж" * 48210, encoding="utf-8")
    calls = []
    def count(system, messages, on_chunk):
        calls.append(messages)
        return backend.reply
    backend.chat = count
    before = [row for row in ops.log.tail(1000) if row["op"] != "agent_read"]
    payload = {"phrase": "синтетический вопрос " * 3000} if family == "ask" else {}
    code, result = post(server, f"/api/agent/{family}", payload)
    assert code == 413 and calls == []
    assert "before model inference" in result["error"]
    assert len(result["documents"]) == 21
    prefix = "staging/" if family == "ask" else ""
    assert {label.split(" — ", 1)[1] for label in result["documents"]} == {
        prefix + "Prescription.txt", *(f"{prefix}long-{index:02d}.txt" for index in range(20))}
    if family != "ask":
        assert any("read 6 000 of 48 210" in note for note in result["notes"])
    assert [row for row in ops.log.tail(1000) if row["op"] != "agent_read"] == before
    if old:
        assert accept(server, family, old)[0] == 200
