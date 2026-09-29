"""Explicit release staging; authenticated metadata is reverified at this seam.

A StagedRelease is a receipt, not installation authority. Installation must
reverify signed inputs with installed trust and rehash the selected package.
"""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from enum import StrEnum
import hashlib
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import re
import stat
import ssl
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener
from urllib.parse import urlsplit, urlunsplit

from .releases import RefusalCode, ReleaseDescription, ReleaseRefusal, verify_release
from .runtime import worker_python


@dataclass(frozen=True, slots=True)
class LocalReleaseSource:
    """An explicitly selected local development directory."""
    directory: Path


@dataclass(frozen=True, slots=True)
class HttpsReleaseSource:
    """An explicitly selected HTTPS directory URL; no discovery or credentials."""
    channel_url: str


class StageCode(StrEnum):
    QUOTA_EXCEEDED = "quota_exceeded"
    STAGING_BUSY = "staging_busy"
    STAGING_REVIEW_REQUIRED = "staging_review_required"
    VERIFICATION_REFUSED = "verification_refused"
    SIZE_MISMATCH = "size_mismatch"
    HASH_MISMATCH = "hash_mismatch"
    IO_ERROR = "io_error"
    UNSAFE_PATH = "unsafe_path"
    CLEANUP_FAILED = "cleanup_failed"
    INVALID_SOURCE = "invalid_source"
    INVALID_REQUEST = "invalid_request"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    WORKER_FAILED = "worker_failed"
    RESPONSE_REFUSED = "response_refused"
    NETWORK_ERROR = "network_error"


@dataclass(frozen=True, slots=True)
class StageRefusal:
    code: StageCode
    verification_code: RefusalCode | None = None


@dataclass(frozen=True, slots=True)
class StagedRelease:
    release: ReleaseDescription
    raw_manifest: bytes
    detached_signature: bytes
    package_path: Path
    attempt_id: str


class _StageFailure(Exception):
    def __init__(self, code: StageCode):
        self.code = code


def _identity(info):
    return info.st_dev, info.st_ino


def _ordinary(info, *, directory=False):
    return (not stat.S_ISLNK(info.st_mode)
            and not getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            and (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)))


def _directory(path):
    path = Path(path)
    if ".." in path.parts or str(path).startswith("\\\\"):
        raise _StageFailure(StageCode.UNSAFE_PATH)
    path = path.absolute()
    if os.name == "nt":
        import ctypes
        if ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(path.anchor)) == 4:
            raise _StageFailure(StageCode.UNSAFE_PATH)
    for part in (*reversed(path.parents), path):
        if not _ordinary(part.lstat(), directory=True):
            raise _StageFailure(StageCode.UNSAFE_PATH)
    return path


def _owned(path, identity, *, directory=False):
    info = path.lstat()
    if not _ordinary(info, directory=directory) or _identity(info) != identity:
        raise _StageFailure(StageCode.UNSAFE_PATH)


def _https_location(channel, filename):
    if (type(channel) is not str or not 0 < len(channel) <= 2048
            or any(ord(character) < 33 or ord(character) > 126 for character in channel)
            or any(character in channel for character in "\\?#%")):
        raise _StageFailure(StageCode.INVALID_SOURCE)
    try:
        parts = urlsplit(channel)
        if (parts.scheme != "https" or not parts.hostname or "@" in parts.netloc
                or parts.port == 0 or ".." in parts.path.split("/")
                or re.fullmatch(r"[A-Za-z0-9/._~-]*", parts.path) is None):
            raise ValueError("Invalid channel")
    except ValueError:
        raise _StageFailure(StageCode.INVALID_SOURCE) from None
    return urlunsplit(("https", parts.netloc, parts.path.rstrip("/") + "/" + filename, "", ""))


def _cleanup(attempt, identity, owned):
    deadline = time.monotonic() + 2
    while True:
        try:
            _directory(attempt.parent)
            _owned(attempt, identity, directory=True)
            for path, file_identity in owned.items():
                try:
                    _owned(path, file_identity)
                except FileNotFoundError:
                    continue
                path.unlink()
            attempt.rmdir()
            return True
        except _StageFailure:
            return False
        except OSError as error:
            # Reaped Windows processes can leave brief sharing violations.
            # Every retry rechecks identities; unknown children are never removed.
            if getattr(error, "winerror", 0) not in (5, 32, 33) or time.monotonic() >= deadline:
                return False
            time.sleep(0.02)


_WORKER_CODES = (StageCode.IO_ERROR, StageCode.UNSAFE_PATH, StageCode.SIZE_MISMATCH,
                 StageCode.HASH_MISMATCH, StageCode.WORKER_FAILED,
                 StageCode.RESPONSE_REFUSED, StageCode.NETWORK_ERROR)


def _transfer(job):
    """Child owns blocking source reads/writes/fsync; it cannot publish a final."""
    temporary = Path(job["temporary"])
    _directory(temporary.parent)
    _owned(temporary, tuple(job["identity"]))
    digest, count = hashlib.sha256(), 0
    with _open_input(job) as incoming, temporary.open("r+b") as outgoing:
        if _identity(os.fstat(outgoing.fileno())) != tuple(job["identity"]):
            raise _StageFailure(StageCode.UNSAFE_PATH)
        while True:
            try:
                chunk = incoming.read(min(65536, job["size"] + 1 - count))
            except HTTPException:
                raise _StageFailure(StageCode.RESPONSE_REFUSED) from None
            except OSError:
                raise _StageFailure(StageCode.NETWORK_ERROR if job["kind"] == "https" else StageCode.IO_ERROR) from None
            if not chunk:
                break
            count += len(chunk)
            if count > job["size"]:
                raise _StageFailure(StageCode.SIZE_MISMATCH)
            digest.update(chunk)
            outgoing.write(chunk)
        if count != job["size"]:
            raise _StageFailure(StageCode.SIZE_MISMATCH)
        if digest.hexdigest() != job["sha256"]:
            raise _StageFailure(StageCode.HASH_MISMATCH)
        outgoing.flush()
        os.fsync(outgoing.fileno())


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def _open_input(job):
    if job["kind"] == "https":
        opener = build_opener(ProxyHandler({}), _NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))
        try:
            response = opener.open(Request(job["source"], headers={"Accept-Encoding": "identity"}), timeout=30)
        except HTTPError:
            raise _StageFailure(StageCode.RESPONSE_REFUSED) from None
        except (URLError, OSError, HTTPException):
            raise _StageFailure(StageCode.NETWORK_ERROR) from None
        try:
            lengths = response.headers.get_all("Content-Length", [])
            encodings = response.headers.get_all("Content-Encoding", [])
            transfers = response.headers.get_all("Transfer-Encoding", [])
            if (response.status != 200 or response.geturl() != job["source"]
                    or len(lengths) > 1 or len(encodings) > 1 or len(transfers) > 1
                    or (encodings and encodings[0].strip().lower() != "identity")
                    or (transfers and (transfers[0].strip().lower() != "chunked" or lengths))):
                raise _StageFailure(StageCode.RESPONSE_REFUSED)
            if lengths:
                if re.fullmatch(r"[0-9]{1,10}", lengths[0].strip()) is None:
                    raise _StageFailure(StageCode.RESPONSE_REFUSED)
                if int(lengths[0]) != job["size"]:
                    raise _StageFailure(StageCode.SIZE_MISMATCH)
            return response
        except BaseException:
            response.close()
            raise
    source_file = Path(job["source"])
    _directory(source_file.parent)
    if not _ordinary(source_file.lstat()):
        raise _StageFailure(StageCode.UNSAFE_PATH)
    incoming = source_file.open("rb")
    try:
        info = os.fstat(incoming.fileno())
        if not _ordinary(info):
            raise _StageFailure(StageCode.UNSAFE_PATH)
        if info.st_size != job["size"]:
            raise _StageFailure(StageCode.SIZE_MISMATCH)
        return incoming
    except BaseException:
        incoming.close()
        raise


def _run_transfer(job, deadline):
    env = {name: os.environ[name] for name in ("SystemRoot", "WINDIR", "TEMP", "TMP", "SSL_CERT_FILE", "SSL_CERT_DIR")
           if name in os.environ}
    try:
        interpreter = worker_python()
    except RuntimeError:
        raise _StageFailure(StageCode.WORKER_FAILED) from None
    command = [interpreter, "-I", "-B", "-c",
               "import runpy,sys; sys.path.insert(0,sys.argv[1]); "
               "runpy.run_module('vault_v2.release_stage',run_name='__main__')",
               str(Path(__file__).absolute().parent.parent)]
    if time.monotonic() >= deadline:
        raise _StageFailure(StageCode.DEADLINE_EXCEEDED)
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, cwd=Path(job["temporary"]).parent,
                               env=env, shell=False,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _StageFailure(StageCode.DEADLINE_EXCEEDED)
        try:
            process.communicate(json.dumps(job).encode("utf-8"), timeout=remaining)
        except subprocess.TimeoutExpired:
            raise _StageFailure(StageCode.DEADLINE_EXCEEDED) from None
        if process.returncode:
            code = (_WORKER_CODES[process.returncode - 1]
                    if 1 <= process.returncode <= len(_WORKER_CODES) else StageCode.WORKER_FAILED)
            raise _StageFailure(code)
    finally:
        if process.poll() is None:
            try:
                process.kill()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                raise _StageFailure(StageCode.CLEANUP_FAILED) from None
        if process.stdin is not None:
            process.stdin.close()


@contextmanager
def _staging_gate(parent):
    """Coordinate cooperating writers, not hostile code or legacy updaters."""
    if os.name == "nt":
        from .installation import InstallationBusy, _kernel
        try:
            kernel = _kernel()
        except (InstallationBusy, OSError):
            raise _StageFailure(StageCode.STAGING_BUSY) from None
        identity = _identity(parent.stat())
        name = "Global\\VaultV2-Staging-" + hashlib.sha256(repr(identity).encode()).hexdigest()
        handle = kernel.CreateMutexW(None, False, name)
        if not handle:
            raise _StageFailure(StageCode.STAGING_BUSY)
        acquired = False
        try:
            acquired = kernel.WaitForSingleObject(handle, 0) in (0, 0x80)
            if not acquired:
                raise _StageFailure(StageCode.STAGING_BUSY)
            yield
        finally:
            if acquired:
                kernel.ReleaseMutex(handle)
            kernel.CloseHandle(handle)
    else:
        import fcntl
        descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise _StageFailure(StageCode.STAGING_BUSY) from None
            yield
        finally:
            os.close(descriptor)


def _staging_capacity(parent, incoming_size):
    # Count is bounded without descending into or deleting earlier attempts.
    total = incoming_size
    with os.scandir(parent) as entries:
        for count, entry in enumerate(entries, 1):
            if count >= 4:
                raise _StageFailure(StageCode.QUOTA_EXCEEDED)
            if not entry.name.startswith("release-") or not _ordinary(entry.stat(follow_symlinks=False), directory=True):
                raise _StageFailure(StageCode.STAGING_REVIEW_REQUIRED)
            with os.scandir(entry.path) as children:
                child = next(children, None)
                if child is None or next(children, None) is not None:
                    raise _StageFailure(StageCode.STAGING_REVIEW_REQUIRED)
                info = child.stat(follow_symlinks=False)
                if (not _ordinary(info)
                        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", child.name) is None
                        or ".." in child.name or not child.name.endswith((".exe", ".apk"))
                        or re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", child.name.split(".", 1)[0].upper())):
                    raise _StageFailure(StageCode.STAGING_REVIEW_REQUIRED)
                total += info.st_size
                if total > 2 * 1024**3:
                    raise _StageFailure(StageCode.QUOTA_EXCEEDED)


def stage_release(
    raw_manifest: bytes, detached_signature: bytes, installed_public_key: bytes | None,
    expected_platform: str, installed_version_code: int, available_api_version: int, *,
    source: LocalReleaseSource | HttpsReleaseSource, staging_parent: Path,
    timeout_s: float = 60.0,
) -> StagedRelease | StageRefusal:
    started = time.monotonic()
    release = verify_release(raw_manifest, detached_signature, installed_public_key,
                             expected_platform, installed_version_code, available_api_version)
    if isinstance(release, ReleaseRefusal):
        return StageRefusal(StageCode.VERIFICATION_REFUSED, release.code)
    if type(timeout_s) not in (int, float) or not 0 < timeout_s <= 300 or not math.isfinite(timeout_s):
        return StageRefusal(StageCode.INVALID_REQUEST)
    try:
        parent = _directory(staging_parent)
        identity = _identity(parent.lstat())
        with _staging_gate(parent):
            _directory(parent)
            _owned(parent, identity, directory=True)
            _staging_capacity(parent, release.size)
            remaining = timeout_s - (time.monotonic() - started)
            if remaining <= 0:
                return StageRefusal(StageCode.DEADLINE_EXCEEDED)
            return _stage_release(raw_manifest, detached_signature, installed_public_key,
                expected_platform, installed_version_code, available_api_version,
                source=source, staging_parent=parent, timeout_s=remaining)
    except _StageFailure as failure:
        return StageRefusal(failure.code)
    except (OSError, TypeError, ValueError):
        return StageRefusal(StageCode.IO_ERROR)


def _stage_release(
    raw_manifest: bytes, detached_signature: bytes, installed_public_key: bytes | None,
    expected_platform: str, installed_version_code: int, available_api_version: int, *,
    source: LocalReleaseSource | HttpsReleaseSource, staging_parent: Path,
    timeout_s: float = 60.0,
) -> StagedRelease | StageRefusal:
    started = time.monotonic()
    release = verify_release(raw_manifest, detached_signature, installed_public_key,
                             expected_platform, installed_version_code, available_api_version)
    if isinstance(release, ReleaseRefusal):
        return StageRefusal(StageCode.VERIFICATION_REFUSED, release.code)
    if type(timeout_s) not in (int, float) or not 0 < timeout_s <= 300 or not math.isfinite(timeout_s):
        return StageRefusal(StageCode.INVALID_REQUEST)
    deadline = started + timeout_s
    attempt, identity, owned = None, None, {}
    try:
        if type(source) is LocalReleaseSource:
            directory = _directory(source.directory)
            source_file = directory / release.file
            if not _ordinary(source_file.lstat()):
                raise _StageFailure(StageCode.UNSAFE_PATH)
            kind, location = "local", str(source_file)
        elif type(source) is HttpsReleaseSource:
            kind, location = "https", _https_location(source.channel_url, release.file)
        else:
            raise _StageFailure(StageCode.INVALID_SOURCE)
        parent = _directory(staging_parent)
        attempt = Path(tempfile.mkdtemp(prefix="release-", dir=parent))
        identity = _identity(attempt.lstat())
        temporary = attempt / "package.part"
        with temporary.open("xb") as outgoing:
            owned[temporary] = _identity(os.fstat(outgoing.fileno()))
        _run_transfer({"kind": kind, "source": location, "temporary": str(temporary),
                       "identity": owned[temporary], "size": release.size,
                       "sha256": release.sha256}, deadline)
        if time.monotonic() >= deadline:
            raise _StageFailure(StageCode.DEADLINE_EXCEEDED)
        _directory(parent)
        _owned(attempt, identity, directory=True)
        _owned(temporary, owned[temporary])
        package = attempt / release.file
        os.link(temporary, package)
        owned[package] = owned[temporary]
        temporary.unlink()
        if time.monotonic() >= deadline:
            raise _StageFailure(StageCode.DEADLINE_EXCEEDED)
        return StagedRelease(release, raw_manifest, detached_signature, package, attempt.name)
    except _StageFailure as failure:
        refusal = StageRefusal(failure.code)
    except (OSError, TypeError, ValueError):
        refusal = StageRefusal(StageCode.IO_ERROR)
    if refusal.code == StageCode.CLEANUP_FAILED:
        return refusal
    if attempt is not None and not _cleanup(attempt, identity, owned):
        return StageRefusal(StageCode.CLEANUP_FAILED)
    return refusal


if __name__ == "__main__":
    try:
        request = sys.stdin.buffer.read(8193)
        if len(request) > 8192:
            raise _StageFailure(StageCode.WORKER_FAILED)
        _transfer(json.loads(request))
    except _StageFailure as failure:
        sys.exit(_WORKER_CODES.index(failure.code) + 1)
    except (OSError, ValueError, TypeError, KeyError):
        sys.exit(_WORKER_CODES.index(StageCode.IO_ERROR) + 1)
