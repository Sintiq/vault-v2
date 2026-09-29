"""Bounded PDF children; admission scheduling is not a native DLL lock.

No owner paths or credentials reach the child. Job cleanup is crash containment,
not a hostile-code security sandbox (same cleanup scope as the OCR child).
"""
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import subprocess
import threading
import time

from .door_process import _WindowsJob
from .child_temp import ChildCleanupError, temporary_child_directory
from .errors import VaultError
from .file_access import visible_file
from .runtime import worker_python

MAX_INPUT = 512 * 1024 * 1024
MAX_OUTPUT = 64 * 1024 * 1024
_ADMISSION = threading.BoundedSemaphore(1)


class PdfPreviewBusy(VaultError):
    def __init__(self):
        super().__init__("busy")


def _check(deadline, cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise VaultError("PDF operation cancelled.")
    if time.monotonic() >= deadline:
        raise VaultError("PDF page not shown: page deadline exceeded.")


@contextmanager
def _admit(deadline, cancel_event, wait):
    _check(deadline, cancel_event)
    acquired = _ADMISSION.acquire(blocking=False)
    if not acquired and not wait:
        raise PdfPreviewBusy()
    while not acquired:
        _check(deadline, cancel_event)
        acquired = _ADMISSION.acquire(timeout=0.01)
    try:
        _check(deadline, cancel_event)
        yield
    finally:
        _ADMISSION.release()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate child JSON member")
        result[key] = value
    return result


def pdf_request(source, operation, *, page=0, width=1200, wait=True,
                cancel_event=None, timeout_s=10.0):
    """One info/render/text-page request; source is bytes or (paths, path)."""
    try:
        valid = type(timeout_s) in (int, float) and math.isfinite(timeout_s) and 0 < timeout_s <= 10
    except OverflowError:
        valid = False
    if not valid:
        raise VaultError("PDF deadline is invalid (maximum 10 seconds).")
    deadline = time.monotonic() + timeout_s
    with _admit(deadline, cancel_event, wait):
        if isinstance(source, tuple):
            paths, path = source
            checked = visible_file(paths, path)
            if checked.suffix.lower() != ".pdf":
                raise VaultError("Only PDF files can be rendered as PDF pages.")
            try:
                if checked.stat().st_size > MAX_INPUT:
                    raise VaultError("PDF input exceeds the 512 MiB limit.")
                with checked.open("rb") as stream:
                    data = stream.read(MAX_INPUT + 1)
            except OSError:
                raise VaultError("PDF file cannot be read.") from None
        else:
            data = source
        if not isinstance(data, bytes) or not data or len(data) > MAX_INPUT:
            raise VaultError("PDF input is empty or exceeds the 512 MiB limit.")
        _check(deadline, cancel_event)
        request = json.dumps({"operation": operation, "page": page, "width": width}).encode("ascii") + b"\n"
        try:
            raw = _run_child(request, data, deadline, cancel_event)
            header, separator, body = raw.partition(b"\n")
            if not separator or len(header) > 4096:
                raise ValueError("invalid child framing")
            metadata = json.loads(header.decode("utf-8"), object_pairs_hook=_unique_object)
            if not isinstance(metadata, dict):
                raise ValueError("invalid child metadata")
            if set(metadata) == {"error"}:
                messages = {
                    "decode": "This PDF cannot be decoded here.",
                    "page": "PDF page is out of range.",
                    "geometry": "PDF page geometry is invalid.",
                    "tall": "PDF page is too tall for a safe preview.",
                    "text_limit": "PDF page text exceeds the local extraction limit.",
                    "password": "PDF requires an opening password; text was not extracted.",
                    "forms": "PDF form values could not be read safely; text was not extracted.",
                }
                code = metadata["error"]
                raise VaultError(messages.get(code, "PDF page not shown: local child failed.")
                                 if isinstance(code, str) else "PDF page not shown: local child failed.")
            return metadata, body
        except VaultError:
            raise
        except ChildCleanupError:
            raise VaultError("PDF temporary directory cleanup failed; local data may remain in temporary storage.") from None
        except Exception:
            raise VaultError("PDF page not shown: local child failed.") from None


def _run_child(header, payload, deadline, cancel_event):
    env = {name: os.environ[name] for name in ("SystemRoot", "WINDIR", "TEMP", "TMP") if name in os.environ}
    env.update(OMP_THREAD_LIMIT="1", PYTHONDONTWRITEBYTECODE="1")
    argv = [worker_python(), "-I", "-B", str(Path(__file__).with_name("pdf_worker.py").absolute())]
    with temporary_child_directory(prefix="vault-pdf-") as cwd:
        job = _WindowsJob() if os.name == "nt" else None
        process = None
        output, failed = bytearray(), threading.Event()
        workers = []
        try:
            _check(deadline, cancel_event)
            process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, cwd=cwd, env=env, shell=False,
                                       bufsize=0, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if job is not None:
                job.assign(process)

            def read_output():
                try:
                    while chunk := process.stdout.read(8192):
                        if len(output) + len(chunk) > MAX_OUTPUT:
                            failed.set()
                            return
                        output.extend(chunk)
                except OSError:
                    failed.set()

            def write_input():
                try:
                    for part in (header, payload):
                        remaining = memoryview(part)
                        while remaining:
                            if cancel_event is not None and cancel_event.is_set():
                                return
                            written = process.stdin.write(remaining)
                            if not written:
                                failed.set()
                                return
                            remaining = remaining[written:]
                except OSError:
                    pass  # A refused worker can exit before consuming all input.
                finally:
                    process.stdin.close()

            workers = [threading.Thread(target=read_output, daemon=True),
                       threading.Thread(target=write_input, daemon=True)]
            for worker in workers:
                worker.start()
            while process.poll() is None:
                _check(deadline, cancel_event)
                if failed.is_set():
                    raise VaultError("PDF page not shown: child output limit exceeded.")
                time.sleep(0.01)
            _check(deadline, cancel_event)
        finally:
            try:
                if job is not None:
                    job.stop_and_wait()
                elif process is not None and process.poll() is None:
                    process.kill()
            finally:
                if job is not None:
                    job.close()
                if process is not None:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
                    for worker in workers:
                        if worker.ident is not None:
                            worker.join(timeout=2)
                    process.stdin.close()
                    process.stdout.close()
        if failed.is_set() or any(worker.is_alive() for worker in workers) or process.returncode != 0:
            raise VaultError("PDF page not shown: local child failed.")
        return bytes(output)
