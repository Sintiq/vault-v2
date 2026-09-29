"""Gatekeeper tests — the only exit from the vault."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from vault_v2.gatekeeper import APPROVAL_TTL_S, ExportItem, ExportRequest, GateError, Gatekeeper
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.receipts import sha256_file


@pytest.fixture()
def vault(tmp_path: Path) -> VaultOps:
    return VaultOps(VaultPaths(tmp_path / "vault"))


def _mk(p: Path, content: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_folder_export_writes_files_and_manifest(vault: VaultOps, tmp_path: Path) -> None:
    a = _mk(vault.paths.staging / "a.txt", "alpha")
    b = _mk(vault.paths.staging / "sub" / "b.txt", "bravo")
    gate = Gatekeeper(vault)
    dest = tmp_path / "out"
    req = gate.prepare([a, b], dest, "folder")
    assert {i.rel for i in req.items} == {"a.txt", "sub/b.txt"}
    gate.approve(req)
    res = gate.execute(req)
    assert res.verified and (dest / "a.txt").read_text(encoding="utf-8") == "alpha"
    assert (dest / "sub" / "b.txt").exists()
    manifest = (dest / "manifest.txt").read_text(encoding="utf-8")
    assert sha256_file(a) in manifest and "sub/b.txt" in manifest
    ops = [r["op"] for r in vault.log.tail(10)]
    assert ops.count("export") == 2 and ops[-1] == "export_done"
    assert a.exists(), "export copies; originals stay in Staging"


def test_zip_export_verifies_by_reading_back(vault: VaultOps, tmp_path: Path) -> None:
    a = _mk(vault.paths.staging / "a.txt", "alpha")
    gate = Gatekeeper(vault)
    req = gate.prepare([a], tmp_path / "pack", "zip")
    gate.approve(req)
    res = gate.execute(req)
    assert res.verified and res.destination.suffix == ".zip"
    with zipfile.ZipFile(res.destination) as zf:
        assert set(zf.namelist()) == {"a.txt", "manifest.txt"}
        assert zf.read("a.txt") == b"alpha"


def test_execute_without_approval_refuses(vault: VaultOps, tmp_path: Path) -> None:
    a = _mk(vault.paths.staging / "a.txt", "alpha")
    gate = Gatekeeper(vault)
    req = gate.prepare([a], tmp_path / "out")
    with pytest.raises(GateError):
        gate.execute(req)
    assert not (tmp_path / "out").exists()


def test_approval_is_one_use(vault: VaultOps, tmp_path: Path) -> None:
    a = _mk(vault.paths.staging / "a.txt", "alpha")
    gate = Gatekeeper(vault)
    req = gate.prepare([a], tmp_path / "out")
    gate.approve(req)
    gate.execute(req)
    with pytest.raises(GateError):
        gate.execute(req)


def test_approval_expires(vault: VaultOps, tmp_path: Path) -> None:
    a = _mk(vault.paths.staging / "a.txt", "alpha")
    gate = Gatekeeper(vault)
    req = gate.prepare([a], tmp_path / "out")
    ap = gate.approve(req)
    ap.granted_at -= APPROVAL_TTL_S + 1
    with pytest.raises(GateError):
        gate.execute(req)


def test_changed_since_preview_refuses(vault: VaultOps, tmp_path: Path) -> None:
    a = _mk(vault.paths.staging / "a.txt", "alpha")
    gate = Gatekeeper(vault)
    req = gate.prepare([a], tmp_path / "out")
    gate.approve(req)
    a.write_text("tampered", encoding="utf-8")
    with pytest.raises(GateError, match="changed since preview"):
        gate.execute(req)
    assert not (tmp_path / "out").exists()


def test_only_staging_can_leave(vault: VaultOps, tmp_path: Path) -> None:
    d = _mk(vault.paths.documents / "d.txt", "doc")
    p = _mk(vault.paths.personal / "p.txt", "priv")
    gate = Gatekeeper(vault)
    for src in (d, p):
        with pytest.raises(GateError, match="only Staging"):
            gate.prepare([src], tmp_path / "out")


def test_destination_must_be_outside_vault(vault: VaultOps) -> None:
    a = _mk(vault.paths.staging / "a.txt", "alpha")
    gate = Gatekeeper(vault)
    with pytest.raises(GateError, match="outside the vault"):
        gate.prepare([a], vault.paths.documents / "leak")


def test_non_empty_destination_refuses(vault: VaultOps, tmp_path: Path) -> None:
    a = _mk(vault.paths.staging / "a.txt", "alpha")
    dest = tmp_path / "out"
    _mk(dest / "existing.txt", "x")
    gate = Gatekeeper(vault)
    req = gate.prepare([a], dest)
    gate.approve(req)
    with pytest.raises(GateError, match="not empty"):
        gate.execute(req)


@pytest.mark.parametrize("name", ["manifest.txt", "MANIFEST.TXT", "MaNiFeSt.TxT"])
@pytest.mark.parametrize("mode", ["folder", "zip"])
def test_reserved_manifest_refuses_at_preview(vault: VaultOps, tmp_path: Path, name: str, mode: str) -> None:
    ordinary = _mk(vault.paths.staging / "a.txt", "approved ordinary file")
    source = _mk(vault.paths.staging / name, "approved user manifest")
    dest = tmp_path / "new-parent" / "out"
    gate = Gatekeeper(vault)
    receipts_before = vault.log.tail()

    with pytest.raises(GateError, match="reserved.*manifest.txt"):
        gate.prepare([ordinary, source], dest, mode)

    assert not dest.parent.exists(), "refusal must precede destination or parent creation"
    assert vault.log.tail() == receipts_before, "a refused export must not append receipts"
    assert source.read_text(encoding="utf-8") == "approved user manifest"
    assert ordinary.read_text(encoding="utf-8") == "approved ordinary file"


@pytest.mark.parametrize("name", ["manifest.txt", "MANIFEST.TXT"])
@pytest.mark.parametrize("mode", ["folder", "zip"])
def test_manifest_directory_refuses_at_preview(vault: VaultOps, tmp_path: Path, name: str, mode: str) -> None:
    ordinary = _mk(vault.paths.staging / "a.txt", "approved ordinary file")
    source = _mk(vault.paths.staging / name / "record.txt", "approved user record")
    dest = tmp_path / "new-parent" / "out"
    gate = Gatekeeper(vault)
    receipts_before = vault.log.tail()

    with pytest.raises(GateError, match="reserved.*manifest.txt"):
        gate.prepare([ordinary, source.parent], dest, mode)

    assert not dest.parent.exists()
    assert vault.log.tail() == receipts_before
    assert source.read_text(encoding="utf-8") == "approved user record"
    assert ordinary.read_text(encoding="utf-8") == "approved ordinary file"


@pytest.mark.parametrize("name", ["manifest.txt", "MANIFEST.TXT"])
def test_folder_nested_manifest_is_preserved(vault: VaultOps, tmp_path: Path, name: str) -> None:
    source = _mk(vault.paths.staging / "reports" / name, "approved nested manifest")
    dest = tmp_path / "out"
    gate = Gatekeeper(vault)
    req = gate.prepare([source.parent], dest, "folder")
    gate.approve(req)

    result = gate.execute(req)

    assert result.verified
    assert (dest / "reports" / name).read_text(encoding="utf-8") == "approved nested manifest"
    assert (dest / "manifest.txt").read_text(encoding="utf-8").startswith("VAULT EXPORT\n")
    assert [r["op"] for r in vault.log.tail()] == ["export", "export_done"]


@pytest.mark.parametrize("name", ["manifest.txt", "MANIFEST.TXT"])
def test_zip_nested_manifest_is_preserved(vault: VaultOps, tmp_path: Path, name: str) -> None:
    source = _mk(vault.paths.staging / "reports" / name, "approved nested manifest")
    gate = Gatekeeper(vault)
    req = gate.prepare([source.parent], tmp_path / "out", "zip")
    gate.approve(req)

    result = gate.execute(req)

    assert result.verified
    with zipfile.ZipFile(result.destination) as archive:
        assert archive.namelist() == [f"reports/{name}", "manifest.txt"]
        assert archive.read(f"reports/{name}") == b"approved nested manifest"
        assert archive.read("manifest.txt").startswith(b"VAULT EXPORT\n")
    assert source.read_text(encoding="utf-8") == "approved nested manifest"
    assert [r["op"] for r in vault.log.tail()] == ["export", "export_done"]


@pytest.mark.parametrize("requested_name", ["out", "out.ZIP"])
def test_zip_manifest_refusal_preserves_existing_destination(vault: VaultOps, tmp_path: Path, requested_name: str) -> None:
    source = _mk(vault.paths.staging / "MANIFEST.TXT", "approved user manifest")
    destination = tmp_path / requested_name
    actual = tmp_path / ("out.zip" if requested_name == "out" else "out.ZIP")
    actual.write_bytes(b"existing destination bytes")
    gate = Gatekeeper(vault)
    receipts_before = vault.log.tail()

    with pytest.raises(GateError, match="reserved.*manifest.txt"):
        gate.prepare([source], destination, "zip")

    assert actual.read_bytes() == b"existing destination bytes"
    assert not (tmp_path / "out").exists()
    assert source.read_text(encoding="utf-8") == "approved user manifest"
    assert vault.log.tail() == receipts_before


@pytest.mark.parametrize("relative", ["manifest.txt", "MANIFEST.TXT", "manifest.txt/record.txt"])
@pytest.mark.parametrize("mode", ["folder", "zip"])
def test_execute_rechecks_reserved_manifest_for_direct_request(vault: VaultOps, tmp_path: Path, relative: str, mode: str) -> None:
    source = _mk(vault.paths.staging / relative, "synthetic direct request")
    dest = tmp_path / "new-parent" / "out"
    # Public value objects deliberately bypass prepare for this defense-in-depth
    # check; this is not a claim that arbitrary constructed requests are safe.
    req = ExportRequest(
        id="synthetic-direct-request",
        items=(ExportItem(source, relative, source.stat().st_size, sha256_file(source)),),
        destination=dest,
        mode=mode,
        created_at=0,
        digest="synthetic-direct-descriptor",
    )
    gate = Gatekeeper(vault)
    gate.approve(req)
    receipts_before = vault.log.tail()

    with pytest.raises(GateError, match="reserved.*manifest.txt"):
        gate.execute(req)

    assert not dest.parent.exists()
    assert source.read_text(encoding="utf-8") == "synthetic direct request"
    assert vault.log.tail() == receipts_before
