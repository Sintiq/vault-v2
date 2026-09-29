"""Runtime ownership and honest read-only UI, on synthetic roots only."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from vault_v2.runtime import RuntimeLease


def test_one_runtime_per_root_even_with_two_windows_in_same_process(tmp_path: Path) -> None:
    root = tmp_path / "synthetic-vault"
    first = RuntimeLease(root)
    try:
        assert not first.read_only
        metadata = json.loads((root / ".vault.lock").read_text(encoding="utf-8"))
        assert metadata["pid"] == os.getpid()
        assert metadata["started_at"] and metadata["machine"]
        before = (root / ".vault.lock").read_bytes()
        second = RuntimeLease(root)
        try:
            assert second.read_only
            assert "another Vault window" in second.reason
            assert (root / ".vault.lock").read_bytes() == before
        finally:
            second.close()
    finally:
        first.close()

    reopened = RuntimeLease(root)
    try:
        assert not reopened.read_only
        assert reopened.reclaimed is None
    finally:
        reopened.close()


def test_dead_runtime_lock_is_reclaimed_but_live_pid_is_not(tmp_path: Path) -> None:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=10)
    root = tmp_path / "synthetic-vault"
    root.mkdir()
    metadata = {"pid": child.pid, "started_at": "2026-09-22T18:00:00+00:00",
                "machine": socket.gethostname(), "state": "active"}
    (root / ".vault.lock").write_text(json.dumps(metadata), encoding="utf-8")
    lease = RuntimeLease(root)
    try:
        assert not lease.read_only
        assert lease.reclaimed == metadata
    finally:
        lease.close()

    metadata["pid"] = os.getpid()
    (root / ".vault.lock").write_text(json.dumps(metadata), encoding="utf-8")
    before = (root / ".vault.lock").read_bytes()
    lease = RuntimeLease(root)
    try:
        assert lease.read_only
        assert (root / ".vault.lock").read_bytes() == before
    finally:
        lease.close()


@pytest.mark.parametrize("metadata", [
    {"state": "released"},
    {"pid": 1, "machine": "another-machine", "started_at": "2026-09-22T18:00:00+00:00", "state": "released"},
    {"pid": True, "machine": socket.gethostname(), "started_at": "not a time", "state": "released"},
    [], None,
])
def test_unknown_lock_ownership_is_read_only_and_preserved(tmp_path: Path, metadata) -> None:
    root = tmp_path / "synthetic-vault"
    root.mkdir()
    path = root / ".vault.lock"
    path.write_text(json.dumps(metadata), encoding="utf-8")
    before = path.read_bytes()
    lease = RuntimeLease(root)
    try:
        assert lease.read_only
        assert path.read_bytes() == before
    finally:
        lease.close()


def test_failed_clean_release_keeps_exclusive_ownership(tmp_path: Path, monkeypatch) -> None:
    lease = RuntimeLease(tmp_path / "synthetic-vault")
    real_fsync = os.fsync

    def fail_sync(fd):
        raise OSError("synthetic release persistence failure")

    monkeypatch.setattr(os, "fsync", fail_sync)
    try:
        with pytest.raises(OSError):
            lease.close()
        monkeypatch.setattr(os, "fsync", real_fsync)
        contender = RuntimeLease(lease.root)
        try:
            assert contender.read_only, "failed release must retain the OS lock and in-process ownership"
        finally:
            contender.close()
    finally:
        monkeypatch.setattr(os, "fsync", real_fsync)
        lease.close()


@pytest.fixture()
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    application = QApplication.instance() or QApplication([])
    application.setQuitOnLastWindowClosed(False)
    return application


def test_second_window_is_read_only_without_model_phone_or_directory_writes(tmp_path: Path, monkeypatch, app) -> None:
    from vault_v2.main import MainWindow

    root = tmp_path / "synthetic-vault"
    lease = RuntimeLease(root)
    (root / "vault.json").write_text(json.dumps({"agent_backend": "unsupported"}), encoding="utf-8")
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}

    def forbidden(*args, **kwargs):
        pytest.fail("a read-only window must not contact model, phone, or external processes")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    window = None
    try:
        window = MainWindow(root)
        app.processEvents()
        assert window.read_only
        assert "read-only: another Vault window holds this vault" in window.windowTitle()
        assert window.api_server is None
        assert not any(action.isEnabled() for action in window.write_actions)
        assert not window.panes["staging"].view.acceptDrops()
        assert not window.panes["staging"].view.dragEnabled()
        assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
        assert not any(p.is_dir() for p in root.iterdir()), "read-only constructors must not create service folders"
    finally:
        if window is not None:
            window.close()
        lease.close()


def _pending_root(tmp_path: Path) -> tuple[Path, Path, bytes]:
    root = tmp_path / "synthetic-vault"
    pending = root / ".receipts" / "pending" / "synthetic-op.json"
    pending.parent.mkdir(parents=True)
    payload = json.dumps({"op": "move", "paths": {"src": "staging/from.txt", "dst": "documents/to.txt"},
                          "expected_hashes": {}, "ts": "2026-09-22T18:00:00+00:00"}).encode()
    pending.write_bytes(payload)
    (root / "vault.json").write_text(json.dumps({"agent_backend": "unsupported"}), encoding="utf-8")
    return root, pending, payload


def test_startup_pending_dialog_does_nothing_until_explicit_acknowledgement(tmp_path: Path, app) -> None:
    from vault_v2.main import MainWindow
    root, pending, payload = _pending_root(tmp_path)
    window = MainWindow(root, start_services=False)
    try:
        app.processEvents()
        dialog = window.pending_dialog
        assert dialog is not None
        assert "may have applied" in dialog.windowTitle()
        assert "move" in dialog.details.toPlainText()
        assert "staging/from.txt" in dialog.details.toPlainText()
        assert "documents/to.txt" in dialog.details.toPlainText()
        assert pending.read_bytes() == payload
        assert window.ops.log.tail() == []
        dialog.reject()
        assert pending.read_bytes() == payload
        assert window.ops.log.tail() == []
        window.show_pending()
        window.pending_dialog.ack_btn.click()
        app.processEvents()
        assert not pending.exists()
        assert (pending.parent / "seen" / pending.name).read_bytes() == payload
        assert window.ops.log.tail()[-1]["op"] == "pending_acknowledged"
        assert not (root / "documents" / "to.txt").exists(), "acknowledgement must never replay the operation"
    finally:
        window.close()


def test_window_keeps_lease_and_shows_busy_while_a_writer_is_running(tmp_path: Path, app) -> None:
    from PySide6.QtTest import QTest
    from vault_v2.main import MainWindow
    root = tmp_path / "synthetic-vault"
    root.mkdir()
    (root / "vault.json").write_text(json.dumps({"agent_backend": "unsupported"}), encoding="utf-8")
    window = MainWindow(root, start_services=False)
    entered, finish = threading.Event(), threading.Event()

    def work():
        with window.ops.log.write("backup snapshot"):
            entered.set()
            finish.wait(5)

    worker = threading.Thread(target=work)
    worker.start()
    try:
        assert entered.wait(3)
        QTest.qWait(150)
        assert "busy: backup snapshot" in window.guard_status.text()
        assert not any(action.isEnabled() for action in window.write_actions)
        window.close()
        assert json.loads((root / ".vault.lock").read_text(encoding="utf-8"))["state"] == "active"
    finally:
        finish.set()
        worker.join(timeout=5)
        window.close()


def test_reclaimed_lock_is_receipted_before_normal_window_writes(tmp_path: Path, app) -> None:
    from vault_v2.main import MainWindow
    root = tmp_path / "synthetic-vault"
    root.mkdir()
    (root / "vault.json").write_text(json.dumps({"agent_backend": "unsupported"}), encoding="utf-8")
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=10)
    (root / ".vault.lock").write_text(json.dumps({
        "pid": child.pid, "machine": socket.gethostname(),
        "started_at": "2026-09-22T18:00:00+00:00", "state": "active",
    }), encoding="utf-8")
    window = MainWindow(root, start_services=False)
    try:
        assert not window.read_only
        assert window.ops.log.tail()[0]["op"] == "runtime_lock_reclaimed"
        assert window.ops.log.tail()[0]["extra"]["pid"] == child.pid
        assert window.ops.log.verify() == 1
    finally:
        window.close()


def test_backup_dialog_keeps_worker_alive_and_visible_until_work_finishes(tmp_path: Path, monkeypatch, app) -> None:
    from PySide6.QtTest import QTest
    from vault_v2.backup_dialog import BackupDialog
    from vault_v2.ops import VaultOps
    from vault_v2.paths import VaultPaths

    ops = VaultOps(VaultPaths(tmp_path / "synthetic-vault"))
    source = ops.paths.documents / "synthetic.txt"
    source.write_bytes(b"synthetic backup bytes")
    monkeypatch.setattr("vault_v2.backup_dialog.DEFAULT_DEST", tmp_path / "synthetic-backups")
    entered, finish = threading.Event(), threading.Event()
    real_open = Path.open
    worker_threads = []

    class Reader:
        def __enter__(self):
            self.stream = real_open(source, "rb")
            return self
        def __exit__(self, *args):
            self.stream.close()
        def read(self, size=-1):
            worker_threads.append(threading.get_ident())
            entered.set()
            assert finish.wait(5)
            return self.stream.read(size)

    def open_file(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        return Reader() if path == source and mode == "rb" else real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    dialog = BackupDialog(ops.paths.root, ops.log)
    dialog.pass1.setText("synthetic-password")
    dialog.pass2.setText("synthetic-password")
    dialog.show()
    dialog.write_btn.click()
    try:
        assert entered.wait(3)
        QTest.qWait(30)
        assert "writing" in dialog.status.text()
        assert not dialog.write_btn.isEnabled()
        dialog.reject()
        assert dialog.isVisible(), "closing must not destroy a running backup worker"
        assert set(worker_threads) and threading.get_ident() not in worker_threads
    finally:
        finish.set()
        for _ in range(100):
            QTest.qWait(20)
            if dialog._thread is None:
                break
        assert dialog._thread is None
        dialog.close()


def test_existing_backup_verification_does_not_run_on_gui_thread(tmp_path: Path, monkeypatch, app) -> None:
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QFileDialog
    from vault_v2.backup import make_backup
    from vault_v2.backup_dialog import BackupDialog
    from vault_v2.ops import VaultOps
    from vault_v2.paths import VaultPaths

    ops = VaultOps(VaultPaths(tmp_path / "synthetic-vault"))
    (ops.paths.documents / "synthetic.txt").write_bytes(b"synthetic backup")
    backup = make_backup(ops.paths.root, tmp_path / "backups", "synthetic-password")
    monkeypatch.setattr("vault_v2.backup_dialog.DEFAULT_DEST", tmp_path / "backups")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **kw: (str(backup.path), ""))
    real_open = Path.open
    threads = []

    def open_file(path, *args, **kwargs):
        if path == backup.path:
            threads.append(threading.get_ident())
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    dialog = BackupDialog(ops.paths.root, ops.log)
    dialog.pass1.setText("synthetic-password")
    try:
        dialog.check_btn.click()
        for _ in range(100):
            QTest.qWait(20)
            if dialog._thread is None:
                break
        assert dialog._thread is None
        assert "OK" in dialog.status.text()
        assert threads and threading.get_ident() not in threads
    finally:
        dialog.close()
