"""Deterministic request estimation, including serialization and exact boundary."""

import json
from pathlib import Path

import pytest

from vault_v2 import document_budget as policy


def test_estimate_counts_complete_unicode_json_and_rounds_up():
    system = 'A system with "quotes" and \n framing'
    messages = [{"role": "user", "content": "Мигрень\n\"clinical\" \\ path"}]
    raw = json.dumps({"system": system, "messages": messages}, ensure_ascii=False,
                     separators=(",", ":")).encode("utf-8")
    assert policy.estimate_document_tokens(system, messages) == (len(raw) * 125 + 442) // 443


def test_estimated_input_boundary_accepts_exact_limit_then_refuses_whole_request():
    # Serialized envelope has a constant ASCII byte overhead; no private helpers.
    envelope = len(json.dumps({"system": "", "messages": []}, separators=(",", ":")))
    accepted_bytes = (5888 * 443) // 125
    accepted_system = "a" * (accepted_bytes - envelope)
    assert policy.estimate_document_tokens(accepted_system, []) == 5888
    policy.admit_document_request(accepted_system, [], documents=("doc-001 — sample.txt",))
    with pytest.raises(policy.DocumentBudgetExceeded, match="request too large") as refused:
        policy.admit_document_request(accepted_system + "a", [],
                                      documents=("doc-001 — sample.txt", "doc-002 — second.txt"),
                                      reading_notes=("read 6 000 of 48 210 characters",))
    assert refused.value.documents == ("doc-001 — sample.txt", "doc-002 — second.txt")
    assert "estimated" in str(refused.value) and "5888" in str(refused.value)
    assert "read 6 000 of 48 210" in str(refused.value)


def test_calibrated_policy_is_request_local_and_not_exact_token_count():
    assert policy.DOCUMENT_CONTEXT_TOKENS == 8192
    assert policy.DOCUMENT_OUTPUT_TOKENS == 2048
    assert policy.DOCUMENT_TEMPLATE_RESERVE == 256
    assert policy.DOCUMENT_INPUT_TOKENS == 5888


def test_historical_calibration_supports_factor_without_claiming_output_success():
    evidence = json.loads((Path(__file__).parent / "fixtures" /
                           "document-budget-input-calibration.json").read_text(encoding="utf-8"))
    assert evidence["complete"] is True
    cases = evidence["cases"]
    assert {(row["language"], row["excerpt_unicode_chars"], row["job"]) for row in cases} == {
        (language, length, job) for language in ("en", "ru", "mixed")
        for length in (1000, 3000, 6000) for job in ("sort", "tasks", "health")}
    for row in cases:
        prompt = row["prompt"]
        estimate = policy.estimate_document_tokens(prompt["system"], prompt["messages"])
        assert estimate >= row["prompt_eval_count"] * 1.25
        assert estimate <= policy.DOCUMENT_INPUT_TOKENS
    # This is historical INPUT calibration, not a claim that all outputs passed.
    assert next(row for row in cases if row["case"] == "ru-6000-health")["done_reason"] == "length"
