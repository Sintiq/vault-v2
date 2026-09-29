"""Synthetic cache retention through the public trash/backup operations."""
import hashlib
import json
import os
from pathlib import Path

import pytest

from vault_v2.backup import make_backup, restore_backup
from vault_v2.cards import CardStore
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.reader import StagingReader


def cache_pair(paths, payload=b"synthetic source"):
    digest = hashlib.sha256(payload).hexdigest()
    directory = paths.root / ".text"
    directory.mkdir(exist_ok=True)
    text = directory / f"{digest}.txt"
    meta = directory / f"{digest}.json"
    text.write_text("synthetic extracted text", encoding="utf-8")
    meta.write_text("{}", encoding="utf-8")
    return text, meta


def test_purge_last_document_copy_removes_its_exact_cache_pair(tmp_path):
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    source = ops.paths.staging / "synthetic.pdf"
    source.write_bytes(b"synthetic source")
    pair = cache_pair(ops.paths)
    unrelated = cache_pair(ops.paths, b"unrelated source")
    ops.trash(source)

    result = ops.purge_trash()

    assert result.removed == 1 and result.skipped == 0
    assert all(not path.exists() for path in pair)
    assert all(path.exists() for path in unrelated)
    assert not ops.log.pending()
    assert ops.log.verify() >= 2


@pytest.mark.parametrize("pane", ["staging", "documents", "personal"])
def test_purge_keeps_cache_while_identical_bytes_exist_in_any_pane(tmp_path, pane):
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    source = ops.paths.staging / "synthetic.pdf"
    source.write_bytes(b"synthetic source")
    pair = cache_pair(ops.paths)
    copy = ops.paths.pane(pane) / "renamed-copy.bin"
    copy.write_bytes(source.read_bytes())
    ops.trash(source)
    first = ops.purge_trash()
    assert first.removed == 1 and not first.cache_deferred
    assert all(path.exists() for path in pair)

    ops.trash(copy)
    second = ops.purge_trash()
    assert second.removed == 1 and not second.cache_deferred
    assert all(not path.exists() for path in pair)


def test_purge_directory_uses_actual_child_bytes_not_manifest_or_card_digest(tmp_path):
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    source = ops.paths.documents / "folder"
    source.mkdir()
    (source / "child.pdf").write_bytes(b"synthetic source")
    pair = cache_pair(ops.paths)
    slot = ops.trash(source).dst
    manifest = slot.with_name(slot.name + ".trash.json")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["sha256"] = "0" * 64
    manifest.write_text(json.dumps(data), encoding="utf-8")

    result = ops.purge_trash()
    assert result.removed == 1 and not result.cache_deferred
    assert all(not path.exists() for path in pair)


@pytest.mark.parametrize("damage", ["orphan", "manifest", "unreadable-source"])
def test_purge_retains_cache_and_reports_uncertain_remaining_copies(tmp_path, monkeypatch, damage):
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    source = ops.paths.staging / "remove.pdf"
    source.write_bytes(b"synthetic source")
    pair = cache_pair(ops.paths)
    ops.trash(source)
    if damage == "orphan":
        (ops.paths.trash / "orphan.bin").write_bytes(b"possibly a copy")
    elif damage == "manifest":
        other = ops.paths.documents / "damaged.bin"
        other.write_bytes(b"possibly a copy")
        slot = ops.trash(other).dst
        slot.with_name(slot.name + ".trash.json").write_text("{", encoding="utf-8")
    else:
        other = ops.paths.personal / "unreadable.bin"
        other.write_bytes(b"possibly a copy")
        real_open = Path.open

        def denied(path, *args, **kwargs):
            if path == other:
                raise PermissionError("synthetic inaccessible remaining file")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", denied)

    result = ops.purge_trash()
    assert result.removed == 1 and result.cache_deferred
    assert all(path.exists() for path in pair)
    assert ops.log.tail()[-1]["op"] == "text_cache_cleanup_deferred"


def test_encrypted_backup_restore_keeps_a_reusable_extracted_text_pair(tmp_path):
    from test_local_text import ENGLISH, text_pdf

    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    source = text_pdf(ops.paths.staging / "synthetic.pdf")
    reader = StagingReader(ops, CardStore(ops.paths.root / ".cards", ops.log))
    assert ENGLISH in reader.read_text(source.name)
    backup = make_backup(ops.paths.root, tmp_path / "drive", "synthetic passphrase", ops.log)
    restored_root = tmp_path / "restored"
    restore_backup(backup.path, "synthetic passphrase", restored_root)
    restored = VaultOps(VaultPaths(restored_root))
    reader = StagingReader(restored, CardStore(restored_root / ".cards", restored.log))

    assert ENGLISH in reader.read_text(source.name)
    assert len([row for row in restored.log.tail() if row["op"] == "text_extracted"]) == 1
    assert restored.log.verify() > 0


@pytest.mark.skipif(os.name != "nt", reason="Windows junction boundary")
@pytest.mark.parametrize("location", ["cache", "pane", "trash-descendant"])
def test_purge_never_traverses_linked_cache_or_possible_source_copy(tmp_path, location):
    import _winapi

    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    source = ops.paths.staging / "synthetic.pdf"
    source.write_bytes(b"synthetic source")
    pair = cache_pair(ops.paths)
    ops.trash(source)
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "marker.txt"
    marker.write_bytes(b"outside synthetic marker")
    if location == "cache":
        for path in pair:
            path.unlink()
        linked = ops.paths.root / ".text"
        linked.rmdir()
    elif location == "pane":
        linked = ops.paths.personal / "linked"
    else:
        directory = ops.paths.documents / "folder"
        directory.mkdir()
        slot = ops.trash(directory).dst
        linked = slot / "linked"
    _winapi.CreateJunction(str(outside), str(linked))
    try:
        result = ops.purge_trash()
        assert result.removed == 1 and result.cache_deferred
        assert marker.read_bytes() == b"outside synthetic marker"
        assert list(outside.iterdir()) == [marker]
        if location != "cache":
            assert all(path.exists() for path in pair)
    finally:
        linked.rmdir()  # Only this synthetic junction is removed.


def test_unreadable_cache_container_keeps_healthy_trash_purge_visible(tmp_path, monkeypatch):
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    source = ops.paths.staging / "synthetic.pdf"
    source.write_bytes(b"synthetic source")
    pair = cache_pair(ops.paths)
    ops.trash(source)
    real_lstat = Path.lstat

    def unreadable_cache(path, *args, **kwargs):
        if path == ops.paths.root / ".text":
            raise PermissionError("synthetic inaccessible cache directory")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", unreadable_cache)
    result = ops.purge_trash()
    assert result.removed == 1 and result.cache_deferred
    assert all(path.exists() for path in pair)
