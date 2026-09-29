"""Synthetic executable contract tests; no agent or network is invoked."""

import json
import os
from pathlib import Path
import sys
import subprocess
import threading
import time

import pytest

from vault_v2.door_process import OneShotRunner, ProcessFailure


def test_payload_is_stdin_in_a_fresh_empty_cwd_not_an_argument():
    code = (
        "import json,os,sys; "
        "print(json.dumps({'cwd':os.getcwd(),'files':os.listdir('.'),"
        "'args':sys.argv[1:],'payload':sys.stdin.buffer.read().decode()}))"
    )
    runner = OneShotRunner()
    first = json.loads(runner.run([sys.executable, "-I", "-c", code], b"synthetic private text", cancel=threading.Event()))
    second = json.loads(runner.run([sys.executable, "-I", "-c", code], b"another", cancel=threading.Event()))
    assert first["files"] == []
    assert first["args"] == []
    assert first["payload"] == "synthetic private text"
    assert first["cwd"] != second["cwd"]
    assert not Path(first["cwd"]).exists()
    assert not Path(second["cwd"]).exists()


@pytest.mark.parametrize("code", [
    "import sys; print('synthetic secret'); sys.exit(7)",
    "import sys; sys.stderr.write('synthetic secret')",
    "import sys; sys.stdout.write('  \\n')",
], ids=["nonzero", "no-stdout", "whitespace"])
def test_nonzero_or_empty_output_is_sanitized_unusable(code):
    with pytest.raises(ProcessFailure) as error:
        OneShotRunner().run([sys.executable, "-I", "-c", code], b"", cancel=threading.Event())
    assert error.value.status == "unusable"
    assert str(error.value) == "unusable"


def test_timeout_covers_a_child_which_never_reads_stdin():
    start = time.monotonic()
    with pytest.raises(ProcessFailure) as error:
        OneShotRunner().run(
            [sys.executable, "-I", "-c", "import time; time.sleep(30)"],
            b"x" * (256 * 1024), cancel=threading.Event(), timeout_s=0.2,
        )
    assert error.value.status == "timeout"
    assert time.monotonic() - start < 5


def test_already_cancelled_request_does_not_launch():
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ProcessFailure) as error:
        OneShotRunner().run(["synthetic-program-that-does-not-exist"], b"", cancel=cancel)
    assert error.value.status == "cancelled"


def test_cancellation_interrupts_a_running_request():
    cancel = threading.Event()
    timer = threading.Timer(0.15, cancel.set)
    timer.start()
    start = time.monotonic()
    try:
        with pytest.raises(ProcessFailure) as error:
            OneShotRunner().run(
                [sys.executable, "-I", "-c", "import time; time.sleep(1); print('too late')"],
                b"", cancel=cancel,
            )
        assert error.value.status == "cancelled"
        assert time.monotonic() - start < 0.9
    finally:
        timer.join()


@pytest.mark.parametrize("code,payload", [
    ("import sys; print(len(sys.stdin.buffer.read()))", b"x" * (256 * 1024 + 1)),
    ("import sys; sys.stdout.buffer.write(b'x' * (1024 * 1024 + 1))", b""),
    ("import sys; sys.stderr.buffer.write(b'x' * (64 * 1024 + 1)); print('ok')", b""),
], ids=["input", "stdout", "stderr"])
def test_oversized_input_or_output_is_unusable(monkeypatch, code, payload):
    actual_popen = subprocess.Popen
    children, directories = [], []

    def capture_launch(*args, **kwargs):
        directories.append(Path(kwargs["cwd"]))
        child = actual_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", capture_launch)
    with pytest.raises(ProcessFailure) as error:
        OneShotRunner().run([sys.executable, "-I", "-c", code], payload, cancel=threading.Event())
    assert error.value.status == "unusable"
    if payload:
        assert children == []
    else:
        assert children and all(child.poll() is not None for child in children)
        assert all(not directory.exists() for directory in directories)


def test_exact_limits_are_accepted_and_stderr_is_not_returned():
    code = (
        "import sys; assert len(sys.stdin.buffer.read())==262144; "
        "sys.stderr.buffer.write(b'e'*65536); sys.stdout.buffer.write(b'o'*1048576)"
    )
    result = OneShotRunner().run([sys.executable, "-I", "-c", code], b"i" * 262144, cancel=threading.Event())
    assert result == b"o" * 1048576


def test_cancel_cleans_up_its_own_child_tree(tmp_path):
    ready = tmp_path / "synthetic-ready"
    late = tmp_path / "synthetic-orphan"
    child = f"import time; from pathlib import Path; time.sleep(0.8); Path({str(late)!r}).touch()"
    parent = (
        "import subprocess,sys,time; from pathlib import Path; sys.stdin.buffer.read(); "
        f"subprocess.Popen([sys.executable,'-I','-c',{child!r}],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
        f"Path({str(ready)!r}).touch(); time.sleep(5)"
    )
    cancel = threading.Event()

    def cancel_when_ready():
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        cancel.set()

    worker = threading.Thread(target=cancel_when_ready)
    worker.start()
    try:
        with pytest.raises(ProcessFailure) as error:
            OneShotRunner().run([sys.executable, "-I", "-c", parent], b"", cancel=cancel, timeout_s=4)
        assert error.value.status == "cancelled"
        assert ready.exists()
        time.sleep(1)
        assert not late.exists(), "owned descendant survived cancellation"
    finally:
        cancel.set()
        worker.join()


def test_launch_failure_is_sanitized_without_command_or_os_error(tmp_path):
    with pytest.raises(ProcessFailure) as error:
        OneShotRunner().run([str(tmp_path / "synthetic-secret-executable")], b"synthetic secret", cancel=threading.Event())
    assert error.value.status == "unusable"
    assert str(error.value) == "unusable"


def test_elapsed_deadline_prevents_launch():
    with pytest.raises(ProcessFailure) as error:
        OneShotRunner().run(["synthetic-program-that-does-not-exist"], b"", cancel=threading.Event(), timeout_s=0)
    assert error.value.status == "timeout"


@pytest.mark.parametrize("timeout_s", [float("nan"), float("inf"), "invalid"])
def test_invalid_deadline_is_unusable(timeout_s):
    with pytest.raises(ProcessFailure) as error:
        OneShotRunner().run([sys.executable, "-I", "-c", "print('ok')"], b"", cancel=threading.Event(), timeout_s=timeout_s)
    assert error.value.status == "unusable"


def test_success_reaps_background_descendant_holding_output_pipe(tmp_path):
    late = tmp_path / "synthetic-orphan"
    child = f"import time; from pathlib import Path; time.sleep(1); Path({str(late)!r}).touch()"
    parent = (
        "import subprocess,sys; sys.stdin.buffer.read(); "
        f"subprocess.Popen([sys.executable,'-I','-c',{child!r}]); print('done',flush=True)"
    )
    result = OneShotRunner().run([sys.executable, "-I", "-c", parent], b"", cancel=threading.Event(), timeout_s=0.4)
    assert result.strip() == b"done"
    time.sleep(1.1)
    assert not late.exists()


def test_arguments_are_literal_and_explicit_environment_is_used():
    code = "import json,os,sys; print(json.dumps([sys.argv[1],os.environ.get('VAULT_SYNTHETIC_ENV')]))"
    result = OneShotRunner().run(
        [sys.executable, "-I", "-c", code, "literal & echo this-is-not-a-command"],
        b"", cancel=threading.Event(), env={**os.environ, "VAULT_SYNTHETIC_ENV": "test-value"},
    )
    assert json.loads(result) == ["literal & echo this-is-not-a-command", "test-value"]


@pytest.mark.skipif(os.name != "nt", reason="Windows process contract")
def test_windows_child_has_no_console_window():
    result = OneShotRunner().run(
        [sys.executable, "-I", "-c", "import ctypes; print(ctypes.windll.kernel32.GetConsoleWindow())"],
        b"", cancel=threading.Event(),
    )
    assert result.strip() == b"0"


@pytest.mark.skipif(os.name != "nt", reason="Windows job assignment boundary")
def test_failed_job_assignment_sends_no_payload(monkeypatch, tmp_path):
    import ctypes

    received = tmp_path / "synthetic-received"
    actual_windll = ctypes.WinDLL

    class FailedAssignment:
        def __call__(self, *args):
            return 0

    class KernelProxy:
        def __init__(self, *args, **kwargs):
            self.actual = actual_windll(*args, **kwargs)
            self.AssignProcessToJobObject = FailedAssignment()

        def __getattr__(self, name):
            return getattr(self.actual, name)

    monkeypatch.setattr(ctypes, "WinDLL", KernelProxy)
    code = f"import sys; from pathlib import Path; data=sys.stdin.buffer.read(); Path({str(received)!r}).write_bytes(data)"
    with pytest.raises(ProcessFailure) as error:
        OneShotRunner().run([sys.executable, "-I", "-c", code], b"synthetic private text", cancel=threading.Event())
    assert error.value.status == "unusable"
    assert not received.exists()


def test_shell_style_command_string_is_not_an_argv_vector():
    command = subprocess.list2cmdline([sys.executable, "-I", "-c", "print('ok')"])
    with pytest.raises(ProcessFailure) as error:
        OneShotRunner().run(command, b"", cancel=threading.Event())
    assert error.value.status == "unusable"


def test_cancel_during_process_start_sends_no_stdin(monkeypatch):
    cancel = threading.Event()
    sent = []
    actual_popen = subprocess.Popen

    class RecordingInput:
        def __init__(self, stream):
            self.stream = stream

        def write(self, value):
            sent.append(bytes(value))
            return self.stream.write(value)

        def __getattr__(self, name):
            return getattr(self.stream, name)

    def launch_then_cancel(*args, **kwargs):
        process = actual_popen(*args, **kwargs)
        process.stdin = RecordingInput(process.stdin)
        cancel.set()
        return process

    monkeypatch.setattr(subprocess, "Popen", launch_then_cancel)
    with pytest.raises(ProcessFailure) as error:
        OneShotRunner().run(
            [sys.executable, "-I", "-c", "import sys; sys.stdin.buffer.read(); print('done')"],
            b"synthetic private text", cancel=cancel,
        )
    assert error.value.status == "cancelled"
    assert sent == []
