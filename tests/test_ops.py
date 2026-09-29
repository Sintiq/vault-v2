"""Tests for the operations layer — the part the GUI must never bypass."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from vault_v2.ops import VaultError, VaultOps
from vault_v2.backup import make_backup, restore_backup
from vault_v2.paths import DEFAULT_TITLES, VaultPaths
from vault_v2.receipts import ReceiptLog, sha256_file


@pytest.fixture()
def vault(tmp_path: Path) -> VaultOps:
    return VaultOps(VaultPaths(tmp_path / "vault"))


def _mk(p: Path, content: str = "hello") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_import_lands_in_staging_with_receipt(vault: VaultOps, tmp_path: Path) -> None:
    src = _mk(tmp_path / "outside" / "doc.txt", "abc")
    r = vault.import_file(src)
    assert r.dst.parent == vault.paths.staging
    assert r.dst.read_text(encoding="utf-8") == "abc"
    assert r.sha256 == sha256_file(src)
    assert src.exists(), "import never touches the original"
    assert vault.log.verify() == 1


def test_copy_between_panes_keeps_original(vault: VaultOps, tmp_path: Path) -> None:
    src = _mk(vault.paths.personal / "a.txt", "x")
    r = vault.copy(src, vault.paths.staging)
    assert r.dst == vault.paths.staging / "a.txt"
    assert src.exists()
    assert vault.log.verify() == 1


def test_copy_never_overwrites(vault: VaultOps) -> None:
    src = _mk(vault.paths.personal / "a.txt", "new")
    _mk(vault.paths.staging / "a.txt", "old")
    r = vault.copy(src, vault.paths.staging)
    assert r.dst.name == "a (1).txt"
    assert (vault.paths.staging / "a.txt").read_text(encoding="utf-8") == "old"


def test_move_removes_source(vault: VaultOps) -> None:
    src = _mk(vault.paths.staging / "m.txt", "m")
    r = vault.move(src, vault.paths.documents)
    assert not src.exists()
    assert r.dst == vault.paths.documents / "m.txt"


def test_ops_refuse_paths_outside_vault(vault: VaultOps, tmp_path: Path) -> None:
    outside = _mk(tmp_path / "elsewhere" / "z.txt")
    with pytest.raises(VaultError):
        vault.copy(outside, tmp_path / "elsewhere")
    with pytest.raises(VaultError):
        vault.trash(outside)


def test_trash_and_restore_round_trip(vault: VaultOps) -> None:
    src = _mk(vault.paths.documents / "t.txt", "t")
    digest = sha256_file(src)
    r = vault.trash(src)
    assert not src.exists()
    assert r.dst.parent == vault.paths.trash
    items = vault.list_trash()
    assert len(items) == 1 and items[0]["sha256"] == digest
    back = vault.restore(Path(items[0]["slot"]))
    assert back.dst == src and src.read_text(encoding="utf-8") == "t"
    assert vault.list_trash() == []
    assert vault.log.verify() == 2


def test_same_second_same_name_trash_preserves_every_original(vault: VaultOps, monkeypatch) -> None:
    class FrozenDatetime:
        @staticmethod
        def now(tz):
            return datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)

    monkeypatch.setattr("vault_v2.ops.datetime", FrozenDatetime)
    originals = [
        _mk(vault.paths.documents / "one" / "same.txt", "first"),
        _mk(vault.paths.documents / "two" / "same.txt", "second"),
        _mk(vault.paths.personal / "same.txt", "third"),
    ]
    results = [vault.trash(path) for path in originals]

    assert len({result.dst for result in results}) == 3
    assert len(vault.list_trash()) == 3
    for result, original, expected in zip(results, originals, ("first", "second", "third")):
        restored = vault.restore(result.dst)
        assert restored.dst == original
        assert original.read_text(encoding="utf-8") == expected
    assert vault.log.verify() == 6


def test_trash_origin_resolution_failure_leaves_source_and_trash_untouched(
    vault: VaultOps, tmp_path: Path, monkeypatch,
) -> None:
    original = _mk(vault.paths.documents / "synthetic.txt", "keep original bytes")
    real_resolve = Path.resolve
    source_resolutions = 0

    def resolve(path, *args, **kwargs):
        nonlocal source_resolutions
        resolved = real_resolve(path, *args, **kwargs)
        if path == original:
            source_resolutions += 1
            if source_resolutions > 1:
                # The first containment check passes; later filesystem
                # resolution behaves as if a mount/junction changed.
                return tmp_path / "different-root" / "synthetic.txt"
        return resolved

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises((VaultError, ValueError)) as failure:
        vault.trash(original)

    assert original.read_text(encoding="utf-8") == "keep original bytes"
    assert list(vault.paths.trash.iterdir()) == []
    assert vault.log.tail() == []
    assert isinstance(failure.value, VaultError)


@pytest.mark.parametrize("directory", [False, True])
def test_purge_checks_receipts_before_deleting_any_bytes(vault: VaultOps, directory: bool) -> None:
    src = _mk(vault.paths.documents / "kept" / "nested.txt", "keep me") if directory else _mk(
        vault.paths.documents / "kept.txt", "keep me"
    )
    result = vault.trash(src.parent if directory else src)
    manifest = result.dst.with_name(result.dst.name + ".trash.json")
    manifest_before = manifest.read_bytes()
    receipt = json.loads(vault.log.file.read_text(encoding="utf-8"))
    receipt["note"] = "synthetic corruption"
    vault.log.file.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
    receipt_before = vault.log.file.read_bytes()

    with pytest.raises((VaultError, ValueError)):
        vault.purge_trash()

    remaining = result.dst / "nested.txt" if directory else result.dst
    assert remaining.read_text(encoding="utf-8") == "keep me"
    assert manifest.read_bytes() == manifest_before
    assert vault.log.file.read_bytes() == receipt_before


def test_backed_up_trash_restores_into_relocated_vault(vault: VaultOps, tmp_path: Path) -> None:
    original = _mk(vault.paths.personal / "family" / "synthetic.txt", "synthetic keepsake")
    vault.trash(original)
    backup = make_backup(vault.paths.root, tmp_path / "backup", "synthetic-passphrase")
    relocated_root = tmp_path / "relocated"
    restore_backup(backup.path, "synthetic-passphrase", relocated_root)
    relocated = VaultOps(VaultPaths(relocated_root))

    item = relocated.list_trash()[0]
    restored = relocated.restore(Path(item["slot"]))

    assert restored.dst == relocated.paths.personal / "family" / "synthetic.txt"
    assert restored.dst.read_text(encoding="utf-8") == "synthetic keepsake"
    assert not original.exists(), "restoring a relocated backup must not write into the old root"
    assert relocated.list_trash() == []
    assert relocated.log.verify() == 2


def test_trash_listing_resolves_relative_origin_for_existing_callers(vault: VaultOps) -> None:
    original = _mk(vault.paths.documents / "folder" / "synthetic.txt")
    vault.trash(original)

    item = vault.list_trash()[0]

    assert Path(item["origin"]) == original
    assert vault.paths.pane_of(Path(item["origin"])) == "documents"


def test_legacy_absolute_trash_manifest_restores_in_same_root(vault: VaultOps) -> None:
    original = _mk(vault.paths.personal / "old" / "synthetic.txt", "legacy")
    result = vault.trash(original)
    manifest = result.dst.with_name(result.dst.name + ".trash.json")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["origin"] = str(original)
    manifest.write_text(json.dumps(data), encoding="utf-8")

    assert Path(vault.list_trash()[0]["origin"]) == original
    restored = vault.restore(result.dst)
    assert restored.dst == original
    assert original.read_text(encoding="utf-8") == "legacy"


@pytest.mark.parametrize("origin", [
    None, [], {}, 7, "", "../escaped/synthetic.txt", "documents/../../escaped/synthetic.txt",
    ".receipts/forged.txt", "documents",
])
def test_restore_refuses_invalid_manifest_origins_without_effect(vault: VaultOps, origin) -> None:
    original = _mk(vault.paths.documents / "synthetic.txt", "do not move")
    result = vault.trash(original)
    manifest = result.dst.with_name(result.dst.name + ".trash.json")
    manifest.write_text(json.dumps({"origin": origin}), encoding="utf-8")
    before = manifest.read_bytes(), vault.log.file.read_bytes()

    with pytest.raises(VaultError):
        vault.restore(result.dst)

    assert result.dst.read_text(encoding="utf-8") == "do not move"
    assert (manifest.read_bytes(), vault.log.file.read_bytes()) == before


def test_restore_refuses_legacy_absolute_origin_in_different_root(vault: VaultOps, tmp_path: Path) -> None:
    original = _mk(vault.paths.documents / "synthetic.txt", "stay in trash")
    result = vault.trash(original)
    old_origin = tmp_path / "old-root" / "nested" / "documents" / "synthetic.txt"
    manifest = result.dst.with_name(result.dst.name + ".trash.json")
    manifest.write_text(json.dumps({"origin": str(old_origin)}), encoding="utf-8")

    with pytest.raises(VaultError, match="outside|different|relocat"):
        vault.restore(result.dst)

    assert result.dst.read_text(encoding="utf-8") == "stay in trash"
    assert not old_origin.parent.exists()
    assert not original.exists()


@pytest.mark.parametrize("contents", ["{not-json", "[]", "{}", '{"origin": null}'])
def test_restore_refuses_malformed_manifest_as_vault_error(vault: VaultOps, contents: str) -> None:
    result = vault.trash(_mk(vault.paths.documents / "synthetic.txt", "untouched"))
    manifest = result.dst.with_name(result.dst.name + ".trash.json")
    manifest.write_text(contents, encoding="utf-8")

    with pytest.raises(VaultError):
        vault.restore(result.dst)

    assert result.dst.read_text(encoding="utf-8") == "untouched"
    assert manifest.read_text(encoding="utf-8") == contents


def test_restore_refuses_slot_outside_this_vault_trash(vault: VaultOps, tmp_path: Path) -> None:
    alien_slot = _mk(tmp_path / "alien" / "synthetic.txt", "not this vault's trash")
    manifest = alien_slot.with_name(alien_slot.name + ".trash.json")
    manifest.write_text(json.dumps({"origin": "documents/synthetic.txt"}), encoding="utf-8")

    with pytest.raises(VaultError):
        vault.restore(alien_slot)

    assert alien_slot.read_text(encoding="utf-8") == "not this vault's trash"
    assert manifest.exists()
    assert not (vault.paths.documents / "synthetic.txt").exists()
    assert vault.log.verify() == 0


def test_legacy_origin_in_other_root_does_not_hide_recoverable_trash(vault: VaultOps, tmp_path: Path) -> None:
    good_original = _mk(vault.paths.documents / "good.txt", "recover me")
    good = vault.trash(good_original)
    legacy = vault.trash(_mk(vault.paths.personal / "legacy.txt", "legacy bytes"))
    old_origin = tmp_path / "old-root" / "personal" / "legacy.txt"
    manifest = legacy.dst.with_name(legacy.dst.name + ".trash.json")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["origin"] = str(old_origin)
    manifest.write_text(json.dumps(data), encoding="utf-8")

    items = vault.list_trash()

    assert len(items) == 2
    assert {Path(item["origin"]) for item in items} == {good_original, old_origin}
    assert vault.restore(good.dst).dst == good_original
    with pytest.raises(VaultError, match="outside|different|relocat"):
        vault.restore(legacy.dst)
    assert legacy.dst.read_text(encoding="utf-8") == "legacy bytes"
    assert not old_origin.parent.exists()


def test_clear_staging_only_touches_staging(vault: VaultOps) -> None:
    _mk(vault.paths.staging / "s1.txt")
    _mk(vault.paths.staging / "sub" / "s2.txt")
    keep = _mk(vault.paths.documents / "keep.txt")
    n = vault.clear_staging()
    assert n == 2
    assert list(vault.paths.staging.iterdir()) == []
    assert keep.exists()


def test_mkdir_rejects_path_like_names(vault: VaultOps) -> None:
    with pytest.raises(VaultError):
        vault.mkdir(vault.paths.documents, "../escape")
    with pytest.raises(VaultError):
        vault.mkdir(vault.paths.documents, "a/b")
    made = vault.mkdir(vault.paths.documents, "ok")
    assert made.is_dir()


def test_titles_default_and_override(tmp_path: Path) -> None:
    paths = VaultPaths(tmp_path / "v").ensure()
    assert paths.titles() == DEFAULT_TITLES
    paths.settings_file.write_text(json.dumps({"titles": {"documents": "Архив", "bogus": "x"}}), encoding="utf-8")
    t = paths.titles()
    assert t["documents"] == "Архив" and t["staging"] == DEFAULT_TITLES["staging"] and "bogus" not in t


def test_receipt_chain_detects_middle_tamper(tmp_path: Path) -> None:
    log = ReceiptLog(tmp_path / "r")
    log.append("a", "x", "y", sha256="1", size=1)
    log.append("b", "x", "y", sha256="2", size=2)
    log.append("c", "x", "y", sha256="3", size=3)
    assert log.verify() == 3
    lines = log.file.read_text(encoding="utf-8").splitlines()
    mid = json.loads(lines[1])
    mid["size"] = 999
    lines[1] = json.dumps(mid, ensure_ascii=False)
    log.file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        log.verify()
    with pytest.raises(ValueError):
        log.append("d", "x", "y")  # a broken chain blocks further writes
