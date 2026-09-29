"""Damaged trash remains visible and cannot authorize restore or deletion."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from vault_v2.ops import VaultError, VaultOps
from vault_v2.paths import VaultPaths


@pytest.fixture()
def vault(tmp_path: Path) -> VaultOps:
    return VaultOps(VaultPaths(tmp_path / "vault"))


def _trash(vault: VaultOps, name: str, body: str):
    source = vault.paths.documents / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(body, encoding="utf-8")
    return source, vault.trash(source).dst


def _manifest(slot: Path) -> Path:
    return slot.with_name(slot.name + ".trash.json")


def test_damaged_manifest_does_not_hide_healthy_restore(vault: VaultOps) -> None:
    good_origin, good = _trash(vault, "good.txt", "recover healthy")
    _, bad = _trash(vault, "bad.txt", "retain damaged")
    _manifest(bad).write_text("{broken JSON", encoding="utf-8")
    before = vault.log.tail()

    rows = {Path(row["slot"]): row for row in vault.list_trash()}

    assert set(rows) == {good, bad}
    assert Path(rows[good]["origin"]) == good_origin
    assert rows[bad]["error"].startswith("manifest damaged")
    assert "origin" not in rows[bad]
    assert vault.log.tail() == before
    assert vault.restore(good).dst == good_origin
    assert good_origin.read_text(encoding="utf-8") == "recover healthy"
    with pytest.raises(VaultError):
        vault.restore(bad)
    assert bad.read_text(encoding="utf-8") == "retain damaged"
    assert _manifest(bad).read_text(encoding="utf-8") == "{broken JSON"


@pytest.mark.parametrize("directory", [False, True])
def test_orphan_payload_is_visible_and_not_restorable(vault: VaultOps, directory: bool) -> None:
    orphan = vault.paths.trash / "unrecorded"
    if directory:
        orphan.mkdir()
        payload = orphan / "nested.txt"
    else:
        payload = orphan
    payload.write_bytes(b"retain orphan bytes")

    assert vault.list_trash() == [{"slot": str(orphan), "error": "no manifest"}]
    with pytest.raises(VaultError):
        vault.restore(orphan)
    assert payload.read_bytes() == b"retain orphan bytes"
    assert vault.log.tail() == []


def test_payload_named_like_manifest_round_trips(vault: VaultOps) -> None:
    origin, slot = _trash(vault, "notes.trash.json", "ordinary user document")
    rows = vault.list_trash()
    assert len(rows) == 1 and rows[0]["slot"] == str(slot)
    assert "error" not in rows[0]
    assert vault.restore(slot).dst == origin
    assert origin.read_text(encoding="utf-8") == "ordinary user document"


def test_unpaired_suffix_file_is_visible_by_its_physical_name(vault: VaultOps) -> None:
    orphan = vault.paths.trash / "notes.trash.json"
    orphan.write_bytes(b"ordinary user document, not a sidecar")
    assert vault.list_trash() == [{"slot": str(orphan), "error": "no manifest"}]
    with pytest.raises(VaultError):
        vault.restore(orphan)
    assert orphan.read_bytes() == b"ordinary user document, not a sidecar"


def test_purge_removes_only_healthy_and_reports_kept_damage(vault: VaultOps) -> None:
    _, good = _trash(vault, "good.txt", "remove healthy")
    _, bad = _trash(vault, "bad.txt", "keep damaged")
    _manifest(bad).write_bytes(b"{broken JSON")
    orphan = vault.paths.trash / "orphan.txt"
    orphan.write_bytes(b"keep orphan")
    before = bad.read_bytes(), _manifest(bad).read_bytes(), orphan.read_bytes()

    result = vault.purge_trash()

    assert (result.removed, result.skipped) == (1, 2)
    assert not good.exists() and not _manifest(good).exists()
    assert (bad.read_bytes(), _manifest(bad).read_bytes(), orphan.read_bytes()) == before
    assert {Path(row["slot"]) for row in vault.list_trash()} == {bad, orphan}
    purged = [row for row in vault.log.tail() if row["op"] == "purge"]
    assert len(purged) == 1 and Path(purged[0]["dst"]) == good


def test_directory_purge_failure_keeps_manifest_and_has_no_success_receipt(vault: VaultOps, monkeypatch) -> None:
    source = vault.paths.documents / "folder"
    source.mkdir()
    (source / "child.txt").write_bytes(b"remain")
    slot = vault.trash(source).dst
    before = _manifest(slot).read_bytes(), vault.log.tail()

    def denied(path, *, ignore_errors=False):
        if not ignore_errors:
            raise PermissionError("synthetic removal refused")

    monkeypatch.setattr("shutil.rmtree", denied)
    with pytest.raises(OSError):
        vault.purge_trash()
    assert (slot / "child.txt").read_bytes() == b"remain"
    assert (_manifest(slot).read_bytes(), vault.log.tail()) == before


@pytest.mark.parametrize("field,value", [
    ("trashed_at", None), ("trashed_at", []), ("trashed_at", ""),
    ("sha256", []), ("sha256", 5), ("sha256", "not-a-digest"),
    ("origin", "../escaped.txt"), ("origin", "documents/a\nb.txt"),
    ("origin", "C:relative.txt"), ("origin", "documents/bad:name.txt"),
])
def test_bad_metadata_fields_are_isolated_and_nonrestorable(vault: VaultOps, field: str, value) -> None:
    _, good = _trash(vault, "good.txt", "healthy")
    _, bad = _trash(vault, "bad.txt", "keep")
    manifest = _manifest(bad)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data[field] = value
    manifest.write_text(json.dumps(data), encoding="utf-8")

    rows = {Path(row["slot"]): row for row in vault.list_trash()}
    assert "error" not in rows[good]
    assert rows[bad]["error"].startswith("manifest damaged") and "origin" not in rows[bad]
    with pytest.raises(VaultError):
        vault.restore(bad)
    assert bad.read_text(encoding="utf-8") == "keep"


def test_manifest_cannot_spoof_slot_or_error_fields(vault: VaultOps) -> None:
    origin, slot = _trash(vault, "good.txt", "healthy")
    manifest = _manifest(slot)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data.update({"slot": "outside", "error": "fake damage", "extra": "untrusted"})
    manifest.write_text(json.dumps(data), encoding="utf-8")

    row = vault.list_trash()[0]
    assert set(row) == {"slot", "origin", "trashed_at", "sha256"}
    assert row["slot"] == str(slot)
    assert vault.restore(slot).dst == origin


@pytest.mark.parametrize("failure", [PermissionError, RecursionError])
def test_unreadable_or_unparseable_manifest_does_not_hide_healthy(vault: VaultOps, monkeypatch, failure) -> None:
    _, good = _trash(vault, "good.txt", "healthy")
    _, bad = _trash(vault, "bad.txt", "keep")
    real_read = Path.read_text

    def failed_read(path, *args, **kwargs):
        if path == _manifest(bad):
            raise failure("synthetic failed read")
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", failed_read)
    rows = {Path(row["slot"]): row for row in vault.list_trash()}
    assert "error" not in rows[good]
    assert rows[bad]["error"].startswith("manifest damaged")
    with pytest.raises(VaultError):
        vault.restore(bad)


@pytest.mark.parametrize("which", ["payload", "manifest"])
def test_reparse_entry_is_blocked_before_manifest_read(vault: VaultOps, monkeypatch, which: str) -> None:
    _, slot = _trash(vault, "linked.txt", "retain")
    target = slot if which == "payload" else _manifest(slot)
    real_lstat = Path.lstat

    def linked_stat(path, *args, **kwargs):
        result = real_lstat(path, *args, **kwargs)
        if path == target:
            return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return result

    def no_read(path, *args, **kwargs):
        raise AssertionError("a reparse payload/sidecar must be rejected before any manifest read")

    monkeypatch.setattr(Path, "lstat", linked_stat)
    monkeypatch.setattr(Path, "read_text", no_read)
    rows = vault.list_trash()
    assert len(rows) == 1 and rows[0]["error"].startswith("manifest damaged")
    with pytest.raises(VaultError):
        vault.restore(slot)
    result = vault.purge_trash()
    assert (result.removed, result.skipped) == (0, 1)
    assert slot.read_bytes() == b"retain" and _manifest(slot).exists()


def test_restore_does_not_resolve_an_error_slot(vault: VaultOps, monkeypatch) -> None:
    orphan = vault.paths.trash / "orphan.txt"
    orphan.write_bytes(b"retain")
    real_resolve = Path.resolve

    def no_slot_resolve(path, *args, **kwargs):
        if path == orphan:
            raise AssertionError("error slot must not follow its target")
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", no_slot_resolve)
    with pytest.raises(VaultError):
        vault.restore(orphan)


def test_reparse_trash_root_is_not_enumerated(vault: VaultOps, monkeypatch) -> None:
    real_lstat = Path.lstat

    def linked_stat(path, *args, **kwargs):
        result = real_lstat(path, *args, **kwargs)
        if path == vault.paths.trash:
            return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return result

    def no_scan(path):
        raise AssertionError("linked trash root must not be enumerated")

    monkeypatch.setattr(Path, "lstat", linked_stat)
    monkeypatch.setattr(Path, "iterdir", no_scan)
    with pytest.raises(VaultError):
        vault.list_trash()


def test_list_order_preserves_lexical_slot_order(vault: VaultOps) -> None:
    short = vault.paths.trash / "20200101T000000Z-z.txt"
    long = vault.paths.trash / "20200101T000000Z-a-long-name.txt"
    for slot in (short, long):
        slot.write_bytes(b"synthetic")
    assert [Path(row["slot"]).name for row in vault.list_trash()] == [long.name, short.name]


def test_missing_payload_sidecar_is_visible_and_cannot_create_restore_folders(vault: VaultOps) -> None:
    origin, slot = _trash(vault, "nested/lost.txt", "synthetic missing payload")
    slot.unlink()
    origin.parent.rmdir()
    manifest = _manifest(slot)
    before = manifest.read_bytes(), vault.log.tail()

    rows = vault.list_trash()
    assert rows == [{"slot": str(manifest), "error": "no manifest"}]
    with pytest.raises(VaultError):
        vault.restore(slot)
    assert not origin.parent.exists()
    result = vault.purge_trash()
    assert (result.removed, result.skipped) == (0, 1)
    assert (manifest.read_bytes(), vault.log.tail()) == before


def test_ambiguous_suffix_roles_are_all_kept_and_nonrestorable(vault: VaultOps) -> None:
    _, slot = _trash(vault, "overlap.txt", "retain payload")
    metadata = _manifest(slot)
    nested_metadata = _manifest(metadata)
    nested_metadata.write_bytes(metadata.read_bytes())
    paths = [slot, metadata, nested_metadata]
    before = {path: path.read_bytes() for path in paths}

    rows = vault.list_trash()
    assert {Path(row["slot"]) for row in rows} == set(paths)
    assert all(row["error"].startswith("manifest damaged") and "origin" not in row for row in rows)
    for path in paths:
        with pytest.raises(VaultError):
            vault.restore(path)
    result = vault.purge_trash()
    assert (result.removed, result.skipped) == (0, 3)
    assert {path: path.read_bytes() for path in paths} == before


def test_listing_and_purge_do_not_resolve_relative_restore_destination(vault: VaultOps, monkeypatch) -> None:
    _, slot = _trash(vault, "held.txt", "synthetic payload")
    manifest = _manifest(slot)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["origin"] = "documents/link/held.txt"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    candidate = vault.paths.documents / "link" / "held.txt"
    real_resolve = Path.resolve

    def no_candidate_resolve(path, *args, **kwargs):
        if path == candidate:
            raise AssertionError("display/purge must not probe the restore destination")
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", no_candidate_resolve)
    rows = vault.list_trash()
    assert rows[0]["origin"] == str(candidate) and "error" not in rows[0]
    result = vault.purge_trash()
    assert (result.removed, result.skipped) == (1, 0)
