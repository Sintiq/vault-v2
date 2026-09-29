"""Root write admission covers public stores, shelves, and reminder state."""

from datetime import date
import json
from pathlib import Path
from threading import Thread
import urllib.request

import pytest

from vault_v2.cards import Card, CardStore, load_shelves
from vault_v2.health import Entry, HealthStore
from vault_v2.ops import VaultError
from vault_v2.paths import VaultPaths
from vault_v2.receipts import ReceiptLog
from vault_v2.reminders import RemindedLog, remind
from vault_v2.shelves import add_shelf, custom_shelves, remove_shelf
from vault_v2.tasks import Task, TaskStore


def example_card():
    return Card.build("a" * 64, "synthetic.txt", "TEXT", shelf="INBOX", topics=["example"])


def example_task():
    return Task("task-1", "a" * 64, "synthetic.txt", "Renew example", "2026-10-01", "Renew example by 2026-10-01", "BASELINE")


def example_entry():
    return Entry("entry-1", "a" * 64, "synthetic.txt", "2026-10-01", "VISIT", "Example visit", "Example visit", "BASELINE")


@pytest.fixture(autouse=True)
def reset_shelves():
    yield
    load_shelves({})


@pytest.mark.parametrize("family", ["cards", "tasks", "health"])
def test_corrupt_receipts_refuse_store_write_without_changing_memory_or_disk(tmp_path, family):
    log = ReceiptLog(tmp_path / ".receipts")
    if family == "cards":
        store = CardStore(tmp_path / ".cards", log)
        write = lambda: store.confirm(example_card())
    elif family == "tasks":
        store = TaskStore(tmp_path / ".tasks", log)
        write = lambda: store.add(example_task())
    else:
        store = HealthStore(tmp_path / ".health", log)
        write = lambda: store.add(example_entry())
    log.append("seed", "", "")
    log.file.write_text("not a receipt\n", encoding="utf-8")

    with pytest.raises((ValueError, VaultError)):
        write()

    assert store.all() == []
    assert not store.file.exists()
    assert store.generation == 0


@pytest.mark.parametrize("store_type,make_item,operation", [
    (CardStore, example_card, "propose"),
    (CardStore, example_card, "confirm"),
    (TaskStore, example_task, "add"),
    (HealthStore, example_entry, "add"),
])
def test_failed_first_save_clears_unpersisted_memory_and_blocks_more_writes(
    tmp_path, monkeypatch, store_type, make_item, operation,
):
    log = ReceiptLog(tmp_path / ".receipts")
    store = store_type(tmp_path / "store", log)
    replace = Path.replace

    def fail_replace(path, target):
        if Path(target) == store.file:
            raise OSError("synthetic replace failure")
        return replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(VaultError, match="may have applied"):
        getattr(store, operation)(make_item())

    assert store.all() == []
    assert not store.file.exists()
    assert store.generation == 1
    with pytest.raises(VaultError):
        getattr(store, operation)(make_item())
    assert store.generation == 1


@pytest.mark.parametrize("family,operation", [
    ("cards", "propose"), ("cards", "forget"), ("cards", "forget_missing"),
    ("tasks", "set_done"), ("tasks", "remove"), ("health", "remove"),
])
def test_corrupt_receipts_block_existing_record_changes(tmp_path, family, operation):
    log = ReceiptLog(tmp_path / ".receipts")
    if family == "cards":
        store = CardStore(tmp_path / ".cards", log)
        item = example_card()
        store.confirm(item)
        changes = {"propose": lambda: store.propose(item), "forget": lambda: store.forget(item.sha256),
                   "forget_missing": lambda: store.forget_missing([])}
    elif family == "tasks":
        store = TaskStore(tmp_path / ".tasks", log)
        item = example_task()
        store.add(item)
        changes = {"set_done": lambda: store.set_done(item.id, True), "remove": lambda: store.remove(item.id)}
    else:
        store = HealthStore(tmp_path / ".health", log)
        item = example_entry()
        store.add(item)
        changes = {"remove": lambda: store.remove(item.id)}
    before, records, generation = store.file.read_bytes(), store.all(), store.generation
    log.file.write_text("not a receipt\n", encoding="utf-8")

    with pytest.raises(VaultError):
        changes[operation]()

    assert store.file.read_bytes() == before
    assert store.all() == records
    assert store.generation == generation


@pytest.mark.parametrize("operation", ["add", "remove"])
def test_corrupt_receipts_refuse_shelf_changes_before_settings_or_cards(tmp_path, operation):
    paths = VaultPaths(tmp_path)
    log = ReceiptLog(paths.receipts)
    store = CardStore(tmp_path / ".cards", log)
    add_shelf(paths, "EXAMPLE")
    item = Card.build("a" * 64, "synthetic.txt", "TEXT", shelf="EXAMPLE", topics=["example"])
    store.confirm(item)
    settings_before, cards_before = paths.settings_file.read_bytes(), store.file.read_bytes()
    log.file.write_text("not a receipt\n", encoding="utf-8")

    with pytest.raises(VaultError):
        if operation == "add":
            add_shelf(paths, "ANOTHER")
        else:
            remove_shelf(paths, store, "EXAMPLE")

    assert paths.settings_file.read_bytes() == settings_before
    assert store.file.read_bytes() == cards_before
    assert custom_shelves(paths) == ["EXAMPLE"]
    assert store.all()[0].shelf == "EXAMPLE"


def test_corrupt_receipts_refuse_reminder_mark_without_changing_memory_or_disk(tmp_path):
    tasks_dir = tmp_path / ".tasks"
    log = ReceiptLog(tmp_path / ".receipts")
    log.append("seed", "", "")
    seen = RemindedLog(tasks_dir)
    log.file.write_text("not a receipt\n", encoding="utf-8")

    with pytest.raises(VaultError):
        seen.mark("task-1", date(2026, 10, 1))

    assert not seen.already("task-1", date(2026, 10, 1))
    assert not seen.file.exists()


def test_reminder_invalid_journal_does_not_contact_phone(tmp_path, monkeypatch):
    log = ReceiptLog(tmp_path / ".receipts")
    store = TaskStore(tmp_path / ".tasks", log)
    store.add(example_task())
    door = tmp_path / ".door"
    door.mkdir()
    (door / "config.json").write_text(json.dumps({"address": "http://synthetic.invalid", "key": "fake"}))
    log.file.write_text("not a receipt\n", encoding="utf-8")

    def no_network(*args, **kwargs):
        pytest.fail("invalid journal must be noticed before any phone request")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", no_network)
    with pytest.raises(VaultError):
        remind(tmp_path, store, log, date(2026, 10, 1))


@pytest.mark.parametrize("store_type,make_item,operation", [
    (CardStore, example_card, "propose"),
    (CardStore, example_card, "confirm"),
    (TaskStore, example_task, "add"),
    (HealthStore, example_entry, "add"),
])
def test_receipt_failure_after_store_save_keeps_disk_state_visible_and_blocks_writes(
    tmp_path, monkeypatch, store_type, make_item, operation,
):
    log = ReceiptLog(tmp_path / ".receipts")
    store = store_type(tmp_path / "store", log)
    original_open = Path.open

    def fail_receipt_open(path, mode="r", *args, **kwargs):
        if path == log.file and mode == "a":
            raise OSError("synthetic receipt failure")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_receipt_open)
    with pytest.raises(VaultError, match="may have applied"):
        getattr(store, operation)(make_item())

    assert len(store.all()) == 1
    assert store.all() == store_type(store.dir, log).all()
    before = store.file.read_bytes()
    with pytest.raises(VaultError, match="blocked until restart"):
        getattr(store, operation)(make_item())
    assert store.file.read_bytes() == before


@pytest.mark.parametrize("family", ["cards", "tasks", "health", "reminded", "shelves"])
def test_readonly_public_writes_create_no_directories(tmp_path, family):
    root = tmp_path / "uncreated"
    log = ReceiptLog(root / ".receipts", read_only=True)
    if family == "cards":
        store = CardStore(root / ".cards", log)
        write = lambda: store.confirm(example_card())
    elif family == "tasks":
        store = TaskStore(root / ".tasks", log)
        write = lambda: store.add(example_task())
    elif family == "health":
        store = HealthStore(root / ".health", log)
        write = lambda: store.add(example_entry())
    elif family == "reminded":
        seen = RemindedLog(root / ".tasks", log)
        write = lambda: seen.mark("task-1", date(2026, 10, 1))
    else:
        write = lambda: add_shelf(VaultPaths(root), "EXAMPLE", log)
    with pytest.raises(VaultError, match="read-only"):
        write()
    assert not root.exists()


def test_phone_wait_holds_no_guard_and_reminder_write_keeps_other_thread_changes(tmp_path, monkeypatch):
    log = ReceiptLog(tmp_path / ".receipts")
    store = TaskStore(tmp_path / ".tasks", log)
    store.add(example_task())
    door = tmp_path / ".door"
    door.mkdir()
    (door / "config.json").write_text(json.dumps({"address": "http://100.64.0.1:8779", "key": "fake"}))
    today = date(2026, 10, 1)
    errors = []

    class Reply:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return b'{"shown": true}'

    def fake_phone(*args, **kwargs):
        def concurrent_mark():
            try:
                RemindedLog(tmp_path / ".tasks", log).mark("other-task", today)
            except Exception as exc:
                errors.append(exc)

        worker = Thread(target=concurrent_mark, daemon=True)
        worker.start()
        worker.join(2)
        assert not worker.is_alive(), "phone wait held the root write guard"
        assert errors == []
        return Reply()

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", fake_phone)
    assert remind(tmp_path, store, log, today) == "reminders: 1 sent to the phone"
    seen = RemindedLog(tmp_path / ".tasks", log)
    assert seen.already("task-1", today)
    assert seen.already("other-task", today)
    assert log.tail(1)[0]["op"] == "remind"
    assert log.verify() == 4


@pytest.mark.parametrize("failure_point", ["save", "receipt"])
def test_reminder_failure_reloads_persisted_state_and_blocks_retry(tmp_path, monkeypatch, failure_point):
    log = ReceiptLog(tmp_path / ".receipts")
    seen = RemindedLog(tmp_path / ".tasks", log)
    today = date(2026, 10, 1)
    seen.mark("earlier", today)
    original_open = Path.open

    def fail_write(path, mode="r", *args, **kwargs):
        target = seen.file.with_suffix(".json.tmp") if failure_point == "save" else log.file
        if path == target and mode in ("a", "w"):
            raise OSError("synthetic reminder persistence failure")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_write)
    with pytest.raises(VaultError, match="may have applied"):
        seen.mark("later", today)
    assert seen.already("earlier", today)
    assert seen.already("later", today) is (failure_point == "receipt")
    reloaded = RemindedLog(tmp_path / ".tasks", log)
    assert reloaded.already("later", today) == seen.already("later", today)
    with pytest.raises(VaultError, match="blocked until restart"):
        seen.mark("another", today)


@pytest.mark.parametrize("family", ["cards", "tasks", "health"])
def test_failed_update_restores_existing_persisted_records_in_memory(tmp_path, monkeypatch, family):
    log = ReceiptLog(tmp_path / ".receipts")
    if family == "cards":
        store = CardStore(tmp_path / "store", log)
        store.confirm(example_card())
        write = lambda: store.forget(example_card().sha256)
    elif family == "tasks":
        store = TaskStore(tmp_path / "store", log)
        store.add(example_task())
        write = lambda: store.set_done(example_task().id, True)
    else:
        store = HealthStore(tmp_path / "store", log)
        store.add(example_entry())
        write = lambda: store.remove(example_entry().id)
    records, saved = store.all(), store.file.read_bytes()
    original_replace = Path.replace

    def fail_replace(path, target):
        if Path(target) == store.file:
            raise OSError("synthetic save failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(VaultError, match="may have applied"):
        write()
    assert store.file.read_bytes() == saved
    assert store.all() == records
    assert store.generation == 2


def test_shelf_remove_settings_failure_preserves_visible_partial_results(tmp_path, monkeypatch):
    paths = VaultPaths(tmp_path)
    log = ReceiptLog(paths.receipts)
    store = CardStore(tmp_path / ".cards", log)
    add_shelf(paths, "EXAMPLE", log)
    item = Card.build("a" * 64, "synthetic.txt", "TEXT", shelf="EXAMPLE", topics=["example"])
    store.confirm(item)
    original_replace = Path.replace

    def fail_settings_replace(path, target):
        if Path(target) == paths.settings_file:
            raise OSError("synthetic settings failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_settings_replace)
    with pytest.raises(VaultError, match="may have applied"):
        remove_shelf(paths, store, "EXAMPLE")
    assert custom_shelves(paths) == ["EXAMPLE"]
    assert store.all()[0].shelf == "INBOX"
    assert store.all() == CardStore(store.dir, log).all()
    assert log.tail(1)[0]["op"] == "card_confirm"
    with pytest.raises(VaultError, match="blocked until restart"):
        remove_shelf(paths, store, "EXAMPLE")
