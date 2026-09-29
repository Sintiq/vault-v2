"""Build-time safety checks; no generated installer is ever executed here."""
import hashlib
import json
import os
from pathlib import Path

import pytest

from tools.build_windows_installer import BuildError, compile_installer, validate_payload


@pytest.fixture
def payload(tmp_path):
    root = tmp_path / "payload"
    root.mkdir()
    files = {"python/python.exe": b"MZ synthetic", "python/pythonw.exe": b"MZ synthetic",
             "vault_v2/launcher.py": b"# synthetic", "vault_v2/installation.py": b"# synthetic",
             "vault-install.json": json.dumps({"schema": "vault-v2-install@1",
                 "worker_python": "python/python.exe", "program_root": "../..",
                 "version": "0.0.0-test", "version_code": 2, "api_version": 1}).encode()}
    entries = []
    for name, raw in files.items():
        path = root / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(raw)
        entries.append({"path": name, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    (root / "windows-payload-files.json").write_text(json.dumps({"schema": "vault-v2-payload-files@1", "files": entries}))
    return root


def test_validate_exact_payload_identity_and_files(payload):
    checked = validate_payload(payload)
    assert checked.version_code == 2
    assert checked.version == "0.0.0-test"
    assert "windows-payload-files.json" in checked.files


def test_direct_redist_payload_requires_recipient_terms_before_compilation(payload):
    report = payload / "runtime-redist.json"
    report.write_bytes(b'{"schema":"vault-msvc-redist-applied@1"}')
    inventory_path = payload / "windows-payload-files.json"
    inventory = json.loads(inventory_path.read_text())
    inventory["files"].append({"path": report.name, "size": report.stat().st_size,
                               "sha256": hashlib.sha256(report.read_bytes()).hexdigest()})
    inventory_path.write_text(json.dumps(inventory))
    with pytest.raises(BuildError, match="recipient terms"):
        validate_payload(payload)


def test_validated_terms_are_bound_to_the_compiler_payload(payload):
    raw = b"Synthetic component terms; not a real agreement.\n"
    path = payload / "MSVC-RECIPIENT-TERMS.txt"
    path.write_bytes(raw)
    inventory_path = payload / "windows-payload-files.json"
    inventory = json.loads(inventory_path.read_text())
    inventory["files"].append({"path": path.name, "size": len(raw),
                               "sha256": hashlib.sha256(raw).hexdigest()})
    inventory_path.write_text(json.dumps(inventory))
    checked = validate_payload(payload)
    assert checked.recipient_terms == raw


def test_terms_changed_between_inventory_and_capture_are_refused(payload, monkeypatch):
    import io
    raw = b"Synthetic original terms.\n"
    path = payload / "MSVC-RECIPIENT-TERMS.txt"
    path.write_bytes(raw)
    index = payload / "windows-payload-files.json"
    inventory = json.loads(index.read_text())
    inventory["files"].append({"path": path.name, "size": len(raw),
                               "sha256": hashlib.sha256(raw).hexdigest()})
    index.write_text(json.dumps(inventory))
    original_open = Path.open
    reads = 0

    def changed_filesystem(candidate, *args, **kwargs):
        nonlocal reads
        if candidate == path and args and args[0] == "rb":
            reads += 1
            if reads == 2:
                return io.BytesIO(b"X" * len(raw))
        return original_open(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "open", changed_filesystem)
    with pytest.raises(BuildError, match="recipient terms.*changed"):
        validate_payload(payload)


@pytest.mark.parametrize("raw", [b"", b" \r\n", b"\xff", b"terms\0hidden", b"x" * 65537,
                                 b"terms\x7f", "terms\u0085hidden".encode("utf-8")],
                         ids=["empty", "whitespace", "non-utf8", "nul", "oversize", "del", "c1"])
def test_unreadable_recipient_terms_are_refused(payload, raw):
    path = payload / "MSVC-RECIPIENT-TERMS.txt"
    path.write_bytes(raw)
    inventory_path = payload / "windows-payload-files.json"
    inventory = json.loads(inventory_path.read_text())
    inventory["files"].append({"path": path.name, "size": len(raw),
                               "sha256": hashlib.sha256(raw).hexdigest()})
    inventory_path.write_text(json.dumps(inventory))
    with pytest.raises(BuildError, match="recipient terms"):
        validate_payload(payload)


def test_compiler_receives_exact_inventoried_terms(payload, tmp_path, monkeypatch):
    """Optional pinned-toolchain integration; mock only process execution."""
    compiler = os.environ.get("VAULT_TEST_ISCC")
    if not compiler:
        pytest.skip("explicit pinned Inno compiler required for this integration")
    raw = b"Synthetic component terms; no legal acceptance.\r\n"
    path = payload / "MSVC-RECIPIENT-TERMS.txt"
    path.write_bytes(raw)
    index = payload / "windows-payload-files.json"
    inventory = json.loads(index.read_text())
    inventory["files"].append({"path": path.name, "size": len(raw),
                               "sha256": hashlib.sha256(raw).hexdigest()})
    index.write_text(json.dumps(inventory))

    def compiler_process(command, *, cwd, **kwargs):
        terms_args = [arg for arg in command if arg.startswith("/DRecipientTerms=")]
        assert len(terms_args) == 1
        shown = Path(terms_args[0].split("=", 1)[1])
        assert shown.parent == cwd
        assert shown.read_bytes() == raw
        (cwd / "vault-setup.exe").write_bytes(b"MZ synthetic compiler output never executed")
        import subprocess
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr("subprocess.run", compiler_process)
    built = compile_installer(payload, Path(compiler), tmp_path / "candidate.exe")
    assert built.read_bytes() == b"MZ synthetic compiler output never executed"


@pytest.mark.parametrize("change", ["extra", "changed", "missing", "traversal", "duplicate"])
def test_invalid_payload_refused_before_compiler(payload, change):
    inventory = payload / "windows-payload-files.json"
    if change == "extra":
        (payload / "private.txt").write_text("never compile")
    elif change == "changed":
        (payload / "python/python.exe").write_bytes(b"MZ altered")
    elif change == "missing":
        (payload / "python/pythonw.exe").unlink()
    else:
        value = json.loads(inventory.read_text())
        value["files"].append(dict(value["files"][0], path="../outside" if change == "traversal" else value["files"][0]["path"]))
        inventory.write_text(json.dumps(value))
    with pytest.raises(BuildError):
        validate_payload(payload)


def test_unpinned_compiler_is_not_executed(payload, tmp_path):
    compiler = tmp_path / "ISCC.exe"
    compiler.write_bytes(b"not a compiler")
    output = tmp_path / "candidate.exe"
    with pytest.raises(BuildError, match="pin mismatch"):
        compile_installer(payload, compiler, output)
    assert not output.exists()


def test_existing_output_is_preserved_without_compilation(payload, tmp_path):
    output = tmp_path / "candidate.exe"
    output.write_bytes(b"KEEP")
    with pytest.raises(BuildError, match="new executable"):
        compile_installer(payload, tmp_path / "missing.exe", output)
    assert output.read_bytes() == b"KEEP"


@pytest.mark.parametrize("invalid", ["path", "version", "code", "api"])
def test_installed_identity_must_match_versioned_recipe(payload, invalid):
    path = payload / "vault-install.json"
    marker = json.loads(path.read_text())
    key, value = {"path": ("program_root", "../../.."), "version": ("version", 'quote"inject'),
                  "code": ("version_code", True), "api": ("api_version", 0)}[invalid]
    marker[key] = value
    raw = json.dumps(marker).encode()
    path.write_bytes(raw)
    index = payload / "windows-payload-files.json"
    inventory = json.loads(index.read_text())
    for item in inventory["files"]:
        if item["path"] == "vault-install.json":
            item.update(size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    index.write_text(json.dumps(inventory))
    with pytest.raises(BuildError):
        validate_payload(payload)
