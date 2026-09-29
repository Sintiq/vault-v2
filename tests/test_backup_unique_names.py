"""Backup output never replaces another archive or someone else's partial file."""

from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import io
from pathlib import Path
from threading import Barrier
import zipfile

import pytest

from vault_v2.backup import BackupError, decrypt, list_backups, make_backup, verify_backup
from vault_v2.errors import MayHaveApplied
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths


PASSPHRASE = "synthetic backup passphrase"
STAMP = "20260923-081500"


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = cls(2026, 9, 23, 8, 15, tzinfo=timezone.utc)
            return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)

    monkeypatch.setattr("vault_v2.backup.datetime", FrozenDateTime)
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    (ops.paths.documents / "document.txt").write_bytes(b"first synthetic snapshot")
    return ops


def document_bytes(archive_path):
    with zipfile.ZipFile(io.BytesIO(decrypt(archive_path.read_bytes(), PASSPHRASE))) as archive:
        return archive.read("documents/document.txt")


def test_same_second_backups_keep_both_distinct_verified_snapshots(vault, tmp_path):
    destination = tmp_path / "drive"
    first = make_backup(vault.paths.root, destination, PASSPHRASE, vault.log)
    first_archive = first.path.read_bytes()
    (vault.paths.documents / "document.txt").write_bytes(b"second synthetic snapshot")

    second = make_backup(vault.paths.root, destination, PASSPHRASE, vault.log)

    assert first.path != second.path
    assert first.path.read_bytes() == first_archive
    assert set(list_backups(destination)) == {first.path, second.path}
    assert first.verified and second.verified
    assert verify_backup(first.path, PASSPHRASE) == first.files
    assert verify_backup(second.path, PASSPHRASE) == second.files
    assert document_bytes(first.path) == b"first synthetic snapshot"
    assert document_bytes(second.path) == b"second synthetic snapshot"
    assert [row["dst"] for row in vault.log.tail() if row["op"] == "backup"] == [
        str(first.path), str(second.path)]


@pytest.mark.parametrize("existing_archives", [0, 2])
def test_existing_archives_and_unknown_partial_files_are_preserved(vault, tmp_path, existing_archives):
    destination = tmp_path / "drive"
    destination.mkdir()
    preserved = {}
    for suffix in ("", "-1"):
        partial = destination / f"vault-{STAMP}{suffix}.part"
        partial.write_bytes(b"unrelated unfinished backup")
        preserved[partial] = partial.read_bytes()
    for number in range(existing_archives):
        suffix = f"-{number}" if number else ""
        archive = destination / f"vault-{STAMP}{suffix}.vault"
        archive.write_bytes(b"unrelated existing archive")
        preserved[archive] = archive.read_bytes()

    result = make_backup(vault.paths.root, destination, PASSPHRASE, vault.log)

    assert result.path not in preserved
    assert {path: path.read_bytes() for path in preserved} == preserved
    assert result.verified and verify_backup(result.path, PASSPHRASE) == result.files
    assert document_bytes(result.path) == b"first synthetic snapshot"


def test_competing_creator_wins_candidate_without_being_overwritten(vault, tmp_path, monkeypatch):
    destination = tmp_path / "drive"
    original_open = Path.open
    claimed = []

    def competing_open(path, mode="r", *args, **kwargs):
        if path.parent == destination and path.suffix == ".vault" and mode == "xb" and not claimed:
            with original_open(path, "xb") as other_writer:
                other_writer.write(b"concurrent creator owns this file")
            claimed.append(path)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", competing_open)
    result = make_backup(vault.paths.root, destination, PASSPHRASE, vault.log)

    assert len(claimed) == 1
    assert claimed[0] != result.path
    assert claimed[0].read_bytes() == b"concurrent creator owns this file"
    assert result.verified and verify_backup(result.path, PASSPHRASE) == result.files
    assert document_bytes(result.path) == b"first synthetic snapshot"


def test_partial_output_failure_never_reports_verified_backup(vault, tmp_path, monkeypatch):
    destination = tmp_path / "drive"
    original_open = Path.open

    class BrokenOutput:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def write(self, data):
            self.stream.write(data[:64])
            self.stream.flush()
            raise OSError("synthetic drive failure after partial write")

    def broken_open(path, mode="r", *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        if path.parent == destination and path.suffix == ".vault" and mode == "xb":
            return BrokenOutput(stream)
        return stream

    monkeypatch.setattr(Path, "open", broken_open)
    with pytest.raises(MayHaveApplied):
        make_backup(vault.paths.root, destination, PASSPHRASE, vault.log)

    assert vault.log.guard.blocked
    assert not any(row["op"] == "backup" for row in vault.log.tail())
    partial_archives = list_backups(destination)
    assert len(partial_archives) == 1
    with pytest.raises(BackupError):
        verify_backup(partial_archives[0], PASSPHRASE)
    assert not (destination / "RESTORE.md").exists()


def test_two_vaults_can_back_up_concurrently_without_output_overwrite(vault, tmp_path, monkeypatch):
    other = VaultOps(VaultPaths(tmp_path / "other-vault"))
    (other.paths.documents / "document.txt").write_bytes(b"other synthetic vault")
    destination = tmp_path / "shared-drive"
    first_candidate = destination / f"vault-{STAMP}.vault"
    rendezvous = Barrier(2)
    original_open = Path.open

    def concurrent_open(path, mode="r", *args, **kwargs):
        if path == first_candidate and mode == "xb":
            rendezvous.wait(timeout=10)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", concurrent_open)
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(make_backup, owner.paths.root, destination, PASSPHRASE, owner.log)
                   for owner in (vault, other)]
        results = [future.result(timeout=20) for future in futures]

    assert len(set(result.path for result in results)) == 2
    assert set(list_backups(destination)) == {result.path for result in results}
    for owner, result in zip((vault, other), results):
        assert result.verified and verify_backup(result.path, PASSPHRASE) == result.files
        assert owner.log.tail(1)[0]["dst"] == str(result.path)
    assert {document_bytes(result.path) for result in results} == {
        b"first synthetic snapshot", b"other synthetic vault"}


def test_occupied_candidate_names_stop_without_success_or_overwrite(vault, tmp_path, monkeypatch):
    destination = tmp_path / "drive"
    original_open = Path.open
    attempts = []

    def occupied_open(path, mode="r", *args, **kwargs):
        if path.parent == destination and path.suffix == ".vault" and mode == "xb":
            attempts.append(path)
            raise FileExistsError("synthetic concurrently occupied candidate")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", occupied_open)
    with pytest.raises(MayHaveApplied):
        make_backup(vault.paths.root, destination, PASSPHRASE, vault.log)

    assert 0 < len(attempts) <= 1000
    assert list_backups(destination) == []
    assert not any(destination.iterdir())
    assert not any(row["op"] == "backup" for row in vault.log.tail())
