"""Owner tree operations include hidden bytes but never follow links."""
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from vault_v2.errors import VaultError
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths


LINK_REFUSAL = "this folder contains a link or junction; move it outside Vault first"


@pytest.fixture
def ops(tmp_path):
    return VaultOps(VaultPaths(tmp_path / "synthetic-vault"))


@pytest.mark.skipif(os.name != "nt", reason="Real Windows junction boundary")
@pytest.mark.parametrize("operation", ["copy", "move", "trash", "clear", "restore"])
def test_tree_operation_refuses_deep_junction_before_first_effect(ops, tmp_path, monkeypatch, operation):
    import _winapi

    (ops.paths.staging / "first.txt").write_bytes(b"SYNTHETIC first item stays")
    source = ops.paths.staging / "folder"
    (source / "level-one").mkdir(parents=True)
    (source / "ordinary.txt").write_bytes(b"SYNTHETIC ordinary bytes")
    if operation == "restore":
        source = ops.trash(source).dst
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_bytes(b"SYNTHETIC must not be opened")
    link = source / "level-one" / "linked"
    _winapi.CreateJunction(str(outside), str(link))
    original_open = Path.open

    def no_target_read(path, *args, **kwargs):
        if path.is_relative_to(link) or path.is_relative_to(outside):
            raise AssertionError("operation followed the junction to read its target")
        return original_open(path, *args, **kwargs)

    before = ops.log.tail()
    pending = ops.paths.receipts / "pending"
    pending_before = list(pending.iterdir()) if pending.exists() else None
    trash_before = list(ops.paths.trash.iterdir())
    try:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "open", no_target_read)
            with pytest.raises(VaultError, match=f"^{LINK_REFUSAL}$"):
                if operation in ("copy", "move"):
                    getattr(ops, operation)(source, ops.paths.documents)
                elif operation == "clear":
                    ops.clear_staging()
                else:
                    getattr(ops, operation)(source)
        assert ops.log.tail() == before
        assert (list(pending.iterdir()) if pending.exists() else None) == pending_before
        assert list(ops.paths.trash.iterdir()) == trash_before
        assert not list(ops.paths.documents.iterdir())
        assert (source / "ordinary.txt").read_bytes() == b"SYNTHETIC ordinary bytes"
        assert (ops.paths.staging / "first.txt").read_bytes() == b"SYNTHETIC first item stays"
        assert not ops.log.guard.blocked
    finally:
        link.rmdir()  # Synthetic junction only; never its target.


@pytest.mark.skipif(os.name != "nt", reason="Real Windows junction boundary")
@pytest.mark.parametrize("operation", ["copy", "move", "trash", "clear", "restore"])
def test_destination_junction_refuses_before_source_hash_or_first_effect(ops, monkeypatch, operation):
    import _winapi

    source = ops.paths.staging / "folder"
    source.mkdir()
    (source / "ordinary.txt").write_bytes(b"SYNTHETIC ordinary bytes")
    target = ops.paths.documents / "actual-target"
    target.mkdir()
    if operation == "restore":
        source = ops.trash(source).dst
        link = ops.paths.staging
        link.rmdir()
    elif operation in ("trash", "clear"):
        link = ops.paths.trash
        link.rmdir()
    else:
        link = ops.paths.documents / "destination"
    _winapi.CreateJunction(str(target), str(link))
    original_open = Path.open

    def no_source_hash(path, *args, **kwargs):
        if path.is_relative_to(source):
            raise AssertionError("source content read before destination-link refusal")
        return original_open(path, *args, **kwargs)

    before = ops.log.tail()
    pending = ops.paths.receipts / "pending"
    pending_before = list(pending.iterdir()) if pending.exists() else None
    try:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "open", no_source_hash)
            with pytest.raises(VaultError, match=f"^{LINK_REFUSAL}$"):
                if operation in ("copy", "move"):
                    getattr(ops, operation)(source, link)
                elif operation == "clear":
                    ops.clear_staging()
                else:
                    getattr(ops, operation)(source)
        assert ops.log.tail() == before
        assert (list(pending.iterdir()) if pending.exists() else None) == pending_before
        assert not list(target.iterdir())
        assert (source / "ordinary.txt").read_bytes() == b"SYNTHETIC ordinary bytes"
        assert not ops.log.guard.blocked
    finally:
        link.rmdir()


def test_owner_operations_round_trip_all_hidden_tree_bytes(ops, tmp_path, monkeypatch):
    source = tmp_path / "external-folder"
    expected = {
        "visible.txt": b"SYNTHETIC visible",
        ".hidden.txt": b"SYNTHETIC hidden",
        ".hidden-folder/nested.txt": b"SYNTHETIC nested hidden",
        "metadata.trash.json": b"SYNTHETIC sidecar-looking payload",
        "level/.private/child.trash.json": b"SYNTHETIC deep hidden",
    }
    for relative, data in expected.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def assert_tree(root):
        assert {p.relative_to(root).as_posix(): p.read_bytes()
                for p in root.rglob("*") if p.is_file()} == expected

    copied = ops.copy(source, ops.paths.documents).dst
    assert_tree(source)
    assert_tree(copied)
    moved = ops.move(copied, ops.paths.staging).dst
    assert not copied.exists()
    assert_tree(moved)
    trashed = ops.trash(moved).dst
    assert_tree(trashed)
    restored = ops.restore(trashed).dst
    assert_tree(restored)
    class FrozenDatetime:
        @staticmethod
        def now(tz):
            return datetime(2026, 9, 23, 7, 40, 0, tzinfo=timezone.utc)

    monkeypatch.setattr("vault_v2.ops.datetime", FrozenDatetime)
    # A batch must reserve payload AND sidecar names before the first write.
    sibling = restored.with_name(restored.name + ".trash.json")
    sibling.write_bytes(b"SYNTHETIC actual owner document, not a manifest")
    assert ops.clear_staging() == 2
    assert not list(ops.paths.staging.iterdir())
    items = ops.list_trash()
    assert len(items) == 2 and all("error" not in item for item in items)
    cleared = Path(next(item["slot"] for item in items if Path(item["origin"]) == restored))
    assert_tree(cleared)
    assert_tree(ops.restore(cleared).dst)
    sibling_slot = Path(next(item["slot"] for item in items if Path(item["origin"]) == sibling))
    assert ops.restore(sibling_slot).dst.read_bytes() == b"SYNTHETIC actual owner document, not a manifest"
    assert ops.log.verify() == 8


@pytest.mark.skipif(os.name != "nt", reason="Real Windows junction boundary")
@pytest.mark.parametrize("operation", ["copy", "move", "trash", "clear", "restore"])
@pytest.mark.parametrize("above_root", [False, True])
def test_lexical_source_ancestor_is_checked_before_payload_or_manifest_read(
        tmp_path, monkeypatch, operation, above_root):
    import _winapi

    container = tmp_path / "container"
    ops = VaultOps(VaultPaths(container / "root"))
    source = ops.paths.staging / "folder"
    source.mkdir()
    (source / "ordinary.txt").write_bytes(b"SYNTHETIC must stay")
    if operation == "restore":
        source = ops.trash(source).dst
    before = ops.log.tail()
    pending_before = ops.log.pending()
    link = container if above_root else source.parent
    target = tmp_path / "relocated-synthetic-container"
    link.rename(target)
    _winapi.CreateJunction(str(target), str(link))
    actual_source = target / source.relative_to(link)
    original_open = Path.open

    def no_payload_read(path, *args, **kwargs):
        if path.is_relative_to(source) or path.is_relative_to(actual_source):
            raise AssertionError("payload read through a lexical ancestor junction")
        if operation == "restore" and path.name == source.name + ".trash.json":
            raise AssertionError("manifest read before source-ancestor refusal")
        return original_open(path, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "open", no_payload_read)
            with pytest.raises(VaultError, match=f"^{LINK_REFUSAL}$"):
                if operation in ("copy", "move"):
                    getattr(ops, operation)(source, ops.paths.documents)
                elif operation == "clear":
                    ops.clear_staging()
                else:
                    getattr(ops, operation)(source)
        assert ops.log.tail() == before
        assert ops.log.pending() == pending_before
        assert (actual_source / "ordinary.txt").read_bytes() == b"SYNTHETIC must stay"
        assert not list(ops.paths.documents.iterdir())
        assert not ops.log.guard.blocked
    finally:
        link.rmdir()
        target.rename(link)


@pytest.mark.parametrize("kind", ["file", "directory", "dangling"])
@pytest.mark.parametrize("operation", ["copy", "move", "trash", "clear", "restore"])
def test_all_symlink_kinds_refuse_before_payload_read(ops, tmp_path, monkeypatch, kind, operation):
    source = ops.paths.staging / "folder"
    source.mkdir()
    if operation == "restore":
        source = ops.trash(source).dst
    target = tmp_path / "synthetic-target"
    if kind == "file":
        target.write_bytes(b"SYNTHETIC must not read")
    elif kind == "directory":
        target.mkdir()
        (target / "ordinary.txt").write_bytes(b"SYNTHETIC must not read")
    link = source / "linked"
    try:
        link.symlink_to(target, target_is_directory=kind == "directory")
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows account cannot create symlinks; real junction matrix remains active")
        raise
    original_open = Path.open

    def no_target_read(path, *args, **kwargs):
        if path.is_relative_to(link) or path.is_relative_to(target):
            raise AssertionError("symlink target content read")
        return original_open(path, *args, **kwargs)

    before, pending_before = ops.log.tail(), ops.log.pending()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "open", no_target_read)
            with pytest.raises(VaultError, match=f"^{LINK_REFUSAL}$"):
                if operation in ("copy", "move"):
                    getattr(ops, operation)(source, ops.paths.documents)
                elif operation == "clear":
                    ops.clear_staging()
                else:
                    getattr(ops, operation)(source)
        assert ops.log.tail() == before
        assert ops.log.pending() == pending_before
        assert not list(ops.paths.documents.iterdir())
        assert not ops.log.guard.blocked
    finally:
        if kind == "directory":
            link.rmdir()
        else:
            link.unlink()


@pytest.mark.parametrize("operation", ["copy", "move", "restore"])
def test_selected_dangling_destination_refuses_before_pending_or_effect(ops, monkeypatch, operation):
    source = ops.paths.staging / "synthetic.txt"
    source.write_bytes(b"SYNTHETIC must stay")
    destination = ops.paths.documents / source.name
    if operation == "restore":
        destination, source = source, ops.trash(source).dst
    before, pending_before = ops.log.tail(), ops.log.pending()
    pending_dir = ops.paths.receipts / "pending"
    pending_existed = pending_dir.exists()
    original_lstat = Path.lstat

    def lstat(path, *args, **kwargs):
        if path == destination:
            # Filesystem seam: Windows account lacks CreateSymbolicLink privilege.
            # exists() still reports False, as it does for a dangling symlink.
            return SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0)
        return original_lstat(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", lstat)
        with pytest.raises(VaultError, match=f"^{LINK_REFUSAL}$"):
            if operation == "restore":
                ops.restore(source)
            else:
                getattr(ops, operation)(source, ops.paths.documents)
    assert ops.log.tail() == before
    assert ops.log.pending() == pending_before
    assert pending_dir.exists() == pending_existed
    assert source.read_bytes() == b"SYNTHETIC must stay"
    assert not destination.exists()
    assert not ops.log.guard.blocked


@pytest.mark.parametrize("operation", ["trash", "clear"])
def test_trash_sidecar_reparse_refuses_before_pending_or_effect(ops, monkeypatch, operation):
    class FrozenDatetime:
        @staticmethod
        def now(tz):
            return datetime(2026, 9, 23, 7, 40, 0, tzinfo=timezone.utc)

    monkeypatch.setattr("vault_v2.ops.datetime", FrozenDatetime)
    source = ops.paths.staging / "synthetic.txt"
    source.write_bytes(b"SYNTHETIC must stay")
    sidecar = ops.paths.trash / "20260923T074000Z-synthetic.txt.trash.json"
    original_lstat = Path.lstat

    def lstat(path, *args, **kwargs):
        if path == sidecar:
            return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)
        return original_lstat(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", lstat)
        with pytest.raises(VaultError, match=f"^{LINK_REFUSAL}$"):
            if operation == "clear":
                ops.clear_staging()
            else:
                ops.trash(source)
    assert ops.log.tail() == []
    assert not (ops.paths.receipts / "pending").exists()
    assert source.read_bytes() == b"SYNTHETIC must stay"
    assert not list(ops.paths.trash.iterdir())
    assert not ops.log.guard.blocked
