"""Process-lifetime Windows install admission, separate from per-vault write locks.

All managed windows retain runtime markers (including read-only windows). A short
gate serializes admission against an installer marker. Unknown OS state refuses
entry; this is coordination among our processes, not protection from hostile code.
The installer must implement this same protocol, not just a one-time AppMutex check.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
import re
from enum import StrEnum


NAMESPACE = "Global\\VaultV2-InstallAdmission-v1"


class RuntimeState(StrEnum):
    IDLE = "idle"
    ACTIVE = "active"
    UNKNOWN = "unknown"


class InstallationBusy(RuntimeError):
    """Fixed safe refusal: no automatic close, kill or retry that changes state."""


def _kernel():
    if os.name != "nt":
        raise InstallationBusy("Windows installation admission is unavailable")
    import ctypes
    from ctypes import wintypes
    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    dll.CreateMutexW.restype = wintypes.HANDLE
    dll.OpenMutexW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    dll.OpenMutexW.restype = wintypes.HANDLE
    dll.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    dll.WaitForSingleObject.restype = wintypes.DWORD
    dll.ReleaseMutex.argtypes = (wintypes.HANDLE,)
    dll.ReleaseMutex.restype = wintypes.BOOL
    dll.CloseHandle.argtypes = (wintypes.HANDLE,)
    dll.CloseHandle.restype = wintypes.BOOL
    return dll


def _name(namespace: str, suffix: str) -> str:
    if type(namespace) is not str or re.fullmatch(r"(?:Global|Local)\\[A-Za-z0-9._-]{1,160}", namespace) is None:
        raise InstallationBusy("Invalid installation admission namespace")
    return namespace + "." + suffix


def _state(kernel, name: str) -> RuntimeState:
    import ctypes
    ctypes.set_last_error(0)
    handle = kernel.OpenMutexW(0x00100000, False, name)  # SYNCHRONIZE, read only
    if handle:
        kernel.CloseHandle(handle)
        return RuntimeState.ACTIVE
    return RuntimeState.IDLE if ctypes.get_last_error() == 2 else RuntimeState.UNKNOWN


def runtime_state(namespace: str = NAMESPACE) -> RuntimeState:
    """Observation only. Callers must retain InstallationSession for actual admission."""
    try:
        return _state(_kernel(), _name(namespace, "runtime"))
    except InstallationBusy:
        return RuntimeState.UNKNOWN


@contextmanager
def _gate(kernel, namespace: str):
    handle = kernel.CreateMutexW(None, False, _name(namespace, "gate"))
    if not handle:
        raise InstallationBusy("Cannot inspect installation state safely")
    acquired = False
    try:
        status = kernel.WaitForSingleObject(handle, 0)
        if status not in (0, 0x80):  # acquired / abandoned owner, no persisted assumptions
            raise InstallationBusy("Installation state is busy or unknown; try again")
        acquired = True
        yield
    finally:
        if acquired:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


class InstallationSession:
    """Retain this context for the whole app lifetime or whole installer handoff.

    Multiple runtime sessions are allowed. Installer sessions require zero managed
    windows and no installer; runtime sessions require no installer. The named
    objects are reclaimed by Windows after process death. Namespace overrides are
    for isolated synthetic tests, never taken from release metadata.
    """
    def __init__(self, kind: str, *, namespace: str = NAMESPACE):
        if kind not in ("runtime", "installer"):
            raise ValueError("Unsupported installation session kind")
        self.kind, self.namespace = kind, namespace
        self._handle = None
        self._kernel = None

    def __enter__(self):
        if self._handle is not None:
            raise InstallationBusy("Installation session is already active")
        kernel = _kernel()
        with _gate(kernel, self.namespace):
            if _state(kernel, _name(self.namespace, "installer")) != RuntimeState.IDLE:
                raise InstallationBusy("Vault installation is active or unknown; finish it first")
            if self.kind == "installer" and _state(kernel, _name(self.namespace, "runtime")) != RuntimeState.IDLE:
                raise InstallationBusy("Close all Vault windows before installing; state is active or unknown")
            handle = kernel.CreateMutexW(None, False, _name(self.namespace, self.kind))
            if not handle:
                raise InstallationBusy("Cannot retain installation admission safely")
            self._kernel, self._handle = kernel, handle
        return self

    def __exit__(self, *_):
        if self._handle is not None:
            self._kernel.CloseHandle(self._handle)
            self._handle = None
