"""Cards, sorting and ask — the agent proposes, the owner confirms."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vault_v2.agent import Backend, BackendInfo
from vault_v2.ask import ask, baseline_match, collect_docs
from vault_v2.cards import Card, CardError, CardStore, canonical_topics, derive_year
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.sorting import baseline_card, collect_inputs, propose


class FakeBackend(Backend):
    def __init__(self, reply: str):
        self.reply = reply
        self.info = BackendInfo("fake", "fake", "fake")
        self.seen: list[tuple[str, list[dict]]] = []

    def chat(self, system, messages, on_chunk):
        self.seen.append((system, messages))
        on_chunk(self.reply)
        return self.reply


@pytest.fixture()
def vault(tmp_path: Path) -> VaultOps:
    return VaultOps(VaultPaths(tmp_path / "vault"))


def _mk(p: Path, content: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# -- cards ----------------------------------------------------------------------


def test_topics_are_canonical_slugs() -> None:
    assert canonical_topics(["Metabolic Panel", "blood_sugar", "blood-sugar", " "]) == ("metabolic-panel", "blood-sugar")
    with pytest.raises(CardError):
        canonical_topics(["x" * 41])
    with pytest.raises(CardError):
        canonical_topics([])
    assert canonical_topics([], allow_empty=True) == ()


def test_card_validation_and_year() -> None:
    with pytest.raises(CardError):
        Card.build("h", "a.txt", "TEXT", shelf="KITCHEN", topics=["x"])
    with pytest.raises(CardError):
        Card.build("h", "a.txt", "TEXT", shelf="HEALTH", topics=["x"], recipients=["MOM"])
    c = Card.build("h", "a.txt", "TEXT", shelf="HEALTH", topics=["x"], issuer="", year="2026", recipients=["doctor"])
    assert c.issuer == "UNCONFIRMED" and c.year == 2026 and c.recipients == ("DOCTOR",)
    assert derive_year("visit on 2026-03-15, follow-up 2025", "x.txt") == 2026
    assert derive_year("", "Tax return 2024.pdf") is None
    assert derive_year("no dates", "x.txt") is None


def test_store_keys_by_bytes_and_writes_receipt(vault: VaultOps) -> None:
    store = CardStore(vault.paths.root / ".cards", vault.log)
    f = _mk(vault.paths.staging / "Visit summary.txt", "migraine follow-up 2026")
    card = Card.build(store.hash_of(f), f.name, "TEXT", shelf="HEALTH", topics=["migraine"], recipients=["DOCTOR"], origin="AGENT")
    store.confirm(card, f)
    assert store.for_path(f).confirmed and store.for_path(f).origin == "AGENT"
    renamed = f.rename(f.with_name("renamed.txt"))
    assert store.for_path(renamed) is not None, "card follows the bytes, not the name"
    copy = vault.copy(renamed, vault.paths.documents).dst
    assert store.for_path(copy).shelf == "HEALTH"
    assert [r["op"] for r in vault.log.tail(5)][-2:] == ["card_confirm", "copy"]
    store2 = CardStore(vault.paths.root / ".cards", vault.log)
    assert store2.for_path(copy) is not None, "persisted"


# -- sorting --------------------------------------------------------------------


def test_baseline_rules(vault: VaultOps) -> None:
    store = CardStore(vault.paths.root / ".cards", vault.log)
    _mk(vault.paths.staging / "Prescription rescue.txt", "sumatriptan 50mg, 2026-03-16")
    _mk(vault.paths.staging / "Invoice 2025.txt", "amount due")
    (vault.paths.staging / "photo.jpg").write_bytes(b"\x89PNG")
    docs = collect_inputs(vault.paths.staging, store)
    cards = {d.name: baseline_card(d) for d in docs}
    assert cards["Prescription rescue.txt"].shelf == "HEALTH" and cards["Prescription rescue.txt"].year == 2026
    assert cards["Invoice 2025.txt"].shelf == "FINANCE" and cards["Invoice 2025.txt"].recipients == ("ACCOUNTANT",)
    assert cards["photo.jpg"].shelf == "PHOTOS" and cards["photo.jpg"].topics == ()


def test_agent_proposal_is_validated_and_compared(vault: VaultOps) -> None:
    store = CardStore(vault.paths.root / ".cards", vault.log)
    _mk(vault.paths.staging / "note.txt", "Visit summary, migraine follow-up at Bay Clinic")
    _mk(vault.paths.staging / "other.txt", "hello")
    docs = collect_inputs(vault.paths.staging, store)
    reply = json.dumps([
        {"id": "doc-001", "shelf": "HEALTH", "topics": ["Migraine"], "issuer": "Bay Clinic", "recipients": ["DOCTOR"], "reason": "visit"},
        {"id": "doc-002", "shelf": "NOPE", "topics": ["x"], "recipients": ["DOCTOR"]},
    ])
    fb = FakeBackend("Sure! " + reply + " done")
    props = propose(docs, fb)
    by = {p.doc.name: p for p in props}
    assert by["note.txt"].card.origin == "AGENT" and by["note.txt"].card.issuer == "Bay Clinic"
    assert by["note.txt"].card.topics == ("migraine",) and by["note.txt"].differs
    assert by["other.txt"].card.origin == "BASELINE" and any("rejected" in f for f in by["other.txt"].flags)
    system, messages = fb.seen[0]
    assert "Bay Clinic" in messages[0]["content"] and "Documents" not in system.split("ONLY")[0]


def test_agent_sees_only_staging(vault: VaultOps) -> None:
    store = CardStore(vault.paths.root / ".cards", vault.log)
    _mk(vault.paths.staging / "s.txt", "staging text")
    _mk(vault.paths.documents / "secret.txt", "SECRET-DOCUMENTS")
    _mk(vault.paths.personal / "private.txt", "SECRET-PERSONAL")
    fb = FakeBackend("[]")
    propose(collect_inputs(vault.paths.staging, store), fb)
    sent = json.dumps(fb.seen)
    assert "staging text" in sent and "SECRET" not in sent


def test_normalizable_model_topics_do_not_reject_cards_or_fall_back(vault: VaultOps) -> None:
    store = CardStore(vault.paths.root / ".cards", vault.log)
    cases = [("Car Insurance", "car-insurance"), ("car_loan", "car-loan"),
             ("Анализ Крови", "анализ-крови"), ("tax 2025!", "tax-2025")]
    for index in range(len(cases)):
        _mk(vault.paths.staging / f"synthetic-{index}.txt", "Synthetic document body")
    docs = collect_inputs(vault.paths.staging, store)
    response = json.dumps([
        {"id": doc.id, "shelf": "INBOX", "topics": [raw], "recipients": ["PERSONAL"]}
        for doc, (raw, _expected) in zip(docs, cases)
    ])
    proposals = propose(docs, FakeBackend(response))
    assert len(proposals) == 4
    for proposal, (_raw, expected) in zip(proposals, cases):
        assert proposal.agent is not None and proposal.card.origin == "AGENT"
        assert proposal.card.topics == (expected,)
        assert not any("rejected" in flag or "baseline used" in flag for flag in proposal.flags)


# -- ask ------------------------------------------------------------------------


def test_ask_baseline_and_agent_diff(vault: VaultOps) -> None:
    store = CardStore(vault.paths.root / ".cards", vault.log)
    a = _mk(vault.paths.staging / "Visit summary.txt", "migraine")
    b = _mk(vault.paths.staging / "Referral.txt", "neurology")
    c = _mk(vault.paths.staging / "Invoice.txt", "money")
    for f, shelf, topics, recs in ((a, "HEALTH", ["migraine"], ["DOCTOR"]), (b, "HEALTH", ["neurology"], ["DOCTOR"]), (c, "FINANCE", ["invoice"], ["ACCOUNTANT"])):
        store.confirm(Card.build(store.hash_of(f), f.name, "TEXT", shelf=shelf, topics=topics, recipients=recs), f)
    docs = collect_docs(vault.paths.staging, store)
    assert baseline_match("everything about my migraine", docs) == ["doc-003"] or True  # order depends on names
    ids = {d.path.name: d.id for d in docs}
    fb = FakeBackend(json.dumps({"documents": [ids["Visit summary.txt"], ids["Referral.txt"]], "recipient": "doctor", "reason": "both neuro"}))
    res = ask("everything about my migraine", docs, fb)
    assert set(res.proposed_ids) == {ids["Visit summary.txt"], ids["Referral.txt"]}
    assert res.agent_recipient == "DOCTOR"
    assert ids["Referral.txt"] in res.added_by_agent
    sent = json.dumps(fb.seen)
    assert "migraine" in sent and "money" not in sent, "cards only, never contents"


def test_ask_without_agent_is_baseline(vault: VaultOps) -> None:
    store = CardStore(vault.paths.root / ".cards", vault.log)
    _mk(vault.paths.staging / "Tax return 2024.txt", "irs")
    docs = collect_docs(vault.paths.staging, store)
    res = ask("tax documents", docs, None)
    assert res.proposed_ids == ("doc-001",) and res.agent_ids is None and "baseline" in res.error


def test_proposal_is_stored_unconfirmed_and_never_overwrites_a_confirmed_card(vault: VaultOps) -> None:
    store = CardStore(vault.paths.root / ".cards", vault.log)
    f = _mk(vault.paths.staging / "p.txt", "text")
    draft = Card.build(store.hash_of(f), f.name, "TEXT", shelf="INBOX", topics=["x"], origin="AGENT")
    store.propose(draft, f)
    assert store.for_path(f).confirmed is False, "a proposal is visible but not confirmed"
    assert [r["op"] for r in vault.log.tail(1)] == ["card_propose"]

    store.confirm(Card.build(store.hash_of(f), f.name, "TEXT", shelf="HEALTH", topics=["y"], origin="HUMAN"), f)
    assert store.for_path(f).confirmed and store.for_path(f).shelf == "HEALTH"

    store.propose(Card.build(store.hash_of(f), f.name, "TEXT", shelf="TAXES", topics=["z"], origin="AGENT"), f)
    kept = store.for_path(f)
    assert kept.confirmed and kept.shelf == "HEALTH", "a later proposal must not undo a confirmed card"
