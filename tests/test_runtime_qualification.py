"""The qualifier must understand real Windows subprocess audit events."""
from pathlib import Path
import subprocess

import pytest

from tools.qualify_runtime_0p import checked_worker_command


@pytest.mark.parametrize("worker", ["pdf_worker.py", "ocr_worker.py"])
@pytest.mark.parametrize("windows_event", [True, False])
def test_exact_worker_launch_accepts_windows_string_or_posix_argv(tmp_path, worker, windows_event):
    python = tmp_path / "Бандл с пробелами" / "python" / "python.exe"
    command = [str(python), "-I", "-B", str(python.parent.parent / "vault_v2" / worker)]
    executable, arguments = (None, subprocess.list2cmdline(command)) if windows_event else (str(python), command)
    assert checked_worker_command(executable, arguments, python) == command


@pytest.mark.parametrize("change", ["interpreter", "flag", "script", "extra", "explicit_executable"])
def test_qualifier_refuses_other_command_even_if_it_mentions_worker(tmp_path, change):
    python = tmp_path / "bundle" / "python" / "python.exe"
    command = [str(python), "-I", "-B", str(python.parent.parent / "vault_v2" / "pdf_worker.py")]
    executable = None
    if change == "interpreter":
        command[0] = "python.exe"
    elif change == "flag":
        command.remove("-I")
    elif change == "script":
        command[3] = str(tmp_path / "pdf_worker.py")
    elif change == "extra":
        command.append("extra")
    else:
        executable = str(tmp_path / "other.exe")
    with pytest.raises(RuntimeError, match="unexpected child"):
        checked_worker_command(executable, subprocess.list2cmdline(command), python)
