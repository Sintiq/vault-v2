"""A model issuer and a filename cannot invent card evidence."""
import json
from pathlib import Path

import pytest

from vault_v2.cards import UNCONFIRMED, derive_year, grounded_issuer
from vault_v2.sorting import SortInput, baseline_card, propose


@pytest.mark.parametrize("issuer,text,expected", [
    ("Bay Clinic", "Visit at BAY CLINIC on October 15, 2026.", "Bay Clinic"),
    ("Bay Clinic", "Visit at a different clinic.", UNCONFIRMED),
    ("Bay Clinic", "Visit at Bay  Clinic.", UNCONFIRMED),
    ("Bay Clinic", "Bay\nClinic", UNCONFIRMED),
    ("Bay Clinic", "", UNCONFIRMED),
    (None, "Bay Clinic", UNCONFIRMED),
])
def test_issuer_requires_a_literal_case_insensitive_match(issuer, text, expected):
    assert grounded_issuer(issuer, text) == expected


@pytest.mark.parametrize("text,name,expected", [
    ("Letter dated 2026", "archive-2028.txt", 2026),
    ("No year in this document", "tax-2026.txt", None),
    ("", "2026.pdf", None),
    ("2025 return revised in 2026", "2030.txt", 2026),
    ("number 20260001", "2026.txt", None),
])
def test_card_year_comes_from_text_only(text, name, expected):
    assert derive_year(text, name) == expected


class Backend:
    def __init__(self, issuer):
        self.issuer = issuer

    def chat(self, system, messages, on_chunk):
        return json.dumps([{"id": "one", "shelf": "HEALTH", "topics": ["visit"],
                            "issuer": self.issuer, "year": 2040, "recipients": ["DOCTOR"]}])


def doc(text="Visit on October 15, 2026", name="Bay Clinic 2030.txt", kind="TEXT"):
    return SortInput("one", Path(name), name, kind, 100, "a" * 64, text)


def test_unsupported_issuer_is_flagged_without_dropping_the_agent_card():
    proposal = propose([doc()], Backend("Bay Clinic"))[0]
    assert proposal.card.origin == "AGENT"
    assert proposal.card.issuer == UNCONFIRMED
    assert proposal.card.year == 2026
    assert "issuer_not_in_text" in proposal.flags


def test_valid_issuer_is_kept_and_model_year_is_ignored():
    proposal = propose([doc("BAY CLINIC, October 15, 2026")], Backend("Bay Clinic"))[0]
    assert proposal.card.issuer == "Bay Clinic"
    assert proposal.card.year == 2026
    assert "issuer_not_in_text" not in proposal.flags


def test_binary_filename_is_not_year_or_issuer_evidence():
    document = doc("", "Bay Clinic 2026.pdf", "BINARY")
    assert baseline_card(document).year is None
    proposal = propose([document], Backend("Bay Clinic"))[0]
    assert proposal.card.issuer == UNCONFIRMED
    assert proposal.card.year is None


def test_issuer_cannot_borrow_evidence_from_another_document():
    other = SortInput("two", Path("other.txt"), "other.txt", "TEXT", 100, "b" * 64, "Bay Clinic 2027")
    proposal = propose([doc(), other], Backend("Bay Clinic"))[0]
    assert proposal.card.issuer == UNCONFIRMED
    assert proposal.card.year == 2026
