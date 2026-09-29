"""Manual shelf assignment changes only the card, under the root guard."""
import stat
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from vault_v2 import cards
from vault_v2.agent_api import AgentAPI
from vault_v2.cards import Card, CardError, CardStore
from vault_v2.errors import VaultBusy, VaultError
from vault_v2.health import HealthStore
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.proposals import ProposalConflict
from vault_v2.reader import StagingReader
from vault_v2.tasks import TaskStore


@pytest.fixture(autouse=True)
def reset_shelves():
    cards.load_shelves({"shelves": ["IMMIGRATION"]})
    yield
    cards.load_shelves({})


@pytest.fixture
def vault(tmp_path):
    ops = VaultOps(VaultPaths(tmp_path / "synthetic-vault"))
    store = CardStore(ops.paths.root / ".cards", ops.log)
    source = ops.paths.personal / "letter.txt"
    source.write_text("Synthetic personal letter", encoding="utf-8")
    return ops, store, source


def test_missing_card_gets_only_owner_shelf_and_receipt(vault):
    ops, store, source = vault
    before = source.read_bytes()
    card = store.set_shelf(source, "IMMIGRATION")
    assert card.name == source.name and card.kind == "TEXT"
    assert card.shelf == "IMMIGRATION" and card.origin == "HUMAN" and card.confirmed
    assert card.topics == card.recipients == ()
    assert card.year is None and card.issuer == "UNCONFIRMED"
    assert source.read_bytes() == before
    assert store.generation == 1
    assert CardStore(store.dir, ops.log).for_path(source) == card
    receipt = ops.log.tail(1)[0]
    assert receipt["op"] == "card_confirm" and receipt["extra"]["origin"] == "HUMAN"
    assert ops.log.verify() == 1


def test_existing_card_keeps_other_metadata_and_increments_generation_once(vault):
    ops, store, source = vault
    old = Card.build(store.hash_of(source), source.name, "TEXT", shelf="HEALTH",
                     topics=["appointment"], issuer="Synthetic Clinic", year=2026,
                     recipients=["DOCTOR"], origin="AGENT", reason="original proposal")
    store.propose(old, source)
    card = store.set_shelf(source, "IMMIGRATION")
    assert card.topics == old.topics and card.issuer == old.issuer
    assert card.year == old.year and card.recipients == old.recipients and card.reason == old.reason
    assert card.origin == "HUMAN" and card.confirmed and store.generation == 2
    assert ops.log.tail(1)[0]["op"] == "card_confirm"


@pytest.mark.parametrize("bad", ["outside", "hidden", "service", "directory", "missing", "parent"])
def test_invalid_paths_do_not_create_cards_or_receipts(vault, bad):
    ops, store, source = vault
    if bad == "outside":
        target = ops.paths.root.parent / "outside.txt"
        target.write_text("synthetic outside", encoding="utf-8")
    elif bad == "hidden":
        target = ops.paths.staging / ".hidden" / "file.txt"
        target.parent.mkdir()
        target.write_text("synthetic hidden", encoding="utf-8")
    elif bad == "service":
        target = ops.paths.staging / "file.trash.json"
        target.write_text("{}", encoding="utf-8")
    elif bad == "directory":
        target = ops.paths.staging
    elif bad == "missing":
        target = ops.paths.staging / "missing.txt"
    else:
        target = ops.paths.staging / ".." / "personal" / source.name
    with pytest.raises((CardError, OSError)):
        store.set_shelf(target, "HEALTH")
    assert not store.file.exists() and not ops.log.file.exists() and store.generation == 0


def test_reparse_component_refused_before_hashing(vault, monkeypatch):
    ops, store, source = vault
    original = Path.lstat
    def reparse(path, *args, **kwargs):
        if path == source.parent:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", reparse)
    monkeypatch.setattr(store, "hash_of", lambda _path: pytest.fail("linked path must not be read"))
    with pytest.raises(CardError, match="linked"):
        store.set_shelf(source, "HEALTH")
    assert store.generation == 0 and not store.file.exists()


@pytest.mark.parametrize("mode", ["readonly", "journal", "unknown_shelf"])
def test_pre_effect_refusals_preserve_existing_card(vault, mode):
    ops, store, source = vault
    store.set_shelf(source, "HEALTH")
    before, rows = store.file.read_bytes(), store.all()
    shelf = "IMMIGRATION"
    if mode == "readonly":
        ops.log.read_only = True
    elif mode == "journal":
        ops.log.file.write_text("not a receipt", encoding="utf-8")
    else:
        shelf = "NOT_A_SHELF"
    with pytest.raises((CardError, VaultError)):
        store.set_shelf(source, shelf)
    assert store.file.read_bytes() == before and store.all() == rows and store.generation == 1


def test_busy_guard_refuses_before_card_mutation(vault):
    ops, store, source = vault
    entered, release = Event(), Event()
    def hold_guard():
        with ops.log.write("synthetic concurrent write"):
            entered.set()
            release.wait(5)
    thread = Thread(target=hold_guard)
    thread.start()
    try:
        assert entered.wait(2)
        with ops.log.nonblocking(), pytest.raises(VaultBusy):
            store.set_shelf(source, "HEALTH")
        assert store.generation == 0 and not store.file.exists()
    finally:
        release.set()
        thread.join(2)


def test_manual_shelf_invalidates_published_sort_batch(vault):
    ops, store, source = vault
    staged = ops.paths.staging / "note.txt"
    staged.write_text("Synthetic staged letter", encoding="utf-8")
    agent = AgentAPI(ops.paths.staging, StagingReader(ops, store), store,
                     TaskStore(ops.paths.root / ".tasks", ops.log),
                     HealthStore(ops.paths.root / ".health", ops.log), lambda: None)
    batch = agent.sort_propose()
    store.set_shelf(staged, "IMMIGRATION")
    with pytest.raises(ProposalConflict):
        agent.sort_confirm([{"id": batch["proposals"][0]["id"]}])
    assert store.for_path(staged).shelf == "IMMIGRATION"


def test_receipt_failure_preserves_visible_effect_and_blocks_retry(vault, monkeypatch):
    ops, store, source = vault
    original = Path.open
    def fail(path, mode="r", *args, **kwargs):
        if path == ops.log.file and mode == "a":
            raise OSError("synthetic receipt failure")
        return original(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, "open", fail)
    with pytest.raises(VaultError, match="may have applied"):
        store.set_shelf(source, "IMMIGRATION")
    assert store.for_path(source).shelf == "IMMIGRATION" and store.generation == 1
    with pytest.raises(VaultError, match="blocked until restart"):
        store.set_shelf(source, "HEALTH")


def test_removing_custom_shelf_preserves_minimal_human_card(vault):
    from vault_v2.shelves import add_shelf, remove_shelf
    ops, store, source = vault
    add_shelf(ops.paths, "IMMIGRATION", ops.log)
    store.set_shelf(source, "IMMIGRATION")
    shelves, moved = remove_shelf(ops.paths, store, "IMMIGRATION")
    card = store.for_path(source)
    assert "IMMIGRATION" not in shelves and moved == 1
    assert card.shelf == "INBOX" and card.origin == "HUMAN" and card.confirmed
    assert card.topics == () and card.issuer == "UNCONFIRMED" and card.year is None
    assert store.generation == 2
    assert [row["op"] for row in ops.log.tail(2)] == ["card_confirm", "shelf_remove"]
