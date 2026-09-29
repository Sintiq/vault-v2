"""Proposal batches observe store changes through the public store boundary."""

from pathlib import Path
from threading import Event, Thread

import pytest

from vault_v2.cards import Card, CardStore
from vault_v2.health import Entry, HealthStore
from vault_v2.receipts import ReceiptLog
from vault_v2.tasks import Task, TaskStore


def card() -> Card:
    return Card.build("a" * 64, "synthetic.txt", "TEXT", shelf="INBOX", topics=["test"])


def task() -> Task:
    return Task("task-1", "a" * 64, "synthetic.txt", "Return form", None, "Return form", "BASELINE")


def entry() -> Entry:
    return Entry("entry-1", "a" * 64, "synthetic.txt", None, "VISIT", "Visit", "Visit", "BASELINE")


def test_card_generation_tracks_proposals_confirmations_and_forgetting(tmp_path: Path) -> None:
    log = ReceiptLog(tmp_path / "receipts")
    store = CardStore(tmp_path / "cards", log)
    proposal = card()
    assert store.generation == 0

    store.propose(proposal)
    assert store.generation == 1
    confirmed = store.confirm(proposal)
    assert store.generation == 2
    assert store.propose(proposal) == confirmed
    assert store.generation == 3, "a new publication invalidates even a confirmed proposal"

    store.forget("missing")
    assert store.generation == 3
    store.forget(proposal.sha256)
    assert store.generation == 4 and store.all() == []

    store.propose(proposal)
    assert store.forget_missing([]) == 1
    assert store.generation == 6 and store.all() == []
    assert store.forget_missing([]) == 0
    assert store.generation == 6

    reloaded = CardStore(tmp_path / "cards", log)
    assert reloaded.generation == 0, "generation is local to this store instance"


def test_task_generation_tracks_add_done_reopen_and_remove(tmp_path: Path) -> None:
    log = ReceiptLog(tmp_path / "receipts")
    store = TaskStore(tmp_path / "tasks", log)
    proposal = task()
    assert store.generation == 0
    store.add(proposal)
    assert store.generation == 1
    store.add(proposal)
    assert store.generation == 2, "an overwrite is still a mutation"
    store.set_done(proposal.id, True)
    assert store.generation == 3 and store.all()[0].done

    store.set_done(proposal.id, True)
    store.set_done("missing", True)
    store.remove("missing")
    assert store.generation == 3

    store.set_done(proposal.id, False)
    assert store.generation == 4 and not store.all()[0].done
    reloaded = TaskStore(tmp_path / "tasks", log)
    assert reloaded.generation == 0 and reloaded.has(proposal.id)
    store.remove(proposal.id)
    assert store.generation == 5 and store.all() == []


def test_health_generation_tracks_add_and_remove(tmp_path: Path) -> None:
    log = ReceiptLog(tmp_path / "receipts")
    store = HealthStore(tmp_path / "health", log)
    proposal = entry()
    assert store.generation == 0
    store.add(proposal)
    assert store.generation == 1
    store.add(proposal)
    assert store.generation == 2
    store.remove("missing")
    assert store.generation == 2

    reloaded = HealthStore(tmp_path / "health", log)
    assert reloaded.generation == 0 and reloaded.has(proposal.id)
    store.remove(proposal.id)
    assert store.generation == 3 and store.all() == []


@pytest.mark.parametrize("family,operation", [
    ("cards", "propose"), ("cards", "confirm"), ("cards", "forget"), ("cards", "forget_missing"),
    ("tasks", "add"), ("tasks", "set_done"), ("tasks", "remove"),
    ("health", "add"), ("health", "remove"),
])
def test_store_mutation_waits_for_batch_guard(tmp_path: Path, family: str, operation: str) -> None:
    log = ReceiptLog(tmp_path / "receipts")
    if family == "cards":
        store = CardStore(tmp_path / "cards", log)
        proposal = card()
        store.propose(proposal)
        actions = {
            "propose": lambda: store.propose(proposal),
            "confirm": lambda: store.confirm(proposal),
            "forget": lambda: store.forget(proposal.sha256),
            "forget_missing": lambda: store.forget_missing([]),
        }
    elif family == "tasks":
        store = TaskStore(tmp_path / "tasks", log)
        proposal = task()
        store.add(proposal)
        actions = {
            "add": lambda: store.add(proposal),
            "set_done": lambda: store.set_done(proposal.id, True),
            "remove": lambda: store.remove(proposal.id),
        }
    else:
        store = HealthStore(tmp_path / "health", log)
        proposal = entry()
        store.add(proposal)
        actions = {
            "add": lambda: store.add(proposal),
            "remove": lambda: store.remove(proposal.id),
        }
    started, finished = Event(), Event()
    errors = []

    def mutate() -> None:
        started.set()
        try:
            actions[operation]()
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    worker = Thread(target=mutate, daemon=True)
    with store.log.write("batch"), store.mutation_lock:
        generation = store.generation
        worker.start()
        assert started.wait(2), "mutation worker did not start"
        assert not finished.wait(0.1), "mutation completed inside another caller's guard"
        assert store.generation == generation
    worker.join(2)
    assert not worker.is_alive(), "mutation remained blocked after the guard was released"
    assert errors == []
    assert store.generation == generation + 1


@pytest.mark.parametrize("store_type,make_proposal,operation", [
    (CardStore, card, "propose"),
    (CardStore, card, "confirm"),
    (TaskStore, task, "add"),
    (HealthStore, entry, "add"),
])
def test_failed_file_write_still_invalidates_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store_type, make_proposal, operation: str,
) -> None:
    store = store_type(tmp_path / "store", ReceiptLog(tmp_path / "receipts"))
    proposal = make_proposal()
    write_text = Path.write_text

    def fail_store_write(path: Path, *args, **kwargs):
        if path.parent == store.dir:
            raise OSError("synthetic store write failure")
        return write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_store_write)
    with pytest.raises(OSError, match="may have applied"):
        getattr(store, operation)(proposal)

    assert store.generation == 1, "failed persistence must not leave the old batch current"
    assert store.all() == [], "memory must reflect the absent persisted file after failure"


@pytest.mark.parametrize("store_type,make_proposal,operation", [
    (CardStore, card, "confirm"),
    (TaskStore, task, "add"),
    (HealthStore, entry, "add"),
])
def test_batch_guard_allows_its_owner_to_mutate(
    tmp_path: Path, store_type, make_proposal, operation: str,
) -> None:
    store = store_type(tmp_path / "store", ReceiptLog(tmp_path / "receipts"))
    errors = []

    def guarded_mutation() -> None:
        try:
            with store.log.write("batch"), store.mutation_lock:
                getattr(store, operation)(make_proposal())
        except Exception as exc:
            errors.append(exc)

    worker = Thread(target=guarded_mutation, daemon=True)
    worker.start()
    worker.join(2)
    assert not worker.is_alive(), "a batch owner must be able to reenter its store guard"
    assert errors == []
    assert store.generation == 1 and len(store.all()) == 1
