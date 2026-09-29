"""Export source policy through preview and approved execution."""

import os
from dataclasses import replace
from pathlib import Path
import zipfile

import pytest

from vault_v2.gatekeeper import GateError, Gatekeeper
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths


@pytest.fixture()
def vault(tmp_path):
    return VaultOps(VaultPaths(tmp_path / "vault"))


def make_file(path, text="synthetic export bytes"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize("relative", [".private/document.txt", "records.trash.json/document.txt",
                                      "records.TRASH.JSON/document.txt", ".draft", "meta.TRASH.JSON"])
def test_prepare_refuses_hidden_ancestor_before_reading(vault, tmp_path, monkeypatch, relative):
    source = make_file(vault.paths.staging / relative)
    destination = tmp_path / "new-output" / "pack"
    receipts = vault.log.tail()
    original_open = Path.open

    def refuse_source_read(path, *args, **kwargs):
        if path == source:
            pytest.fail("hidden source was opened before refusal")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", refuse_source_read)
    with pytest.raises(GateError):
        Gatekeeper(vault).prepare([source], destination)

    assert not destination.parent.exists()
    assert vault.log.tail() == receipts


def test_prepare_folder_omits_hidden_descendants_without_reading(vault, tmp_path, monkeypatch):
    visible = make_file(vault.paths.staging / "reports" / "visible.txt")
    hidden = make_file(visible.parent / ".private" / "document.txt")
    original_open = Path.open

    def refuse_hidden_read(path, *args, **kwargs):
        if path == hidden:
            pytest.fail("recursive export opened a hidden descendant")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", refuse_hidden_read)
    request = Gatekeeper(vault).prepare([visible.parent], tmp_path / "pack")

    assert [item.rel for item in request.items] == ["reports/visible.txt"]
    assert vault.log.tail() == []


@pytest.mark.skipif(os.name != "nt", reason="Windows junction boundary")
@pytest.mark.parametrize("mode", ["folder", "zip"])
def test_execute_refuses_changed_source_junction_before_reading_or_writing(vault, tmp_path, monkeypatch, mode):
    import _winapi

    source = make_file(vault.paths.staging / "reports" / "document.txt")
    destination = tmp_path / "new-output" / "pack"
    gate = Gatekeeper(vault)
    request = gate.prepare([source], destination, mode)
    approval = gate.approve(request)
    receipts = vault.log.tail()
    target = vault.paths.staging / "moved-reports"
    source.parent.rename(target)
    _winapi.CreateJunction(str(target), str(source.parent))
    try:
        original_open = Path.open

        def refuse_linked_read(path, *args, **kwargs):
            if path == source:
                pytest.fail("execution opened a source replaced by a junction")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", refuse_linked_read)
        with pytest.raises(GateError):
            gate.execute(request)

        assert approval.used
        assert not destination.parent.exists()
        assert vault.log.tail() == receipts
        assert (target / "document.txt").read_text(encoding="utf-8") == "synthetic export bytes"
    finally:
        source.parent.rmdir()  # Remove only the synthetic junction, not its target.


@pytest.mark.parametrize("mode", ["folder", "zip"])
def test_export_manifest_discloses_hidden_boundary_items(vault, tmp_path, mode):
    visible = make_file(vault.paths.staging / "reports" / "visible.txt")
    make_file(visible.parent / ".private" / "first.txt")
    make_file(visible.parent / ".private" / "nested" / "second.txt")
    make_file(visible.parent / ".draft")
    make_file(visible.parent / "ignored.TRASH.JSON")
    gate = Gatekeeper(vault)
    request = gate.prepare([visible.parent], tmp_path / "pack", mode)

    assert request.hidden_count == 3
    assert [item.rel for item in request.items] == ["reports/visible.txt"]
    gate.approve(request)
    result = gate.execute(request)
    if mode == "zip":
        with zipfile.ZipFile(result.destination) as archive:
            manifest = archive.read("manifest.txt").decode("utf-8")
            assert set(archive.namelist()) == {"reports/visible.txt", "manifest.txt"}
    else:
        manifest = (result.destination / "manifest.txt").read_text(encoding="utf-8")
        assert sorted(p.relative_to(result.destination).as_posix()
                      for p in result.destination.rglob("*") if p.is_file()) == [
                          "manifest.txt", "reports/visible.txt"]
    assert result.verified
    assert "3 hidden items not exported" in manifest
    assert "A hidden folder counts as one item; its contents are not inspected." in manifest


def test_preview_digest_binds_hidden_item_disclosure(vault, tmp_path):
    visible = make_file(vault.paths.staging / "reports" / "visible.txt")
    hidden = make_file(visible.parent / ".private" / "document.txt")
    gate = Gatekeeper(vault)
    destination = tmp_path / "pack"
    with_hidden = gate.prepare([visible.parent], destination)
    hidden.parent.rename(tmp_path / "parked-private-folder")
    without_hidden = gate.prepare([visible.parent], destination)

    assert with_hidden.items == without_hidden.items
    assert (with_hidden.hidden_count, without_hidden.hidden_count) == (1, 0)
    assert with_hidden.digest != without_hidden.digest


@pytest.mark.skipif(os.name != "nt", reason="Windows junction boundary")
@pytest.mark.parametrize("location", ["inside", "outside"])
@pytest.mark.parametrize("selection", ["file", "folder"])
def test_prepare_refuses_explicit_junction_alias_before_reading(vault, tmp_path, monkeypatch, location, selection):
    import _winapi

    source = make_file(vault.paths.staging / "plain" / "document.txt")
    link = (vault.paths.staging if location == "inside" else tmp_path) / "linked"
    _winapi.CreateJunction(str(source.parent), str(link))
    try:
        routed_file = link / source.name
        selected = routed_file if selection == "file" else link
        original_open = Path.open

        def refuse_alias_read(path, *args, **kwargs):
            if path in (source, routed_file):
                pytest.fail("explicit junction source was opened")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", refuse_alias_read)
        with pytest.raises(GateError):
            Gatekeeper(vault).prepare([selected], tmp_path / "new-output" / "pack")
        assert not (tmp_path / "new-output").exists()
        assert vault.log.tail() == []
    finally:
        link.rmdir()  # Remove only the synthetic junction, not its target.


@pytest.mark.skipif(os.name != "nt", reason="Windows junction boundary")
@pytest.mark.parametrize("alias_name", ["linked", ".hidden-link"])
def test_prepare_recursion_prunes_junction_without_counting_it_hidden(vault, tmp_path, monkeypatch, alias_name):
    import _winapi

    visible = make_file(vault.paths.staging / "reports" / "visible.txt")
    target = make_file(vault.paths.staging / "plain" / "document.txt")
    link = visible.parent / alias_name
    _winapi.CreateJunction(str(target.parent), str(link))
    try:
        original_open = Path.open
        original_iterdir = Path.iterdir

        def refuse_link_read(path, *args, **kwargs):
            if path == target or path.is_relative_to(link):
                pytest.fail("recursive export read a linked document")
            return original_open(path, *args, **kwargs)

        def refuse_link_walk(path):
            if path == link or path.is_relative_to(link):
                pytest.fail("recursive export entered a junction")
            return original_iterdir(path)

        monkeypatch.setattr(Path, "open", refuse_link_read)
        monkeypatch.setattr(Path, "iterdir", refuse_link_walk)
        request = Gatekeeper(vault).prepare([visible.parent], tmp_path / "pack")
        assert [item.rel for item in request.items] == ["reports/visible.txt"]
        assert request.hidden_count == 0
        assert vault.log.tail() == []
    finally:
        link.rmdir()  # Remove only the synthetic junction, not its target.


def test_hidden_count_deduplicates_overlapping_selected_folders(vault, tmp_path, monkeypatch):
    visible = make_file(vault.paths.staging / "reports" / "nested" / "visible.txt")
    hidden = make_file(visible.parent / ".private" / "document.txt")
    original_iterdir = Path.iterdir

    def refuse_hidden_walk(path):
        if path == hidden.parent or path.is_relative_to(hidden.parent):
            pytest.fail("hidden folder was traversed while counting omissions")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", refuse_hidden_walk)
    request = Gatekeeper(vault).prepare([visible.parent.parent, visible.parent], tmp_path / "pack")
    assert request.hidden_count == 1


def test_relative_configured_vault_exports_visible_source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    vault = VaultOps(VaultPaths(Path("synthetic-vault")))
    source = make_file(vault.paths.staging / "reports" / "document.txt")
    gate = Gatekeeper(vault)
    request = gate.prepare([source], tmp_path / "output")
    gate.approve(request)
    result = gate.execute(request)

    assert result.verified
    assert request.items[0].path.is_absolute()
    assert (result.destination / "reports" / "document.txt").read_text(encoding="utf-8") == "synthetic export bytes"


@pytest.mark.parametrize("mode", ["folder", "zip"])
def test_changed_hidden_disclosure_cannot_reuse_approval(vault, tmp_path, mode):
    visible = make_file(vault.paths.staging / "reports" / "visible.txt")
    make_file(visible.parent / ".private" / "document.txt")
    destination = tmp_path / "new-output" / "pack"
    gate = Gatekeeper(vault)
    request = gate.prepare([visible.parent], destination, mode)
    approval = gate.approve(request)
    receipts = vault.log.tail()
    changed = replace(request, hidden_count=0)

    with pytest.raises(GateError, match="changed since preview"):
        gate.execute(changed)

    assert approval.used
    assert not destination.parent.exists()
    assert vault.log.tail() == receipts
