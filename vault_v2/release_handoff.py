"""Human-confirmed Windows installer handoff; staging receipts confer no authority.

Reauthenticate metadata using installed trust and check the actual file anew.
No migration, automatic rollback, process killing or network changes are allowed.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import hmac
import json
import os
from pathlib import Path
import stat
import subprocess

from .releases import RefusalCode, ReleaseDescription, ReleaseRefusal, verify_release
from .release_stage import StagedRelease
from .installation import InstallationBusy, InstallationSession, NAMESPACE


class HandoffCode(StrEnum):
    VERIFICATION_REFUSED = "verification_refused"
    INVALID_STAGE = "invalid_stage"
    PACKAGE_CHANGED = "package_changed"
    CONFIRMATION_REQUIRED = "confirmation_required"
    RUNTIME_BUSY_OR_UNKNOWN = "runtime_busy_or_unknown"
    LAUNCH_FAILED = "launch_failed"


@dataclass(frozen=True, slots=True)
class HandoffRefusal:
    code: HandoffCode
    verification_code: RefusalCode | None = None


@dataclass(frozen=True, slots=True)
class InstallOffer:
    """Display these exact authenticated fields; quote digest on the owner's press."""
    release: ReleaseDescription
    approval_digest: str


@dataclass(frozen=True, slots=True)
class InstallerStarted:
    pid: int
    release: ReleaseDescription
    state: str = field(default="started_unconfirmed", init=False)


class _Refused(Exception):
    def __init__(self, result: HandoffRefusal):
        self.result = result


def _plain_path(path: Path, *, directory: bool):
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise OSError("linked path")
        if part != path or directory:
            if not stat.S_ISDIR(info.st_mode):
                raise OSError("not a directory")
        elif not stat.S_ISREG(info.st_mode):
            raise OSError("not an ordinary file")


@contextmanager
def _read_locked(path: Path):
    """Windows denies file write/delete while this handle is retained through launch."""
    if os.name != "nt":
        raise OSError("Windows handoff unavailable")
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise OSError("cannot retain package")
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    with os.fdopen(fd, "rb") as stream:
        yield stream


@contextmanager
def _checked(staged, key, installed_version, api, parent):
    if type(staged) is not StagedRelease:
        raise _Refused(HandoffRefusal(HandoffCode.INVALID_STAGE))
    release = verify_release(staged.raw_manifest, staged.detached_signature, key,
                             "windows", installed_version, api)
    if isinstance(release, ReleaseRefusal):
        raise _Refused(HandoffRefusal(HandoffCode.VERIFICATION_REFUSED, release.code))
    parent, path = Path(parent).absolute(), Path(staged.package_path).absolute()
    if (".." in parent.parts or ".." in path.parts or str(parent).startswith("\\\\")
            or path.parent.parent != parent or path.parent.name != staged.attempt_id
            or not staged.attempt_id.startswith("release-") or path.name != release.file):
        raise _Refused(HandoffRefusal(HandoffCode.INVALID_STAGE))
    _plain_path(parent, directory=True)
    _plain_path(path, directory=False)
    with _read_locked(path) as stream:
        digest, total = hashlib.sha256(), 0
        while True:
            chunk = stream.read(min(65536, release.size + 1 - total))
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            if total > release.size:
                break
        if total != release.size or digest.hexdigest() != release.sha256:
            raise _Refused(HandoffRefusal(HandoffCode.PACKAGE_CHANGED))
        fields = [hashlib.sha256(staged.raw_manifest).hexdigest(),
                  hashlib.sha256(staged.detached_signature).hexdigest(), hashlib.sha256(key).hexdigest(),
                  os.path.normcase(str(path)), installed_version, api]
        approval = hashlib.sha256(json.dumps(fields, separators=(",", ":")).encode()).hexdigest()
        yield InstallOffer(release, approval), path


def inspect_staged_release(staged: StagedRelease, installed_public_key: bytes | None,
                           installed_version_code: int, available_api_version: int, *,
                           staging_parent: Path) -> InstallOffer | HandoffRefusal:
    """Read-only inspection. Any later installation must recheck these inputs."""
    try:
        with _checked(staged, installed_public_key, installed_version_code, available_api_version,
                      staging_parent) as (offer, _):
            return offer
    except _Refused as exc:
        return exc.result
    except (OSError, ValueError, TypeError):
        return HandoffRefusal(HandoffCode.INVALID_STAGE)


def _launch_installer(path: Path) -> int:
    return subprocess.Popen([str(path)], cwd=path.parent, close_fds=True, shell=False).pid


def handoff_release(staged: StagedRelease, installed_public_key: bytes | None,
                    installed_version_code: int, available_api_version: int, *, staging_parent: Path,
                    confirmed_digest: str, launch=None, namespace: str = NAMESPACE
                    ) -> InstallerStarted | HandoffRefusal:
    """Call only on an explicit human press quoting the displayed offer digest.

    ``launch`` substitutes only the external process seam in synthetic tests.
    A started process is NOT successful installation. Inno must re-acquire its own
    admission before writing files; an app starting after handoff causes refusal.
    """
    if type(confirmed_digest) is not str or len(confirmed_digest) != 64:
        return HandoffRefusal(HandoffCode.CONFIRMATION_REQUIRED)
    try:
        with InstallationSession("installer", namespace=namespace):
            with _checked(staged, installed_public_key, installed_version_code, available_api_version,
                          staging_parent) as (offer, path):
                if not hmac.compare_digest(confirmed_digest, offer.approval_digest):
                    return HandoffRefusal(HandoffCode.CONFIRMATION_REQUIRED)
                try:
                    pid = (launch or _launch_installer)(path)
                except OSError:
                    return HandoffRefusal(HandoffCode.LAUNCH_FAILED)
                if type(pid) is not int or pid <= 0:
                    return HandoffRefusal(HandoffCode.LAUNCH_FAILED)
                return InstallerStarted(pid, offer.release)
    except InstallationBusy:
        return HandoffRefusal(HandoffCode.RUNTIME_BUSY_OR_UNKNOWN)
    except _Refused as exc:
        return exc.result
    except (OSError, ValueError, TypeError):
        return HandoffRefusal(HandoffCode.INVALID_STAGE)
