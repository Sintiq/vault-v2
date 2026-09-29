"""A retained OS byte lock grants one writable desktop runtime per root.

The HTTP server shares its window's lease; it never acquires another one.
A clean close marks the retained file released while still holding the lock.
It does not unlink after unlocking, which could remove a new owner's lease.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path


READ_ONLY_REASON = "read-only: another Vault window holds this vault"
_owners: dict[str, "RuntimeLease"] = {}
_owners_lock = threading.RLock()
_LEASE_BYTE = 64 * 1024  # outside the small JSON record so it remains readable


def _installed_layout() -> tuple[Path, dict] | None:
    """Recognize our fixed embedded layout, without consulting PATH or cwd.

    This is packaging metadata, not a signature or an integrity attestation.
    A present but broken marker is an error, never a development fallback.
    """
    directory = Path(__file__).resolve().parent.parent
    marker = directory / "vault-install.json"
    if not marker.exists() and not marker.is_symlink():
        # Embedded CPython is not a frozen executable. Its fixed interpreter
        # placement must never silently become a development/data-in-program mode.
        if (getattr(sys, "frozen", False)
                or Path(sys.executable).absolute().parent == directory / "python"):
            raise RuntimeError("Vault install marker is missing; repair the installation.")
        return None
    try:
        if marker.is_symlink() or marker.is_junction():
            raise ValueError("linked marker")
        with marker.open("rb") as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ValueError("oversized marker")
        value = json.loads(raw.decode("utf-8"))
        if (not isinstance(value, dict) or value.get("schema") != "vault-v2-install@1"
                or value.get("worker_python") != "python/python.exe"):
            raise ValueError("unsupported layout")
        program_root = value.get("program_root", ".")
        if program_root not in (".", "../.."):
            raise ValueError("unsupported program root")
        # A legacy flat payload may protect itself, but a versioned payload
        # must protect the entire program tree, including its sibling versions.
        if directory.parent.name.casefold() == "versions" and program_root != "../..":
            raise ValueError("versioned layout requires the whole program root")
        if program_root == "../.." and (
            directory.parent.name != "versions"
            or type(value.get("version_code")) is not int
            or not 1 <= value["version_code"] <= 2147483647
            or directory.name != str(value["version_code"])
        ):
            raise ValueError("unsupported version directory")
    except (OSError, ValueError):
        raise RuntimeError("Vault install marker is invalid; repair the installation.") from None
    return directory, value


def installed_directory() -> Path | None:
    """Directory of this embedded payload; a broken installed marker never falls back."""
    layout = _installed_layout()
    return layout[0] if layout else None


def program_directory() -> Path | None:
    """All managed program versions, excluded from Vault storage (no IO writes)."""
    layout = _installed_layout()
    if layout is None:
        return None
    directory, marker = layout
    return directory.parent.parent if marker.get("program_root") == "../.." else directory


def worker_python() -> str:
    """The real Python interpreter, never a frozen GUI launcher or PATH lookup."""
    directory = installed_directory()
    if directory is None:
        return sys.executable
    runtime = directory / "python"
    executable = runtime / "python.exe"
    if (runtime.is_symlink() or runtime.is_junction() or executable.is_symlink()
            or not executable.is_file()):
        raise RuntimeError("Vault bundled Python is missing or linked; repair the installation.")
    return str(executable)


def updater_python() -> str | None:
    """Installed GUI interpreter only; source checkouts cannot start an updater.

    Reuse the worker's fixed runtime-directory checks. Never consult PATH or
    accept a root, executable or command supplied by a document/settings file.
    """
    if installed_directory() is None:
        return None
    executable = Path(worker_python()).with_name("pythonw.exe")
    if executable.is_symlink() or executable.is_junction() or not executable.is_file():
        raise RuntimeError("Vault bundled GUI Python is missing or linked; repair the installation.")
    return str(executable)


def _lock(stream) -> None:
    stream.seek(_LEASE_BYTE)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(stream) -> None:
    stream.seek(_LEASE_BYTE)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _pid_alive(pid: int) -> bool | None:
    """False means demonstrated absent; permission/inspection errors are unknown."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False if ctypes.get_last_error() == 87 else None
        try:
            status = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(status)):
                return None
            return status.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return None
    return True


def _valid_owner(metadata: object) -> bool:
    if not isinstance(metadata, dict):
        return False
    if type(metadata.get("pid")) is not int or not 0 < metadata["pid"] < 2**32:
        return False
    if metadata.get("machine") != socket.gethostname() or metadata.get("state") not in ("active", "released"):
        return False
    try:
        return datetime.fromisoformat(metadata["started_at"]).tzinfo is not None
    except (KeyError, ValueError, TypeError):
        return False


def _reason_with_pid(reason: str, metadata: object) -> str:
    pid = metadata.get("pid") if isinstance(metadata, dict) else None
    if type(pid) is int and 0 < pid < 2**32:
        return f"{reason} (recorded owner PID {pid})"
    return reason


class RuntimeLease:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.path = self.root / ".vault.lock"
        self.read_only = True
        self.reason = READ_ONLY_REASON
        self.reclaimed: dict | None = None
        self._stream = None
        self._key = os.path.normcase(str(self.root))
        with _owners_lock:
            if self._key in _owners:
                self.reason = f"{READ_ONLY_REASON} (owner PID {os.getpid()})"
                return
            self.root.mkdir(parents=True, exist_ok=True)
            created = False
            try:
                try:
                    stream = self.path.open("x+b", buffering=0)
                    created = True
                except FileExistsError:
                    stream = self.path.open("r+b", buffering=0)
                try:
                    _lock(stream)
                except OSError:
                    try:
                        stream.seek(0)
                        self.reason = _reason_with_pid(
                            self.reason, json.loads(stream.read(_LEASE_BYTE).decode("utf-8")))
                    except (OSError, ValueError, UnicodeError):
                        pass  # owner metadata is diagnostic only, never authority
                    stream.close()
                    return
                self._stream = stream
                if not created:
                    stream.seek(0)
                    try:
                        previous = json.loads(stream.read(_LEASE_BYTE).decode("utf-8"))
                    except (ValueError, UnicodeError):
                        self.reason = "read-only: runtime lock ownership is unknown or damaged"
                        self._release_handle()
                        return
                    if not _valid_owner(previous):
                        self.reason = _reason_with_pid(
                            "read-only: runtime lock ownership is unknown or damaged", previous)
                        self._release_handle()
                        return
                    if previous["state"] != "released":
                        if _pid_alive(previous["pid"]) is not False:
                            self.reason = _reason_with_pid(self.reason, previous)
                            self._release_handle()
                            return
                        self.reclaimed = previous
                self._metadata = {
                    "pid": os.getpid(), "machine": socket.gethostname(),
                    "started_at": datetime.now(timezone.utc).isoformat(), "state": "active",
                }
                self._write(self._metadata)
                self.read_only = False
                self.reason = ""
                _owners[self._key] = self
            except OSError:
                self.reason = "read-only: runtime lock cannot be acquired safely"
                self._release_handle()

    def _write(self, metadata: dict) -> None:
        self._stream.seek(0)
        self._stream.write(b"\n" + json.dumps(metadata, sort_keys=True).encode("utf-8"))
        self._stream.truncate()
        self._stream.flush()
        os.fsync(self._stream.fileno())

    def _release_handle(self) -> None:
        if self._stream is not None:
            try:
                _unlock(self._stream)
            finally:
                self._stream.close()
                self._stream = None

    def close(self) -> None:
        with _owners_lock:
            if self._stream is None:
                return
            # If persistence fails, keep ownership; a still-open window must
            # never become a writer without its OS lease.
            self._write({**self._metadata, "state": "released"})
            _owners.pop(self._key, None)
            self._release_handle()
