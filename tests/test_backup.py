"""The backup: encrypted, verified, restorable without the app, and honest about failure."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from vault_v2.backup import (
    MAGIC,
    BackupError,
    list_backups,
    make_backup,
    restore_backup,
    verify_backup,
)
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths

PASS = "correct horse battery staple"


@pytest.fixture()
def vault(tmp_path: Path):
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    (ops.paths.documents / "Insurance policy.txt").write_text("Blue Shield PPO-4471", encoding="utf-8")
    (ops.paths.personal / "photo.png").write_bytes(b"\x89PNG pretend")
    (ops.paths.staging / "note.txt").write_text("refill before 2026-10-16", encoding="utf-8")
    ops.log.append("import", "x", ops.paths.staging / "note.txt")
    return ops


def test_round_trip_keeps_every_byte_and_the_receipts(vault, tmp_path: Path) -> None:
    dest = tmp_path / "flash"
    result = make_backup(vault.paths.root, dest, PASS, vault.log)
    assert result.verified and result.path.exists() and result.files >= 4
    assert result.sha256 and result.size == result.path.stat().st_size

    into = tmp_path / "restored"
    n = restore_backup(result.path, PASS, into)
    assert n == result.files
    assert (into / "documents" / "Insurance policy.txt").read_text(encoding="utf-8") == "Blue Shield PPO-4471"
    assert (into / "personal" / "photo.png").read_bytes() == b"\x89PNG pretend"
    assert (into / ".receipts" / "receipts.jsonl").exists(), "the receipt chain travels with the vault"

    last = vault.log.tail(1)[0]
    assert last["op"] == "backup" and last["sha256"] == result.sha256 and last["extra"]["verified"] is True


def test_the_drive_is_useless_without_the_passphrase(vault, tmp_path: Path) -> None:
    result = make_backup(vault.paths.root, tmp_path / "flash", PASS)
    blob = result.path.read_bytes()
    assert blob.startswith(MAGIC)
    for secret in (b"Blue Shield", b"PPO-4471", b"refill", PASS.encode()):
        assert secret not in blob, "plaintext must not appear on the drive"
    with pytest.raises(BackupError, match="wrong passphrase"):
        verify_backup(result.path, "not the passphrase")
    with pytest.raises(BackupError):
        restore_backup(result.path, PASS.upper(), tmp_path / "r")


def test_a_damaged_backup_is_refused_not_half_restored(vault, tmp_path: Path) -> None:
    result = make_backup(vault.paths.root, tmp_path / "flash", PASS)
    data = bytearray(result.path.read_bytes())
    data[-40] ^= 0x01                      # one flipped bit deep in the ciphertext
    result.path.write_bytes(bytes(data))
    with pytest.raises(BackupError):
        verify_backup(result.path, PASS)
    into = tmp_path / "r"
    with pytest.raises(BackupError):
        restore_backup(result.path, PASS, into)
    assert not into.exists() or not any(into.iterdir())


def test_restore_never_lands_on_a_folder_that_has_things_in_it(vault, tmp_path: Path) -> None:
    result = make_backup(vault.paths.root, tmp_path / "flash", PASS)
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "something.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(BackupError, match="not empty"):
        restore_backup(result.path, PASS, busy)
    assert (busy / "something.txt").read_text(encoding="utf-8") == "mine"


def test_short_passphrases_are_refused_and_nothing_is_written(vault, tmp_path: Path) -> None:
    dest = tmp_path / "flash"
    with pytest.raises(BackupError, match="at least"):
        make_backup(vault.paths.root, dest, "short")
    assert list_backups(dest) == []


def test_restore_instructions_and_a_standalone_script_travel_with_the_archives(vault, tmp_path: Path) -> None:
    dest = tmp_path / "flash"
    make_backup(vault.paths.root, dest, PASS)
    assert (dest / "RESTORE.md").exists()
    script = (dest / "restore_backup.py").read_text(encoding="utf-8")
    assert "VAULTBK1" in script and "Scrypt" in script and "AESGCM" in script
    assert PASS not in script and PASS not in (dest / "RESTORE.md").read_text(encoding="utf-8")
    notes = (dest / "RESTORE.md").read_text(encoding="utf-8")
    assert "access keys" in notes and ".api/key" in notes and ".door/config.json" in notes
    assert "same paired phone" in notes


def test_the_standalone_script_really_restores(vault, tmp_path: Path, monkeypatch) -> None:
    """The script is the disaster-recovery path; it must work on its own."""
    import runpy
    import sys

    dest = tmp_path / "flash"
    result = make_backup(vault.paths.root, dest, PASS)
    into = tmp_path / "from-script"
    monkeypatch.setattr("getpass.getpass", lambda *_a, **_k: PASS)
    monkeypatch.setattr(sys, "argv", ["restore_backup.py", str(result.path), str(into)])
    runpy.run_path(str(dest / "restore_backup.py"), run_name="__main__")
    assert (into / "documents" / "Insurance policy.txt").read_text(encoding="utf-8") == "Blue Shield PPO-4471"


def test_backup_preserves_user_incoming_folder(vault, tmp_path: Path) -> None:
    user_folder = vault.paths.documents / "incoming"
    user_folder.mkdir()
    (user_folder / "user-document.txt").write_bytes(b"synthetic user document")
    service_folder = vault.paths.root / ".api" / "incoming"
    service_folder.mkdir(parents=True)
    (service_folder / "partial-upload.txt").write_bytes(b"temporary upload")

    result = make_backup(vault.paths.root, tmp_path / "flash", PASS)
    restored = tmp_path / "restored"
    restore_backup(result.path, PASS, restored)

    assert (restored / "documents" / "incoming" / "user-document.txt").read_bytes() == b"synthetic user document"
    assert not (restored / ".api" / "incoming").exists()


@pytest.mark.parametrize("relative", ["personal/.exports", "incoming", "documents/.api/incoming"])
def test_backup_exclusions_apply_only_at_exact_service_paths(vault, tmp_path: Path, relative: str) -> None:
    user_folder = vault.paths.root / relative
    user_folder.mkdir(parents=True)
    (user_folder / "keep.txt").write_bytes(b"synthetic retained bytes")
    exports = vault.paths.root / ".exports"
    exports.mkdir(exist_ok=True)
    (exports / "skip.txt").write_bytes(b"temporary export")

    result = make_backup(vault.paths.root, tmp_path / "flash", PASS)
    restored = tmp_path / "restored"
    restore_backup(result.path, PASS, restored)

    assert (restored / relative / "keep.txt").read_bytes() == b"synthetic retained bytes"
    assert not (restored / ".exports").exists()


def _report_sizes(monkeypatch, sizes: dict[Path, int]) -> None:
    """Filesystem-boundary sizes avoid allocating hundreds of MiB in tests."""
    real_stat = Path.stat

    def stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if path in sizes:
            fields = list(result)
            fields[6] = sizes[path]
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(Path, "stat", stat)


@pytest.mark.parametrize("sizes,existing", [
    ((300 * 1024**2, 212 * 1024**2 + 1), False),
    ((512 * 1024**2 + 1, 0), False),
    ((300 * 1024**2, 212 * 1024**2 + 1), True),
])
def test_backup_refuses_total_over_512_mib_before_read_or_destination(tmp_path: Path, monkeypatch, sizes, existing) -> None:
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    first = ops.paths.documents / "first.txt"
    second = ops.paths.personal / "second.txt"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    _report_sizes(monkeypatch, {first: sizes[0], second: sizes[1]})
    real_open = Path.open

    def open_file(path, *args, **kwargs):
        if path in (first, second):
            pytest.fail("oversized input must be refused before reading file contents")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    destination = tmp_path / "new-drive" / "backups"
    if existing:
        destination.mkdir(parents=True)
        (destination / "existing.vault").write_bytes(b"existing backup must survive")
    with pytest.raises(BackupError, match="512 MiB.*[Rr]educe.*streaming"):
        make_backup(ops.paths.root, destination, PASS, ops.log)
    if existing:
        assert [p.name for p in destination.iterdir()] == ["existing.vault"]
        assert (destination / "existing.vault").read_bytes() == b"existing backup must survive"
    else:
        assert not destination.parent.exists()
    assert ops.log.tail() == []


def test_backup_refuses_input_growth_without_writing_destination(tmp_path: Path, monkeypatch) -> None:
    # Exercise the same policy at tiny scale; the real 512 MiB boundary is
    # tested separately with filesystem stat sizes, not large allocations.
    monkeypatch.setattr("vault_v2.backup.MAX_BACKUP_INPUT_BYTES", 16)
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    first = ops.paths.documents / "first.txt"
    growing = ops.paths.personal / "growing.txt"
    first.write_bytes(b"aa")
    growing.write_bytes(b"bb")
    real_open = Path.open

    def open_file(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if path == growing and mode == "rb":
            with real_open(path, "ab") as stream:
                stream.write(b"synthetic growth after the size scan")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    destination = tmp_path / "new-drive" / "backups"
    with pytest.raises(BackupError, match="512 MiB"):
        make_backup(ops.paths.root, destination, PASS, ops.log)
    assert not destination.parent.exists()
    assert ops.log.tail() == []


def test_backup_cannot_qualify_size_when_an_included_directory_is_unreadable(tmp_path: Path, monkeypatch) -> None:
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    blocked = ops.paths.documents / "unreadable"
    blocked.mkdir()
    (blocked / "synthetic.txt").write_bytes(b"must not be silently skipped")
    real_scandir = os.scandir

    def scandir(path):
        if Path(path) == blocked:
            raise PermissionError("synthetic enumeration failure")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    destination = tmp_path / "new-drive" / "backups"
    with pytest.raises(OSError, match="synthetic enumeration failure"):
        make_backup(ops.paths.root, destination, PASS, ops.log)
    assert not destination.parent.exists()
    assert ops.log.tail() == []


@pytest.mark.parametrize("total", [512 * 1024**2 - 1, 512 * 1024**2])
def test_backup_accepts_included_bytes_at_or_below_limit(tmp_path: Path, monkeypatch, total: int) -> None:
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    first = ops.paths.documents / "first.txt"
    second = ops.paths.personal / "second.txt"
    first.write_bytes(b"first synthetic file")
    second.write_bytes(b"second synthetic file")
    _report_sizes(monkeypatch, {first: 300 * 1024**2, second: total - 300 * 1024**2})

    result = make_backup(ops.paths.root, tmp_path / "drive", PASS, ops.log)
    restored = tmp_path / "restored"
    assert result.verified and result.files == 2
    assert restore_backup(result.path, PASS, restored) == 2
    assert (restored / "documents/first.txt").read_bytes() == b"first synthetic file"
    assert (restored / "personal/second.txt").read_bytes() == b"second synthetic file"


def test_backup_size_excludes_only_service_paths(tmp_path: Path, monkeypatch) -> None:
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    incoming = ops.paths.root / ".api" / "incoming"
    incoming.mkdir(parents=True)
    upload = incoming / "upload.bin"
    export = ops.paths.exports / "temporary.bin"
    upload.write_bytes(b"temporary upload")
    export.write_bytes(b"temporary export")
    document = ops.paths.documents / "keep.txt"
    document.write_bytes(b"keep")
    _report_sizes(monkeypatch, {upload: 512 * 1024**2 + 1, export: 512 * 1024**2 + 1})

    result = make_backup(ops.paths.root, tmp_path / "drive", PASS)
    restored = tmp_path / "restored"
    assert result.files == 1
    restore_backup(result.path, PASS, restored)
    assert (restored / "documents/keep.txt").read_bytes() == b"keep"
    assert not (restored / ".api/incoming").exists()
    assert not (restored / ".exports").exists()


@pytest.mark.parametrize("relative", ["documents/incoming", "personal/.exports", "documents/.api/incoming"])
def test_user_folders_with_service_names_count_toward_backup_limit(tmp_path: Path, monkeypatch, relative: str) -> None:
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    document = ops.paths.root / relative / "large.txt"
    document.parent.mkdir(parents=True)
    document.write_bytes(b"synthetic user content")
    _report_sizes(monkeypatch, {document: 512 * 1024**2 + 1})
    destination = tmp_path / "drive"

    with pytest.raises(BackupError, match="512 MiB"):
        make_backup(ops.paths.root, destination, PASS)
    assert not destination.exists()


def test_backup_stat_failure_cannot_pass_size_preflight(tmp_path: Path, monkeypatch) -> None:
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    source = ops.paths.documents / "synthetic.txt"
    source.write_bytes(b"unreadable size")
    real_stat = Path.stat

    def stat(path, *args, **kwargs):
        if path == source:
            raise PermissionError("synthetic stat failure")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    destination = tmp_path / "drive"
    with pytest.raises(OSError, match="synthetic stat failure"):
        make_backup(ops.paths.root, destination, PASS, ops.log)
    assert not destination.exists()
    assert ops.log.tail() == []


def test_backup_reads_small_sources_without_requesting_a_cap_sized_buffer(tmp_path: Path, monkeypatch) -> None:
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    source = ops.paths.documents / "tiny.txt"
    source.write_bytes(b"tiny synthetic input")
    real_open = Path.open

    class InputReader:
        def __enter__(self):
            self.stream = real_open(source, "rb")
            return self

        def __exit__(self, *args):
            self.stream.close()

        def read(self, size=-1):
            assert 0 < size <= 1024**2, "size guard must not itself request a 512 MiB read buffer"
            return self.stream.read(size)

    def open_file(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if path == source and mode == "rb":
            return InputReader()
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    result = make_backup(ops.paths.root, tmp_path / "drive", PASS)
    assert result.verified
    restored = tmp_path / "restored"
    restore_backup(result.path, PASS, restored)
    assert (restored / "documents/tiny.txt").read_bytes() == b"tiny synthetic input"


def test_backup_allows_exact_actual_budget_followed_by_empty_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("vault_v2.backup.MAX_BACKUP_INPUT_BYTES", 16)
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    (ops.paths.documents / "full.txt").write_bytes(b"1234567890abcdef")
    (ops.paths.personal / "empty.txt").write_bytes(b"")

    result = make_backup(ops.paths.root, tmp_path / "drive", PASS)
    restored = tmp_path / "restored"
    assert result.files == 2 and result.verified
    restore_backup(result.path, PASS, restored)
    assert (restored / "documents/full.txt").read_bytes() == b"1234567890abcdef"
    assert (restored / "personal/empty.txt").read_bytes() == b""
