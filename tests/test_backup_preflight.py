"""Synthetic archives through the two recovery entrances and public Verify."""

from __future__ import annotations

import hashlib
import io
import json
import runpy
import stat
import sys
import warnings
import zipfile
from pathlib import Path

import pytest

from vault_v2.backup import BackupError, RESTORE_SCRIPT, encrypt, make_backup, restore_backup, verify_backup

PASS = "synthetic recovery passphrase"
AUTO = object()


def archive(tmp_path, entries, manifest=AUTO, *, raw_manifest=None):
    if manifest is AUTO:
        manifest = {name: hashlib.sha256(data).hexdigest() for name, data in entries}
    buf = io.BytesIO()
    with warnings.catch_warnings(), zipfile.ZipFile(buf, "w") as z:
        warnings.filterwarnings("ignore", message="Duplicate name:", category=UserWarning)
        for name, data in entries:
            z.writestr(name, data)
        z.writestr("MANIFEST.json", raw_manifest if raw_manifest is not None else json.dumps(manifest))
    src = tmp_path / "synthetic.vault"
    src.write_bytes(encrypt(buf.getvalue(), PASS))
    return src


def invoke(route, src, target, monkeypatch):
    if route == "verify":
        return verify_backup(src, PASS)
    if route == "app":
        return restore_backup(src, PASS, target)
    script = src.parent / "restore_backup.py"
    script.write_text(RESTORE_SCRIPT, encoding="utf-8")
    monkeypatch.setattr("getpass.getpass", lambda *_a, **_k: PASS)
    monkeypatch.setattr(sys, "argv", [str(script), str(src), str(target)])
    return runpy.run_path(str(script), run_name="__main__")


@pytest.mark.parametrize("route", ["verify", "app", "script"])
def test_unlisted_late_file_refused_before_target_creation(tmp_path, monkeypatch, route):
    good = b"synthetic first file"
    src = archive(tmp_path, [("first.txt", good), ("extra.txt", b"unlisted")],
                  {"first.txt": hashlib.sha256(good).hexdigest()})
    target = tmp_path / "new-parent" / "restore"
    with pytest.raises((BackupError, SystemExit)):
        invoke(route, src, target, monkeypatch)
    assert not target.parent.exists(), "preflight must precede even mkdir"


@pytest.mark.parametrize("route", ["verify", "app", "script"])
@pytest.mark.parametrize("unsafe", [
    "../restore-peer/escaped.txt", "/absolute.txt", "C:/absolute.txt", "C:drive.txt",
    "//server/share/file", "\\\\?\\C:\\file", "folder\\file", "folder//file",
    "./file", "folder/../file", "folder/./file", "file:stream", "trailing.",
    "trailing ", "folder./file", "NUL.txt", "CON", "com1.doc", "LPT9",
    "aux/child", "COM\u00b9.txt", "bad?name", "bad*name", "bad|name", 'bad"name',
    "bad<name", "bad>name", "control\x01", "", "x" * 256, "\U0001f600" * 128,
])
def test_unsafe_late_path_never_writes_first_file(tmp_path, monkeypatch, route, unsafe):
    src = archive(tmp_path, [("first.txt", b"valid"), (unsafe, b"must refuse")])
    target = tmp_path / "new-parent" / "restore"
    with pytest.raises((BackupError, SystemExit)):
        invoke(route, src, target, monkeypatch)
    assert not target.parent.exists()


@pytest.mark.parametrize("route", ["verify", "app", "script"])
@pytest.mark.parametrize("names", [
    ("same.txt", "same.txt"), ("Same.txt", "same.TXT"),
    ("folder/a", "Folder/b"), ("a", "a/b"), ("a/b", "a"),
    ("MANIFEST.json", "x"), ("manifest.JSON", "x"),
    ("MANIFEST.json/child", "x"), ("directory/", "x"),
])
def test_duplicate_or_conflicting_tree_refused_before_writes(tmp_path, monkeypatch, route, names):
    src = archive(tmp_path, [(name, b"synthetic") for name in names])
    target = tmp_path / "new-parent" / "restore"
    with pytest.raises((BackupError, SystemExit)):
        invoke(route, src, target, monkeypatch)
    assert not target.parent.exists()


@pytest.mark.parametrize("route", ["verify", "app", "script"])
@pytest.mark.parametrize("manifest", [None, [], 7, "file", {"first.txt": None},
    {"first.txt": []}, {"first.txt": 7}, {"first.txt": "short"},
    {"missing.txt": "0" * 64}, {"MANIFEST.json": "0" * 64}])
def test_malformed_manifest_is_clean_refusal(tmp_path, monkeypatch, route, manifest):
    src = archive(tmp_path, [("first.txt", b"valid")], manifest)
    target = tmp_path / "new-parent" / "restore"
    with pytest.raises((BackupError, SystemExit)):
        invoke(route, src, target, monkeypatch)
    assert not target.parent.exists()


@pytest.mark.parametrize("route", ["verify", "app", "script"])
@pytest.mark.parametrize("raw", [b"not json", b"\xff", b'{"first.txt":NaN}',
    ('{"first.txt":"%s","first.txt":"%s"}' %
     (hashlib.sha256(b"valid").hexdigest(), hashlib.sha256(b"valid").hexdigest())).encode()])
def test_unparseable_or_duplicate_manifest_is_clean_refusal(tmp_path, monkeypatch, route, raw):
    src = archive(tmp_path, [("first.txt", b"valid")], raw_manifest=raw)
    target = tmp_path / "new-parent" / "restore"
    with pytest.raises((BackupError, SystemExit)):
        invoke(route, src, target, monkeypatch)
    assert not target.parent.exists()


@pytest.mark.parametrize("route", ["verify", "app", "script"])
@pytest.mark.parametrize("problem", ["no-manifest", "not-zip", "late-crc", "late-hash", "nul-name", "truncated-envelope"])
def test_archive_damage_cannot_leave_early_writes(tmp_path, monkeypatch, route, problem):
    entries = [("first.txt", b"valid"), ("late.txt", b"CRC_SENTINEL_LATE")]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries:
            z.writestr(name, data)
        if problem != "no-manifest":
            manifest = {name: hashlib.sha256(data).hexdigest() for name, data in entries}
            if problem == "late-hash":
                manifest["late.txt"] = "0" * 64
            z.writestr("MANIFEST.json", json.dumps(manifest))
    plain = buf.getvalue()
    if problem == "late-crc":
        plain = plain.replace(b"CRC_SENTINEL_LATE", b"CRC_SENTINEL_LAtE")
    elif problem == "not-zip":
        plain = b"this is not a zip archive"
    elif problem == "nul-name":
        plain = plain.replace(b"late.txt", b"late\x00txt")
    src = tmp_path / "synthetic.vault"
    src.write_bytes(b"VAULTBK1" if problem == "truncated-envelope" else encrypt(plain, PASS))
    target = tmp_path / "new-parent" / "restore"
    with pytest.raises((BackupError, SystemExit)):
        invoke(route, src, target, monkeypatch)
    assert not target.parent.exists()


@pytest.mark.parametrize("route", ["verify", "app", "script"])
@pytest.mark.parametrize("mode", [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFDIR, stat.S_IFCHR])
@pytest.mark.parametrize("metadata", [False, True])
def test_special_zip_members_refused_including_manifest(tmp_path, monkeypatch, route, mode, metadata):
    info = zipfile.ZipInfo("MANIFEST.json" if metadata else "special")
    info.create_system = 3
    info.external_attr = (mode | 0o600) << 16
    buf = io.BytesIO()
    manifest = {"special": hashlib.sha256(b"synthetic").hexdigest()}
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("special" if metadata else info, b"synthetic")
        z.writestr(info if metadata else "MANIFEST.json", json.dumps(manifest))
    src = tmp_path / "synthetic.vault"
    src.write_bytes(encrypt(buf.getvalue(), PASS))
    target = tmp_path / "new-parent" / "restore"
    with pytest.raises((BackupError, SystemExit)):
        invoke(route, src, target, monkeypatch)
    assert not target.parent.exists()


def test_writer_refuses_reserved_metadata_collision_before_output(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "MANIFEST.json").write_bytes(b"owner data, not backup metadata")
    dest = tmp_path / "drive"
    with pytest.raises(BackupError):
        make_backup(root, dest, PASS)
    assert not dest.exists()
    assert (root / "MANIFEST.json").read_bytes() == b"owner data, not backup metadata"


@pytest.mark.parametrize("route", ["verify", "app", "script"])
def test_empty_archive_remains_valid(tmp_path, monkeypatch, route):
    src = archive(tmp_path, [])
    target = tmp_path / "empty"
    result = invoke(route, src, target, monkeypatch)
    if route != "script":
        assert result == 0
    if route != "verify":
        assert target.is_dir() and list(target.iterdir()) == []
