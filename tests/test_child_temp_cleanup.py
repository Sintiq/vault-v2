"""Owned child working directories have bounded, visible cleanup semantics."""
from contextlib import contextmanager
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

from PIL import Image
import pytest

from vault_v2.errors import VaultError
from vault_v2.pdf_preview import render_pdf_bytes_page
from vault_v2.ocr_process import recognize_image
from vault_v2.door_process import OneShotRunner, ProcessFailure
from vault_v2.agent_door import AgentCommand, AgentDoor, DoorError
from vault_v2.receipts import ReceiptLog


@contextmanager
def child_with_held_cwd(monkeypatch, script, *, hold_s=0.25, on_first_cleanup=None):
    """Inject only the executable and OS removal boundary, never Vault internals."""
    real_popen, real_rmdir = subprocess.Popen, os.rmdir
    temp_root = Path(tempfile.gettempdir()).resolve()
    children, directories, first_attempt = [], [], {}

    def launch(argv, **kwargs):
        directory = Path(kwargs["cwd"]).resolve()
        assert directory.parent == temp_root
        assert directory.name.startswith(("vault-pdf-", "vault-ocr-", "vault-agent-"))
        directories.append(directory)
        child = real_popen([sys.executable, "-I", "-B", "-c", script], **kwargs)
        children.append(child)
        return child

    def held_rmdir(path, *args, **kwargs):
        directory = Path(path).resolve()
        if directory in directories:
            now = time.monotonic()
            if directory not in first_attempt:
                first_attempt[directory] = now
                if on_first_cleanup is not None:
                    on_first_cleanup()
            if now - first_attempt[directory] < hold_s:
                raise PermissionError("synthetic owned child directory is still held")
        return real_rmdir(path, *args, **kwargs)

    try:
        with monkeypatch.context() as patched:
            patched.setattr(subprocess, "Popen", launch)
            patched.setattr(os, "rmdir", held_rmdir)
            yield children, directories, first_attempt
    finally:
        # Undo the synthetic OS hold before removing only the exact, empty cwd
        # created by the public operation. Never recursively remove a temp root.
        for directory in directories:
            assert directory.parent == temp_root
            assert directory.name.startswith(("vault-pdf-", "vault-ocr-", "vault-agent-"))
            if directory.exists():
                assert not list(directory.iterdir())
                real_rmdir(directory)


def pdf_reply_script():
    output = BytesIO()
    with Image.new("RGB", (400, 100), "white") as picture:
        picture.save(output, format="PNG")
    frame = json.dumps({"page_count": 1, "page_index": 0, "warning": ""}).encode() + b"\n" + output.getvalue()
    return "import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write(" + repr(frame) + ")"


def test_pdf_success_waits_for_transient_owned_directory_release(monkeypatch):
    with child_with_held_cwd(monkeypatch, pdf_reply_script()) as (children, directories, attempted):
        result = render_pdf_bytes_page(b"synthetic PDF input", width=400)
        assert result.page_count == 1 and result.page_index == 0
        assert children and all(child.poll() is not None for child in children)
        assert attempted and all(not directory.exists() for directory in directories)


def test_ocr_success_waits_for_transient_owned_directory_release(monkeypatch):
    script = 'import sys; sys.stdin.buffer.read(); print(\'{"text":"synthetic recognized text","version":"tesseract 5.5.3"}\')'
    with child_with_held_cwd(monkeypatch, script) as (children, directories, attempted):
        assert recognize_image(b"synthetic image input") == "synthetic recognized text"
        assert children and all(child.poll() is not None for child in children)
        assert attempted and all(not directory.exists() for directory in directories)


def test_agent_success_waits_for_transient_owned_directory_release(monkeypatch):
    script = 'import sys; sys.stdin.buffer.read(); print("synthetic agent reply")'
    with child_with_held_cwd(monkeypatch, script) as (children, directories, attempted):
        output = OneShotRunner().run([sys.executable, "-I", "-c", script], b"synthetic question",
                                     cancel=threading.Event(), timeout_s=5)
        assert output.strip() == b"synthetic agent reply"
        assert children and all(child.poll() is not None for child in children)
        assert attempted and all(not directory.exists() for directory in directories)


def test_pdf_persistent_cleanup_failure_refuses_otherwise_valid_output(monkeypatch):
    with child_with_held_cwd(monkeypatch, pdf_reply_script(), hold_s=float("inf")) as (children, directories, attempted):
        started = time.monotonic()
        with pytest.raises(VaultError, match="cleanup") as failure:
            render_pdf_bytes_page(b"synthetic PDF input", width=400)
        assert time.monotonic() - started < 6
        assert children and all(child.poll() is not None for child in children)
        assert attempted and all(directory.exists() for directory in directories)
        assert all(str(directory) not in str(failure.value) for directory in directories)
        assert "synthetic owned child directory" not in str(failure.value)


@pytest.mark.parametrize("route", ["ocr", "door"])
def test_persistent_cleanup_failure_refuses_otherwise_valid_text(monkeypatch, route):
    reply = ('{"text":"synthetic recognized text","version":"tesseract 5.5.3"}'
             if route == "ocr" else "synthetic agent reply")
    script = "import sys; sys.stdin.buffer.read(); print(" + repr(reply) + ")"
    with child_with_held_cwd(monkeypatch, script, hold_s=float("inf")) as (children, directories, attempted):
        started = time.monotonic()
        with pytest.raises(VaultError if route == "ocr" else ProcessFailure, match="cleanup") as failure:
            if route == "ocr":
                recognize_image(b"synthetic image input")
            else:
                OneShotRunner().run([sys.executable, "-I", "-c", script], b"synthetic question",
                                    cancel=threading.Event(), timeout_s=5)
        if route == "door":
            assert failure.value.status == "cleanup_failed"
        assert time.monotonic() - started < 6
        assert children and all(child.poll() is not None for child in children)
        assert attempted and all(directory.exists() for directory in directories)
        assert all(str(directory) not in str(failure.value) for directory in directories)
        assert "synthetic owned child directory" not in str(failure.value)


@pytest.mark.parametrize("route", ["pdf", "ocr", "door"])
@pytest.mark.parametrize("persistent", [False, True], ids=["transient-hold", "persistent-hold"])
def test_failed_child_still_removes_its_cwd_or_reports_cleanup_failure(monkeypatch, route, persistent):
    script = 'import sys; sys.stdin.buffer.read(); print("private unaccepted output"); sys.exit(17)'
    with child_with_held_cwd(monkeypatch, script, hold_s=float("inf") if persistent else 0.25) as (children, directories, attempted):
        started = time.monotonic()
        with pytest.raises(ProcessFailure if route == "door" else VaultError) as failure:
            if route == "pdf":
                render_pdf_bytes_page(b"synthetic PDF input", width=400)
            elif route == "ocr":
                recognize_image(b"synthetic image input")
            else:
                OneShotRunner().run([sys.executable, "-I", "-c", script], b"synthetic question",
                                    cancel=threading.Event(), timeout_s=5)
        assert time.monotonic() - started < 6
        assert ("cleanup" in str(failure.value)) is persistent
        if route == "door":
            assert failure.value.status == ("cleanup_failed" if persistent else "unusable")
        assert "private unaccepted output" not in str(failure.value)
        assert children and all(child.poll() is not None for child in children)
        assert attempted and all(directory.exists() is persistent for directory in directories)


@pytest.mark.parametrize("cancel_during_cleanup", [False, True], ids=["no-cancel", "late-cancel"])
def test_agent_cleanup_failure_is_audited_even_when_owner_cancels_during_cleanup(tmp_path, monkeypatch, cancel_during_cleanup):
    script = ('import sys; sys.stdin.buffer.read(); '
              'print(\'{"type":"result","subtype":"success","is_error":false,"result":"unaccepted synthetic reply"}\')')
    log = ReceiptLog(tmp_path / "synthetic-vault" / ".receipts")
    command = AgentCommand([sys.executable, "-I", "-B", "-c", script])
    door = AgentDoor(log, {}, command=lambda *_: command, timeout_s=5)
    door.select("claude")
    with child_with_held_cwd(monkeypatch, script, hold_s=float("inf"),
                             on_first_cleanup=door.cancel if cancel_during_cleanup else None) as (children, directories, attempted):
        with pytest.raises(DoorError, match="cleanup"):
            door.request("synthetic test", [{"role": "user", "content": "test cleanup"}])
        outcomes = [row for row in log.tail() if row["op"] == "agent_door_result"]
        assert len(outcomes) == 1
        assert outcomes[0]["extra"]["status"] == "cleanup_failed"
        assert outcomes[0]["sha256"] == ""
        assert "unaccepted synthetic reply" not in json.dumps(log.tail())
        assert children and all(child.poll() is not None for child in children)
        assert attempted and all(directory.exists() for directory in directories)
        assert not door.busy
