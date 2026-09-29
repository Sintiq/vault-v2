"""Root writing contract, observed through file operations and receipt log."""
import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from vault_v2.ops import VaultError, VaultOps
from vault_v2.paths import VaultPaths


def tree(root):
    return {str(p.relative_to(root)): p.read_bytes() if p.is_file() else None
            for p in root.rglob("*")}


def test_invalid_journal_refuses_mkdir_before_any_data_change(tmp_path):
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    ops.log.append("fixture", "", "")
    row = json.loads(ops.log.file.read_text(encoding="utf-8"))
    row["note"] = "synthetic tamper"
    ops.log.file.write_text(json.dumps(row) + "\n", encoding="utf-8")
    before = ops.log.file.read_bytes()

    with pytest.raises((VaultError, ValueError)):
        ops.mkdir(ops.paths.documents, "must-not-exist")

    assert not (ops.paths.documents / "must-not-exist").exists()
    assert ops.log.file.read_bytes() == before


@pytest.mark.parametrize("operation", ["import", "copy", "move", "trash", "restore", "purge", "clear"])
def test_invalid_journal_refuses_each_file_operation_without_changes(tmp_path, operation):
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    source = ops.paths.staging / "synthetic.txt"
    source.write_text("keep exact bytes", encoding="utf-8")
    slot = ops.trash(source).dst if operation in ("restore", "purge") else None
    if not ops.log.file.exists():
        ops.log.append("fixture", "", "")
    ops.log.file.write_text('{"broken": true}\n', encoding="utf-8")
    before = tree(ops.paths.root)
    run = {
        "import": lambda: ops.import_file(source, "documents"),
        "copy": lambda: ops.copy(source, ops.paths.documents),
        "move": lambda: ops.move(source, ops.paths.documents),
        "trash": lambda: ops.trash(source),
        "restore": lambda: ops.restore(slot),
        "purge": ops.purge_trash,
        "clear": ops.clear_staging,
    }[operation]
    with pytest.raises((VaultError, ValueError, TypeError)):
        run()
    assert tree(ops.paths.root) == before


def test_move_receipt_failure_keeps_intent_and_blocks_next_write(tmp_path, monkeypatch):
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    source = ops.paths.staging / "synthetic.txt"
    source.write_bytes(b"abc")
    real_open = Path.open

    def fail_receipt(path, mode="r", *args, **kwargs):
        if path == ops.log.file and mode == "a":
            raise OSError("synthetic disk fault")
        return real_open(path, mode, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", fail_receipt)
        with pytest.raises(VaultError, match="may have applied"):
            ops.move(source, ops.paths.documents)

    assert not source.exists()
    assert (ops.paths.documents / source.name).read_bytes() == b"abc"
    rows = ops.log.pending()
    assert len(rows) == 1
    assert rows[0]["op"] == "move"
    assert rows[0]["paths"]["dst"] == str(ops.paths.documents / source.name)
    assert rows[0]["expected_hashes"]["src"] == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    with pytest.raises(VaultError, match="blocked until restart"):
        ops.mkdir(ops.paths.personal, "forbidden")
    assert not (ops.paths.personal / "forbidden").exists()


@pytest.mark.parametrize("operation", ["import", "copy", "trash", "restore", "purge", "clear", "mkdir"])
def test_each_file_operation_leaves_one_intent_when_receipt_fails(tmp_path, monkeypatch, operation):
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    source = ops.paths.staging / "synthetic.txt"
    source.write_bytes(b"abc")
    slot = ops.trash(source).dst if operation in ("restore", "purge") else None
    run = {
        "import": lambda: ops.import_file(source, "documents"),
        "copy": lambda: ops.copy(source, ops.paths.documents),
        "trash": lambda: ops.trash(source),
        "restore": lambda: ops.restore(slot),
        "purge": ops.purge_trash,
        "clear": ops.clear_staging,
        "mkdir": lambda: ops.mkdir(ops.paths.documents, "new"),
    }[operation]
    real_open = Path.open

    def fault(path, mode="r", *args, **kwargs):
        if path == ops.log.file and mode == "a":
            raise OSError("synthetic append failure")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fault)
    with pytest.raises(VaultError, match="may have applied"):
        run()
    rows = ops.log.pending()
    assert len(rows) == 1
    assert rows[0]["op"] == {"purge": "purge_trash", "clear": "clear_staging"}.get(operation, operation)
    assert rows[0]["paths"]


def test_process_death_after_move_is_shown_only_then_explicitly_acknowledged(tmp_path):
    paths = VaultPaths(tmp_path / "vault").ensure()
    source = paths.staging / "synthetic.txt"
    source.write_bytes(b"abc")
    program = '''
import os, shutil, sys
from pathlib import Path
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths
ops = VaultOps(VaultPaths(Path(sys.argv[1])))
move = shutil.move
def interrupted(*args, **kwargs):
    move(*args, **kwargs)
    os._exit(23)
shutil.move = interrupted
ops.move(ops.paths.staging / "synthetic.txt", ops.paths.documents)
'''
    child = subprocess.run([sys.executable, "-B", "-c", program, str(paths.root)],
                           capture_output=True, text=True, timeout=20)
    assert child.returncode == 23, child.stderr
    before = tree(paths.root)
    reopened = VaultOps(paths)
    rows = reopened.log.pending()
    assert len(rows) == 1 and rows[0]["op"] == "move"
    assert tree(paths.root) == before
    assert not source.exists()
    assert (paths.documents / source.name).read_bytes() == b"abc"
    evidence = paths.receipts / "pending" / rows[0]["id"]
    exact_bytes = evidence.read_bytes()

    assert reopened.log.acknowledge_pending([rows[0]["id"]]) == 1
    assert reopened.log.pending() == []
    assert not evidence.exists()
    assert (evidence.parent / "seen" / evidence.name).read_bytes() == exact_bytes
    assert reopened.log.tail(1)[0]["op"] == "pending_acknowledged"
    assert reopened.log.verify() == 1
    assert list(paths.documents.iterdir()) == [paths.documents / source.name]


def test_nested_clear_staging_completes_with_one_outer_intent(tmp_path, monkeypatch):
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    for name in ("a.txt", "b.txt"):
        (ops.paths.staging / name).write_bytes(b"abc")
    real_open = Path.open
    seen = []

    def observe(path, mode="r", *args, **kwargs):
        if path == ops.log.file and mode == "a":
            seen.append(ops.log.pending())
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", observe)
    assert ops.clear_staging() == 2
    assert [len(rows) for rows in seen] == [1, 1]
    assert seen[0][0]["id"] == seen[1][0]["id"]
    assert seen[0][0]["op"] == "clear_staging"
    assert ops.log.pending() == []
    assert ops.log.verify() == 2


def test_malformed_intent_is_visible_and_no_startup_mutation(tmp_path):
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    pending = ops.paths.receipts / "pending"
    pending.mkdir()
    (pending / "broken.json").write_bytes(b"not JSON")
    before = tree(ops.paths.root)
    rows = ops.log.pending()
    assert rows == [{"id": "broken.json", "op": "unknown", "paths": {}, "expected_hashes": {},
                     "ts": "unknown", "error": "unreadable intention — manual review needed"}]
    assert tree(ops.paths.root) == before


def test_large_folder_intention_keeps_operation_and_paths_readable(tmp_path, monkeypatch):
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    folder = ops.paths.staging / "synthetic-folder"
    folder.mkdir()
    for number in range(700):
        (folder / f"document-{number:04d}-{'x' * 40}.txt").write_bytes(b"abc")
    real_open = Path.open

    def fault(path, mode="r", *args, **kwargs):
        if path == ops.log.file and mode == "a":
            raise OSError("synthetic receipt failure")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fault)
    with pytest.raises(VaultError, match="may have applied"):
        ops.move(folder, ops.paths.documents)
    rows = ops.log.pending()
    assert len(rows) == 1 and rows[0]["op"] == "move"
    assert rows[0]["paths"]["src"] == str(folder)
    assert "error" not in rows[0]


def test_ui_admission_refuses_busy_reads_and_writes_without_waiting(tmp_path):
    from vault_v2.errors import VaultBusy
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    entered, release = threading.Event(), threading.Event()

    def snapshot():
        with ops.log.write("backup snapshot"):
            entered.set()
            assert release.wait(5)

    worker = threading.Thread(target=snapshot)
    worker.start()
    assert entered.wait(2)
    try:
        with ops.log.prefer_nonblocking():
            for read in (ops.log.verify, ops.log.pending, ops.log.tail):
                with pytest.raises(VaultBusy):
                    read()
            with pytest.raises(VaultBusy):
                ops.mkdir(ops.paths.staging, "no-wait")
    finally:
        release.set()
        worker.join(timeout=2)
    assert not (ops.paths.staging / "no-wait").exists()
    assert ops.log.verify() == 0
