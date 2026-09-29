"""Bounded private OCR child; no native OCR import in the window process."""
import json
from dataclasses import dataclass
import math
import os
from pathlib import Path
import subprocess
import threading
import time

from .door_process import _WindowsJob
from .child_temp import ChildCleanupError, temporary_child_directory
from .errors import VaultError
from .runtime import worker_python

MAX_INPUT = 32 * 1024 * 1024
MAX_OUTPUT = 1024 * 1024
ENGINE_VERSIONS = {"tesseract 5.5.2": "Tesseract 5.5.2", "tesseract 5.5.3": "Tesseract 5.5.3"}
ERRORS = {
    "models_missing": "Local OCR models are missing; install the pinned eng/rus files. No automatic download.",
    "models_changed": "Local OCR model SHA256 mismatch or linked model path; OCR is disabled.",
    "engine_unavailable": "Local Tesseract is not installed in this Python environment.",
    "engine_changed": "Local OCR requires pinned Tesseract 5.5.2 or 5.5.3; this engine version is refused.",
    "image_unreadable": "OCR page not read: image cannot be decoded (HEIC may need a local decoder).",
    "image_too_large": "OCR page not read: image exceeds the local pixel limit.",
    "multi_frame_image": "Multi-page or animated image is not supported; split frames into separate files. No frame was read.",
    "text_too_large": "OCR page not read: recognized text exceeds the local limit.",
    "worker_failed": "OCR page not read: local child failed.",
}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate child JSON member")
        result[key] = value
    return result


@dataclass(frozen=True)
class OCRResult:
    text: str
    version: str


def recognize_image(image_bytes: bytes, *, timeout_s: float = 20.0) -> str:
    """Compatibility text interface; use the result interface for provenance."""
    return recognize_image_result(image_bytes, timeout_s=timeout_s).text


def recognize_image_result(image_bytes: bytes, *, timeout_s: float = 20.0) -> OCRResult:
    try:
        valid_deadline = type(timeout_s) in (int, float) and math.isfinite(timeout_s) and timeout_s > 0
    except OverflowError:
        valid_deadline = False
    if (not isinstance(image_bytes, bytes) or not image_bytes or len(image_bytes) > MAX_INPUT
            or not valid_deadline):
        raise VaultError("OCR input or deadline is invalid.")
    try:
        raw = _run_child(image_bytes, timeout_s)
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except VaultError:
        raise
    except ChildCleanupError:
        raise VaultError("OCR temporary directory cleanup failed; local data may remain in temporary storage.") from None
    except Exception:
        raise VaultError("OCR page not read: local child failed.") from None
    if isinstance(result, dict) and set(result) == {"error"}:
        code = result["error"]
        message = ERRORS.get(code, ERRORS["worker_failed"]) if isinstance(code, str) else ERRORS["worker_failed"]
        raise VaultError(message)
    if (not isinstance(result, dict) or set(result) != {"text", "version"}
            or not isinstance(result["text"], str) or len(result["text"]) > 100000
            or not isinstance(result["version"], str) or result["version"] not in ENGINE_VERSIONS):
        raise VaultError("OCR page not read: invalid child result.")
    return OCRResult(result["text"], ENGINE_VERSIONS[result["version"]])


def _run_child(payload, timeout_s):
    deadline = time.monotonic() + timeout_s
    env = {name: os.environ[name] for name in ("SystemRoot", "WINDIR", "TEMP", "TMP") if name in os.environ}
    env.update(OMP_THREAD_LIMIT="1", PYTHONDONTWRITEBYTECODE="1")
    argv = [worker_python(), "-I", "-B", str(Path(__file__).with_name("ocr_worker.py").absolute())]
    with temporary_child_directory(prefix="vault-ocr-") as cwd:
        job = _WindowsJob() if os.name == "nt" else None
        process = None
        output, failed = bytearray(), threading.Event()
        workers = []
        try:
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
                    remaining = memoryview(payload)
                    while remaining:
                        written = process.stdin.write(remaining)
                        if not written:
                            failed.set()
                            break
                        remaining = remaining[written:]
                except OSError:
                    # A refused worker may exit before consuming its input.
                    pass
                finally:
                    process.stdin.close()

            workers = [threading.Thread(target=read_output, daemon=True),
                       threading.Thread(target=write_input, daemon=True)]
            for worker in workers:
                worker.start()
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    raise VaultError("OCR page not read: page deadline exceeded.")
                if failed.is_set():
                    raise VaultError("OCR page not read: child output limit exceeded.")
                time.sleep(0.01)
            if time.monotonic() >= deadline:
                raise VaultError("OCR page not read: page deadline exceeded.")
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
            raise VaultError("OCR page not read: local child failed.")
        return bytes(output)
