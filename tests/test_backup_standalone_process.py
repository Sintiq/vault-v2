"""The exported recovery program works with isolated Python, without the app."""

import hashlib
import io
import json
import subprocess
import sys
import zipfile

import pytest

from vault_v2.backup import encrypt, make_backup
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths


PASSPHRASE = "synthetic standalone restore passphrase"
INPUT_DRIVER = """import getpass, runpy, sys
getpass.getpass = lambda *args, **kwargs: sys.stdin.readline().rstrip('\\n')
script, source, destination = sys.argv[1:]
sys.argv = [script, source, destination]
runpy.run_path(script, run_name='__main__')
"""


@pytest.fixture()
def exported_backup(tmp_path):
    vault = VaultOps(VaultPaths(tmp_path / "source-vault"))
    expected = {
        "documents/record.txt": b"synthetic ordinary document",
        "personal/.private/note.txt": b"synthetic hidden document",
        ".api/key": b"synthetic service key, not a credential",
        ".door/config.json": b'{"synthetic": true}',
    }
    for relative, data in expected.items():
        source = vault.paths.root / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(data)
    result = make_backup(vault.paths.root, tmp_path / "drive", PASSPHRASE)
    script = result.path.parent / "restore_backup.py"
    assert script.is_file()
    return result, script, expected


def run_exported_script(tmp_path, script, archive, destination, optimized):
    working_directory = tmp_path / "unrelated-working-directory"
    working_directory.mkdir(exist_ok=True)
    options = ["-I", "-O"] if optimized else ["-I"]
    return subprocess.run(
        [sys.executable, *options, "-c", INPUT_DRIVER, str(script), str(archive), str(destination)],
        input=PASSPHRASE + "\n", text=True, capture_output=True,
        cwd=working_directory, timeout=25,
    )


@pytest.mark.parametrize("optimized", [False, True], ids=["isolated", "isolated-optimized"])
def test_exported_program_restores_hidden_and_service_files_outside_repo(exported_backup, tmp_path, optimized):
    result, script, expected = exported_backup
    destination = tmp_path / "new-parent" / "restored"

    process = run_exported_script(tmp_path, script, result.path, destination, optimized)

    assert process.returncode == 0, process.stdout + process.stderr
    assert f"restored {result.files} files" in process.stdout
    actual = {path.relative_to(destination).as_posix(): path.read_bytes()
              for path in destination.rglob("*") if path.is_file()}
    assert actual == expected


@pytest.mark.parametrize("optimized", [False, True], ids=["isolated", "isolated-optimized"])
@pytest.mark.parametrize("defect,refusal", [
    ("late-hash", "damaged inside the backup"),
    ("extra-zip-file", "manifest"),
    ("parent-traversal", "unsafe path"),
])
def test_exported_program_preflights_entire_archive_before_mkdir(exported_backup, tmp_path, optimized, defect, refusal):
    _result, script, _expected = exported_backup
    entries = [("documents/first.txt", b"valid synthetic first document"),
               ("documents/late.txt", b"synthetic late document")]
    if defect == "parent-traversal":
        entries[-1] = ("../escape.txt", b"synthetic escape attempt")
    manifest = {name: hashlib.sha256(data).hexdigest() for name, data in entries}
    if defect == "late-hash":
        manifest["documents/late.txt"] = "0" * 64
    elif defect == "extra-zip-file":
        del manifest["documents/late.txt"]
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, data in entries:
            archive.writestr(name, data)
        archive.writestr("MANIFEST.json", json.dumps(manifest))
    malformed = tmp_path / "synthetic-malformed.vault"
    malformed.write_bytes(encrypt(output.getvalue(), PASSPHRASE))
    destination = tmp_path / "new-parent" / "restored"

    process = run_exported_script(tmp_path, script, malformed, destination, optimized)

    assert process.returncode != 0
    assert "restore refused or interrupted:" in process.stderr
    assert refusal in process.stderr
    assert "restored " not in process.stdout
    assert not destination.parent.exists(), "preflight must refuse before creating even the parent directory"
    assert not (tmp_path / "escape.txt").exists()
