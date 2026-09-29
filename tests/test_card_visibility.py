"""Card reads and cleanup share the owner-visible document boundary."""
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from vault_v2.cards import CardError, CardStore
from vault_v2.errors import VaultError
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths


@pytest.fixture
def vault(tmp_path):
    ops = VaultOps(VaultPaths(tmp_path / "synthetic-vault"))
    store = CardStore(ops.paths.root / ".cards", ops.log)
    source = ops.paths.staging / "ordinary.txt"
    source.write_text("synthetic visible document", encoding="utf-8")
    return ops, store, source


@pytest.mark.parametrize("lookup", ["hash_of", "for_path"])
def test_cached_card_lookup_rechecks_ancestors_before_following_stat(vault, monkeypatch, lookup):
    ops, store, source = vault
    store.set_shelf(source, "HEALTH")
    original_lstat, original_stat = Path.lstat, Path.stat

    def reparse(path, *args, **kwargs):
        if path == ops.paths.root.parent:
            return SimpleNamespace(st_mode=stat.S_IFDIR,
                                   st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return original_lstat(path, *args, **kwargs)

    def stat_before_validation(path, *args, **kwargs):
        if path == source and kwargs.get("follow_symlinks", True):
            pytest.fail("card lookup followed the unvalidated file")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", reparse)
    monkeypatch.setattr(Path, "stat", stat_before_validation)
    with pytest.raises(VaultError, match="[Ll]inked"):
        getattr(store, lookup)(source)


@pytest.mark.parametrize("component", ["case.TRASH.JSON", ".hidden"])
def test_hidden_ancestor_shelf_assignment_refuses_without_writes(vault, component):
    ops, store, source = vault
    target = source.parent / component / "letter.txt"
    target.parent.mkdir()
    target.write_text("synthetic hidden document", encoding="utf-8")
    with pytest.raises(CardError):
        store.set_shelf(target, "HEALTH")
    assert store.all() == [] and store.generation == 0
    assert not store.file.exists() and not ops.log.file.exists()


def test_cleanup_with_hidden_file_retains_every_card_and_receipt(vault):
    ops, store, source = vault
    card = store.set_shelf(source, "HEALTH")
    source.rename(source.with_name(".private.txt"))
    rows, generation = ops.log.tail(100), store.generation
    with pytest.raises(VaultError):
        store.forget_missing([ops.paths.staging])
    assert store.get(card.sha256) == card and store.generation == generation
    assert ops.log.tail(100) == rows


def test_relative_configured_root_keeps_public_card_lookup_working(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ops = VaultOps(VaultPaths(Path("synthetic-relative")))
    store = CardStore(ops.paths.root / ".cards", ops.log)
    source = ops.paths.staging / "ordinary.txt"
    source.write_text("synthetic relative path", encoding="utf-8")
    card = store.set_shelf(source, "HEALTH")
    assert store.for_path(source) == card
    assert store.hash_of(source.absolute()) == card.sha256
    assert store.forget_missing([ops.paths.staging]) == 0


@pytest.mark.parametrize("blocked", [".private/letter.txt", "manifest.TRASH.JSON/letter.txt", "hidden.TRASH.JSON"])
@pytest.mark.parametrize("lookup", ["hash_of", "for_path"])
def test_hidden_card_paths_never_open_file_bytes(vault, monkeypatch, blocked, lookup):
    ops, store, _source = vault
    target = ops.paths.staging / blocked
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("synthetic hidden content", encoding="utf-8")
    original = Path.open
    def guarded_open(path, *args, **kwargs):
        if path == target:
            pytest.fail("hidden document bytes were opened")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", guarded_open)
    with pytest.raises(VaultError):
        getattr(store, lookup)(target)


@pytest.mark.parametrize("uncertainty", ["denied", "reparse"])
def test_cleanup_uncertainty_refuses_before_any_card_mutation(vault, monkeypatch, uncertainty):
    ops, store, source = vault
    card = store.set_shelf(source, "HEALTH")
    source.unlink()
    folder = ops.paths.staging / "unavailable"
    folder.mkdir()
    original = Path.lstat
    def unavailable(path, *args, **kwargs):
        if path == folder:
            if uncertainty == "denied":
                raise PermissionError("synthetic access denied")
            return SimpleNamespace(st_mode=stat.S_IFDIR,
                                   st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", unavailable)
    before, generation = ops.log.tail(100), store.generation
    with pytest.raises(VaultError, match="cleanup refused"):
        store.forget_missing([ops.paths.staging])
    assert store.get(card.sha256) == card and store.generation == generation
    assert ops.log.tail(100) == before
