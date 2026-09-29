"""A bounded, one-shot subprocess transport, not an agent security sandbox.

Windows descendants belong to a kill-on-close Job Object before any input is
sent. Assignment follows process creation: an executable can fork in that gap.
This is cleanup for cooperative CLI executables, not hostile-code containment,
filesystem isolation, or a guarantee that a CLI never persists its own input.
"""

from __future__ import annotations

import os
import math
import signal
import subprocess
import threading
import time

from .child_temp import ChildCleanupError, temporary_child_directory

INPUT_LIMIT = 256 * 1024
STDOUT_LIMIT = 1024 * 1024
STDERR_LIMIT = 64 * 1024


class _WindowsJob:
    """Only owns the per-call job handle; never enumerates unrelated processes."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        class Accounting(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_longlong),
                ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        self._ctypes = ctypes
        self._accounting = Accounting
        self._api = ctypes.WinDLL("kernel32", use_last_error=True)
        declarations = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "QueryInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (args, result) in declarations.items():
            function = getattr(self._api, name)
            function.argtypes, function.restype = args, result
        self._handle = self._api.CreateJobObjectW(None, None)
        if not self._handle:
            raise ProcessFailure("unusable")
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not self._api.SetInformationJobObject(self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise ProcessFailure("unusable")

    def assign(self, process):
        if not self._api.AssignProcessToJobObject(self._handle, int(process._handle)):
            raise ProcessFailure("unusable")

    def stop_and_wait(self):
        if not self._api.TerminateJobObject(self._handle, 1):
            raise ProcessFailure("unusable")
        deadline = time.monotonic() + 5
        while True:
            accounting = self._accounting()
            if not self._api.QueryInformationJobObject(
                self._handle, 1, self._ctypes.byref(accounting),
                self._ctypes.sizeof(accounting), None,
            ):
                raise ProcessFailure("unusable")
            if accounting.ActiveProcesses == 0:
                return
            if time.monotonic() >= deadline:
                raise ProcessFailure("unusable")
            time.sleep(0.01)

    def close(self):
        if self._handle:
            self._api.CloseHandle(self._handle)
            self._handle = None


class ProcessFailure(RuntimeError):
    def __init__(self, status: str):
        self.status = status
        super().__init__(status)


class OneShotRunner:
    def run(self, argv: list[str], payload: bytes, *, cancel: threading.Event,
            timeout_s: float = 180, env: dict[str, str] | None = None) -> bytes:
        try:
            return self._run(argv, payload, cancel=cancel, timeout_s=timeout_s, env=env)
        except ProcessFailure:
            raise
        except ChildCleanupError:
            raise ProcessFailure("cleanup_failed") from None
        except Exception:
            # Never publish stderr, command lines, filesystem paths or OS errors.
            raise ProcessFailure("unusable") from None

    def _run(self, argv, payload, *, cancel, timeout_s, env):
        if not isinstance(timeout_s, (float, int)) or not math.isfinite(timeout_s):
            raise ProcessFailure("unusable")
        deadline = time.monotonic() + timeout_s
        if cancel.is_set():
            raise ProcessFailure("cancelled")
        if not isinstance(payload, bytes) or len(payload) > INPUT_LIMIT:
            raise ProcessFailure("unusable")
        if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv):
            raise ProcessFailure("unusable")
        if time.monotonic() >= deadline:
            raise ProcessFailure("timeout")
        with temporary_child_directory(prefix="vault-agent-") as cwd:
            job = _WindowsJob() if os.name == "nt" else None
            try:
                process = subprocess.Popen(
                    argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, cwd=cwd, env=env, shell=False, bufsize=0,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    start_new_session=os.name != "nt",
                )
            except BaseException:
                if job is not None:
                    job.close()
                raise
            stdout = bytearray()
            io_failed = threading.Event()

            def read_stream(stream, output, limit):
                count = 0
                try:
                    while chunk := stream.read(8192):
                        count += len(chunk)
                        if count > limit:
                            io_failed.set()
                            return
                        if output is not None:
                            output.extend(chunk)
                except OSError:
                    io_failed.set()
                finally:
                    stream.close()

            def write_input():
                try:
                    remaining = memoryview(payload)
                    while remaining:
                        if cancel.is_set() or time.monotonic() >= deadline:
                            return
                        written = process.stdin.write(remaining)
                        if not written:
                            io_failed.set()
                            return
                        remaining = remaining[written:]
                    process.stdin.flush()
                except OSError:
                    io_failed.set()
                finally:
                    try:
                        process.stdin.close()
                    except OSError:
                        io_failed.set()

            workers = [
                threading.Thread(target=read_stream, args=(process.stdout, stdout, STDOUT_LIMIT), daemon=True),
                threading.Thread(target=read_stream, args=(process.stderr, None, STDERR_LIMIT), daemon=True),
                threading.Thread(target=write_input, daemon=True),
            ]
            try:
                if job is not None:
                    job.assign(process)
                if cancel.is_set():
                    raise ProcessFailure("cancelled")
                if time.monotonic() >= deadline:
                    raise ProcessFailure("timeout")
                for worker in workers:
                    worker.start()
                while True:
                    if cancel.is_set():
                        raise ProcessFailure("cancelled")
                    if io_failed.is_set():
                        raise ProcessFailure("unusable")
                    if time.monotonic() >= deadline:
                        raise ProcessFailure("timeout")
                    if process.poll() is not None:
                        break
                    time.sleep(0.01)
            finally:
                cleanup_failed = False
                if job is not None:
                    try:
                        job.stop_and_wait()
                    except ProcessFailure:
                        cleanup_failed = True
                    finally:
                        job.close()
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if process.poll() is None:
                    process.kill()
                process.wait()
                for worker in workers:
                    if worker.ident is not None:
                        worker.join(timeout=2)
                        cleanup_failed |= worker.is_alive()
                for stream in (process.stdin, process.stdout, process.stderr):
                    if not stream.closed:
                        stream.close()
                if cleanup_failed:
                    raise ProcessFailure("unusable")
            if cancel.is_set():
                raise ProcessFailure("cancelled")
            if time.monotonic() >= deadline:
                raise ProcessFailure("timeout")
            if process.returncode != 0 or io_failed.is_set() or not stdout.strip():
                raise ProcessFailure("unusable")
            return bytes(stdout)
