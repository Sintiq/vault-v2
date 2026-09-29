"""Pending evidence stays visible until acknowledgement is safely recorded."""

import os
from pathlib import Path
import subprocess
import sys

import pytest

from vault_v2.errors import VaultError
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths


def pending_move(tmp_path):
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        with ops.log.write("fixture"):
            with ops.log.intent("move", {"src": ops.paths.staging / "example.txt",
                                         "dst": ops.paths.documents / "example.txt"}):
                raise RuntimeError("synthetic interruption")
    return ops


def test_acknowledgement_receipt_failure_keeps_evidence_visible(tmp_path, monkeypatch):
    ops = pending_move(tmp_path)
    rows = ops.log.pending()
    evidence = ops.log.dir / "pending" / rows[0]["id"]
    before = evidence.read_bytes()
    original_open = Path.open

    def fail_receipt(path, mode="r", *args, **kwargs):
        if path == ops.log.file and mode == "a":
            raise OSError("synthetic acknowledgement receipt fault")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_receipt)
    with pytest.raises(VaultError, match="may have applied"):
        ops.log.acknowledge_pending([rows[0]["id"]])

    assert ops.log.pending() == rows
    assert evidence.read_bytes() == before
    assert not (evidence.parent / "seen" / evidence.name).exists()


def test_acknowledgement_crash_before_archive_keeps_evidence_and_audit_receipt(tmp_path):
    ops = pending_move(tmp_path)
    rows = ops.log.pending()
    program = '''
import os, sys
from pathlib import Path
from vault_v2.receipts import ReceiptLog
log = ReceiptLog(Path(sys.argv[1]) / ".receipts")
def interrupted(path, target):
    os._exit(23)
Path.rename = interrupted
log.acknowledge_pending([log.pending()[0]["id"]])
'''
    child = subprocess.run([sys.executable, "-B", "-c", program, str(ops.paths.root)],
                           capture_output=True, text=True, timeout=10)
    assert child.returncode == 23, child.stderr
    assert ops.log.pending() == rows
    assert ops.log.tail(1)[0]["op"] == "pending_acknowledged"
    assert ops.log.acknowledge_pending([rows[0]["id"]]) == 1
    assert ops.log.pending() == []
    assert ops.log.verify() == 2


def directory_link(link, target):
    if os.name == "nt":
        quote = lambda p: "'" + str(p).replace("'", "''") + "'"
        command = ("$ErrorActionPreference = 'Stop'; New-Item -ItemType Junction -Path "
                   + quote(link) + " -Target " + quote(target) + " | Out-Null")
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                                capture_output=True, text=True, timeout=10,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode == 0, result.stderr
    else:
        link.symlink_to(target, target_is_directory=True)


def test_linked_seen_directory_refuses_acknowledgement_without_external_write(tmp_path):
    ops = pending_move(tmp_path)
    rows = ops.log.pending()
    pending = ops.log.dir / "pending"
    outside = tmp_path / "outside"
    outside.mkdir()
    directory_link(pending / "seen", outside)

    with pytest.raises(VaultError):
        ops.log.acknowledge_pending([rows[0]["id"]])

    assert (pending / rows[0]["id"]).is_file()
    assert list(outside.iterdir()) == []
    assert ops.log.tail() == []


def test_linked_pending_directory_is_visible_without_reading_its_target(tmp_path):
    ops = pending_move(tmp_path)
    pending = ops.log.dir / "pending"
    outside = tmp_path / "outside"
    pending.rename(outside)
    directory_link(pending, outside)
    before = {p.name: p.read_bytes() for p in outside.iterdir()}

    rows = ops.log.pending()

    assert len(rows) == 1
    assert rows[0]["op"] == "unknown"
    assert rows[0]["paths"] == {}
    assert "manual review" in rows[0]["error"]
    with pytest.raises(VaultError):
        ops.mkdir(ops.paths.documents, "forbidden")
    assert not (ops.paths.documents / "forbidden").exists()
    assert {p.name: p.read_bytes() for p in outside.iterdir()} == before
