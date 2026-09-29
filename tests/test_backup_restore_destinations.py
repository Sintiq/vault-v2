"""Disaster recovery refuses aliased targets before writing any vault bytes."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from vault_v2.backup import BackupError, make_backup, restore_backup


PASSPHRASE = "synthetic recovery passphrase"
PAYLOADS = {
    "documents/ordinary.txt": b"SYNTHETIC ordinary document",
    ".api/key": b"SYNTHETIC paired phone key",
    ".text/synthetic.txt": b"SYNTHETIC extracted text",
    "personal/.private/nested.txt": b"SYNTHETIC hidden document",
}


@pytest.fixture
def backup(tmp_path):
    root = tmp_path / "synthetic-source"
    for relative, data in PAYLOADS.items():
        source = root / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(data)
    return make_backup(root, tmp_path / "removable-copy", PASSPHRASE)


def run_standalone(backup, target, *, snapshot_guard=False):
    script = backup.path.parent / "restore_backup.py"
    # Windows getpass.getwch() ignores redirected stdin. Supply only this
    # terminal-input boundary while executing the unmodified exported CLI in
    # an isolated child with no application imports or repository cwd.
    runner = (
        "import getpass, runpy, sys\n"
        "from pathlib import Path\n"
        "getpass.getpass = lambda *a, **k: sys.stdin.readline().rstrip('\\r\\n')\n"
        "sys.argv = sys.argv[1:]\n"
    )
    if snapshot_guard:
        runner += (
            "source, reads, original_read = Path(sys.argv[1]), [], Path.read_bytes\n"
            "def read_snapshot(path, *args, **kwargs):\n"
            "    if path == source:\n"
            "        reads.append(path)\n"
            "        if len(reads) > 1:\n"
            "            return b'SYNTHETIC changed archive must not be read'\n"
            "    return original_read(path, *args, **kwargs)\n"
            "Path.read_bytes = read_snapshot\n"
        )
    runner += "runpy.run_path(sys.argv[0], run_name='__main__')\n"
    if snapshot_guard:
        runner += "print('archive snapshot reads:', len(reads))\n"
    return subprocess.run(
        [sys.executable, "-I", "-c", runner, str(script), str(backup.path), str(target)],
        input=PASSPHRASE + "\n", text=True, capture_output=True,
        cwd=script.parent, timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def assert_refused(route, backup, target):
    if route == "app":
        with pytest.raises(BackupError):
            restore_backup(backup.path, PASSPHRASE, target)
    else:
        result = run_standalone(backup, target)
        assert result.returncode != 0, result.stdout
        assert "restored " not in result.stdout
        assert "restore refused or interrupted:" in result.stderr, result.stderr


@pytest.mark.skipif(os.name != "nt", reason="Real Windows junction destination")
@pytest.mark.parametrize("route", ["app", "standalone"])
@pytest.mark.parametrize("location", ["target", "ancestor"])
def test_recovery_refuses_target_or_ancestor_junction_before_any_write(backup, tmp_path, route, location):
    import _winapi

    actual = tmp_path / "synthetic-junction-target"
    actual.mkdir()
    link = tmp_path / "synthetic-alias"
    # Validate exact roots before creating the alias. Even an unfixed restore
    # may only write into this test's own temporary directory.
    assert actual.resolve().is_relative_to(tmp_path.resolve())
    assert link.absolute().is_relative_to(tmp_path.absolute())
    _winapi.CreateJunction(str(actual), str(link))
    target = link if location == "target" else link / "missing" / "restore"
    try:
        assert target.resolve().is_relative_to(tmp_path.resolve())
        assert_refused(route, backup, target)
        assert list(actual.iterdir()) == []
    finally:
        assert link.is_junction() and link.resolve() == actual.resolve()
        link.rmdir()  # Only the synthetic junction, never its target tree.


@pytest.mark.parametrize("route", ["app", "standalone"])
@pytest.mark.parametrize("existing", [False, True])
def test_recovery_to_empty_target_keeps_hidden_and_service_bytes(backup, tmp_path, route, existing):
    target = tmp_path / "new-parent" / "restored"
    if existing:
        target.mkdir(parents=True)
    if route == "app":
        assert restore_backup(backup.path, PASSPHRASE, target) == len(PAYLOADS)
    else:
        result = run_standalone(backup, target)
        assert result.returncode == 0, result.stderr
        assert "restored 4 files into " in result.stdout
    actual = {path.relative_to(target).as_posix(): path.read_bytes()
              for path in target.rglob("*") if path.is_file()}
    assert actual == PAYLOADS


@pytest.mark.parametrize("route", ["app", "standalone"])
def test_recovery_keeps_nonempty_target_exactly_unchanged(backup, tmp_path, route):
    target = tmp_path / "occupied"
    target.mkdir()
    marker = target / "existing.txt"
    marker.write_bytes(b"SYNTHETIC existing bytes must survive")
    assert_refused(route, backup, target)
    assert list(target.iterdir()) == [marker]
    assert marker.read_bytes() == b"SYNTHETIC existing bytes must survive"


@pytest.mark.parametrize("route", ["app", "standalone"])
def test_recovery_reads_one_immutable_archive_snapshot(backup, tmp_path, monkeypatch, route):
    target = tmp_path / "restored"
    if route == "app":
        original_read = Path.read_bytes
        reads = []
        def read_snapshot(path, *args, **kwargs):
            if path == backup.path:
                reads.append(path)
                if len(reads) > 1:
                    return b"SYNTHETIC changed archive must not be read"
            return original_read(path, *args, **kwargs)
        monkeypatch.setattr(Path, "read_bytes", read_snapshot)
        assert restore_backup(backup.path, PASSPHRASE, target) == len(PAYLOADS)
        assert len(reads) == 1
    else:
        result = run_standalone(backup, target, snapshot_guard=True)
        assert result.returncode == 0, result.stderr
        assert "archive snapshot reads: 1" in result.stdout
    for relative, data in PAYLOADS.items():
        assert (target / relative).read_bytes() == data
