"""Whole-request admission through public document-job boundaries."""

import json
from pathlib import Path

import pytest

from vault_v2.agent import Backend, BackendInfo, document_chat
from vault_v2.ask import AskDoc, ask
from vault_v2.cards import Card, CardStore
from vault_v2.document_budget import DocumentBudgetExceeded
from vault_v2.health import HealthDoc, agent_entries
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.reader import StagingReader
from vault_v2.sorting import SortInput, agent_cards, propose as sort_propose
from vault_v2.tasks import TaskDoc, agent_tasks


ENGLISH = (
    "The clinic recorded a routine follow-up visit. The document lists the visit date, "
    "the examination notes, and the next appointment. This synthetic record belongs "
    "to no real person and contains no treatment recommendation. "
)
RUSSIAN = (
    "В клинике записан плановый повторный визит. Документ содержит дату посещения, "
    "описание осмотра и время следующей встречи. Эта вымышленная запись не относится "
    "к реальному человеку и не содержит рекомендаций по лечению. "
)


def prose(language: str, count: int = 6000) -> str:
    paragraph = {"en": ENGLISH, "ru": RUSSIAN, "mixed": ENGLISH + RUSSIAN}[language]
    return (paragraph * (count // len(paragraph) + 1))[:count]


class CountingBackend(Backend):
    def __init__(self, *, object_reply: bool = False):
        self.info = BackendInfo("fake", "fake", "fake")
        self.seen = []
        self.reply = ('{"documents": [], "recipient": "PERSONAL", "reason": "synthetic"}'
                      if object_reply else "[]")

    def chat(self, system, messages, on_chunk):
        self.seen.append((system, messages))
        return self.reply


def job_docs(route: str, text: str, count: int):
    docs = []
    for index in range(1, count + 1):
        identifier, name, digest = f"doc-{index:03d}", f"clinical-{index:03d}.txt", f"{index:064x}"
        if route.startswith("sort"):
            docs.append(SortInput(identifier, Path(name), name, "TEXT", len(text.encode("utf-8")),
                                  digest, text))
        elif route == "tasks":
            docs.append(TaskDoc(identifier, name, name, digest, text))
        else:
            docs.append(HealthDoc(identifier, name, digest, text))
    return docs


def run_job(route: str, backend: Backend, docs):
    if route == "sort-propose":
        return sort_propose(docs, backend)
    return {"sort": agent_cards, "tasks": agent_tasks, "health": agent_entries}[route](backend, docs)


@pytest.mark.parametrize("route", ["sort", "tasks", "health"])
@pytest.mark.parametrize("language", ["en", "ru", "mixed"])
def test_one_6000_character_document_is_admitted_whole(route, language):
    backend = CountingBackend()
    text = prose(language)

    run_job(route, backend, job_docs(route, text, 1))

    assert len(backend.seen) == 1
    assert text in backend.seen[0][1][0]["content"]


@pytest.mark.parametrize("route", ["sort", "sort-propose", "tasks", "health"])
def test_twenty_full_excerpts_are_refused_before_any_backend_call(route):
    backend = CountingBackend()
    docs = job_docs(route, prose("mixed"), 20)

    with pytest.raises(DocumentBudgetExceeded) as refusal:
        run_job(route, backend, docs)

    assert backend.seen == []
    assert refusal.value.documents == tuple(
        f"doc-{index:03d} — clinical-{index:03d}.txt" for index in range(1, 21))
    assert "clinical-020.txt" in str(refusal.value)


@pytest.mark.parametrize("route", ["sort", "tasks", "health"])
def test_document_metadata_counts_even_when_excerpts_are_empty(route):
    backend = CountingBackend()
    docs = job_docs(route, "", 500)

    with pytest.raises(DocumentBudgetExceeded) as refusal:
        run_job(route, backend, docs)

    assert backend.seen == []
    assert len(refusal.value.documents) == 500
    assert refusal.value.documents[-1] == "doc-500 — clinical-500.txt"


def test_system_message_is_part_of_whole_request_admission():
    backend = CountingBackend()

    with pytest.raises(DocumentBudgetExceeded) as refusal:
        document_chat(backend, prose("en", 40000), [{"role": "user", "content": "[]"}],
                      lambda _chunk: None, documents=("doc-001 — synthetic.txt",))

    assert backend.seen == []
    assert refusal.value.documents == ("doc-001 — synthetic.txt",)


def test_json_escaping_is_counted_in_the_serialized_request():
    backend = CountingBackend()
    # Control characters expand to six ASCII bytes in JSON. Counting the raw
    # 6,000-character message alone would overlook this transport payload.
    with pytest.raises(DocumentBudgetExceeded):
        document_chat(backend, "Synthetic document task", [
            {"role": "user", "content": "\x00" * 6000},
        ], lambda _chunk: None, documents=("doc-001 — synthetic.txt",))

    assert backend.seen == []


def test_ask_refuses_an_oversized_question_before_backend_call():
    backend = CountingBackend(object_reply=True)
    docs = [AskDoc("doc-001", Path("clinical.txt"), None)]

    with pytest.raises(DocumentBudgetExceeded) as refusal:
        ask(prose("ru", 40000), docs, backend)

    assert backend.seen == []
    assert refusal.value.documents == ("doc-001 — clinical.txt",)


def test_ask_refuses_oversized_card_metadata_without_any_excerpts():
    backend = CountingBackend(object_reply=True)
    docs = []
    for index in range(1, 151):
        name = f"clinical-{index:03d}.txt"
        card = Card.build(f"{index:064x}", name, "TEXT", shelf="HEALTH",
                          topics=[f"synthetic-topic-{item:02d}-" + "a" * 20 for item in range(8)],
                          issuer="Synthetic Clinic " + "R" * 100, confirmed=True)
        docs.append(AskDoc(f"doc-{index:03d}", Path(name), card))

    with pytest.raises(DocumentBudgetExceeded) as refusal:
        ask("clinical", docs, backend)

    assert backend.seen == []
    assert refusal.value.documents == tuple(
        f"doc-{index:03d} — clinical-{index:03d}.txt" for index in range(1, 151))


def test_ask_cached_excerpts_are_neither_sent_nor_counted_for_model_admission():
    backend = CountingBackend(object_reply=True)
    excerpt = ("PRIVATE_CACHED_MIGRAINE " + prose("ru"))[:6000]
    docs = [AskDoc(f"doc-{index:03d}", Path(f"scan-{index:03d}.pdf"), None,
                   excerpt=excerpt, total_chars=6000) for index in range(1, 21)]

    result = ask("migraine", docs, backend)

    assert result.baseline_ids == tuple(f"doc-{index:03d}" for index in range(1, 21))
    assert len(backend.seen) == 1
    assert "PRIVATE_CACHED_MIGRAINE" not in json.dumps(backend.seen, ensure_ascii=False)
    assert "scan-020.pdf" in backend.seen[0][1][0]["content"]


def synthetic_reader(tmp_path):
    ops = VaultOps(VaultPaths(tmp_path / "synthetic-vault"))
    cards = CardStore(ops.paths.root / ".cards", ops.log)
    for index in range(1, 21):
        (ops.paths.staging / f"clinical-{index:03d}.txt").write_text(prose("en"), encoding="utf-8")
    return StagingReader(ops, cards)


@pytest.mark.parametrize("workflow", ["tasks", "health"])
def test_propose_does_not_fall_back_to_baseline_after_budget_refusal(tmp_path, workflow):
    from vault_v2.health import propose as health_propose
    from vault_v2.tasks import propose as tasks_propose

    reader, backend = synthetic_reader(tmp_path), CountingBackend()
    propose = {"tasks": tasks_propose, "health": health_propose}[workflow]

    with pytest.raises(DocumentBudgetExceeded) as refusal:
        propose(reader, backend)

    assert backend.seen == []
    assert refusal.value.documents == tuple(
        f"doc-{index:03d} — clinical-{index:03d}.txt" for index in range(1, 21))


@pytest.mark.parametrize("workflow", ["sort", "tasks", "health", "ask"])
def test_explicit_no_backend_baseline_remains_available_for_large_requests(tmp_path, workflow):
    from vault_v2.health import propose as health_propose
    from vault_v2.tasks import propose as tasks_propose

    if workflow == "sort":
        proposals = sort_propose(job_docs("sort", prose("en"), 20), None)
        assert len(proposals) == 20
        assert all(proposal.card.origin == "BASELINE" for proposal in proposals)
    elif workflow == "ask":
        docs = [AskDoc(f"doc-{index:03d}", Path(f"clinical-{index:03d}.txt"), None)
                for index in range(1, 21)]
        result = ask("clinical " + prose("ru", 40000), docs, None)
        assert result.agent_ids is None
        assert len(result.proposed_ids) == 20
    else:
        reader = synthetic_reader(tmp_path)
        proposals, notes = {"tasks": tasks_propose, "health": health_propose}[workflow](reader, None)
        assert proposals and all(proposal.origin == "BASELINE" for proposal in proposals)
        assert "no agent — keyword baseline only" in notes
