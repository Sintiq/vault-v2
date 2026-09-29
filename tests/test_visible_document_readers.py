"""All document readers enforce the same visible-file rule before content I/O."""
from pathlib import Path
import os

import pytest

from vault_v2.ask import collect_docs
from vault_v2.cards import CardStore
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.api import VaultAPI
from vault_v2.errors import VaultError
from vault_v2.sorting import collect_inputs
from vault_v2.reader import StagingReader
from vault_v2.text_cache import DocumentTextCache


@pytest.fixture
def vault(tmp_path):
    ops = VaultOps(VaultPaths(tmp_path / "synthetic-vault"))
    store = CardStore(ops.paths.root / ".cards", ops.log)
    return ops, store


def test_ask_skips_hidden_ancestor_before_hashing(vault, monkeypatch):
    ops, store = vault
    visible = ops.paths.staging / "visible.txt"
    visible.write_text("SYNTHETIC VISIBLE", encoding="utf-8")
    hidden = ops.paths.staging / ".private" / "note.txt"
    hidden.parent.mkdir()
    hidden.write_text("SYNTHETIC HIDDEN", encoding="utf-8")
    original = Path.open

    def guarded_open(path, *args, **kwargs):
        if path == hidden:
            raise AssertionError("hidden document was opened or hashed")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    assert [row.path.name for row in collect_docs(ops.paths.staging, store)] == ["visible.txt"]


@pytest.mark.parametrize("method,suffix", [("read_file", ".txt"), ("blob", ".png")])
@pytest.mark.parametrize("folder", [".private", "nested/.private", "cache.TRASH.JSON"])
def test_phone_refuses_hidden_ancestors_before_read(vault, monkeypatch, method, suffix, folder):
    ops, store = vault
    hidden = ops.paths.staging / folder / ("synthetic" + suffix)
    hidden.parent.mkdir(parents=True)
    hidden.write_bytes(b"SYNTHETIC")
    original = Path.open

    def guarded_open(path, *args, **kwargs):
        if path == hidden:
            raise AssertionError("hidden document was opened")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    api = VaultAPI(ops, store, None, None)
    with pytest.raises(VaultError):
        getattr(api, method)("staging", hidden.relative_to(ops.paths.staging).as_posix())


def test_sort_without_reader_prunes_hidden_before_read(vault, monkeypatch):
    ops, store = vault
    good = ops.paths.staging / "visible.txt"
    good.write_text("SYNTHETIC VISIBLE", encoding="utf-8")
    hidden = ops.paths.staging / ".private" / "note.txt"
    hidden.parent.mkdir()
    hidden.write_text("SYNTHETIC HIDDEN", encoding="utf-8")
    original = Path.open

    def guarded_open(path, *args, **kwargs):
        if path == hidden:
            raise AssertionError("hidden sort input was opened")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    assert [row.path.name for row in collect_inputs(ops.paths.staging, store)] == ["visible.txt"]


@pytest.mark.parametrize("collector", [collect_docs, collect_inputs])
def test_collectors_do_not_expand_to_archive(vault, monkeypatch, collector):
    ops, store = vault
    source = ops.paths.personal / "synthetic.txt"
    source.write_text("SYNTHETIC PERSONAL", encoding="utf-8")
    original = Path.open

    def guarded_open(path, *args, **kwargs):
        if path == source:
            raise AssertionError("Staging collector read the archive")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    with pytest.raises(VaultError):
        collector(ops.paths.personal, store)
    assert ops.log.tail() == []


@pytest.mark.parametrize("folder", [".private", "nested/.private", "cache.TRASH.JSON"])
def test_enumerators_share_pruning_without_entering_hidden_dirs(vault, monkeypatch, folder):
    ops, store = vault
    good = ops.paths.staging / "plain" / "visible.txt"
    good.parent.mkdir()
    good.write_text("SYNTHETIC VISIBLE", encoding="utf-8")
    hidden = ops.paths.staging / folder
    hidden.mkdir(parents=True)
    (hidden / "note.txt").write_text("SYNTHETIC HIDDEN", encoding="utf-8")
    original = Path.iterdir

    def guarded_iterdir(path):
        if path == hidden:
            raise AssertionError("entered a hidden/service directory")
        return original(path)

    monkeypatch.setattr(Path, "iterdir", guarded_iterdir)
    assert [d.path for d in collect_docs(ops.paths.staging, store)] == [good]
    assert [d.path for d in collect_inputs(ops.paths.staging, store)] == [good]
    reader = StagingReader(ops, store)
    assert [d.rel for d in reader.list_staging()] == ["plain/visible.txt"]
    api = VaultAPI(ops, store, None, None)
    with pytest.raises(VaultError):
        api.list_pane("staging", folder)
    parent_rel = hidden.parent.relative_to(ops.paths.staging).as_posix()
    assert hidden.name not in [r["name"] for r in api.list_pane("staging", parent_rel)["items"]]
    assert ops.log.tail() == []


@pytest.mark.skipif(os.name != "nt", reason="Windows path normalization")
@pytest.mark.parametrize("alias", ["cache.TRASH.JSON.", "cache.TRASH.JSON "])
def test_windows_hidden_directory_alias_cannot_bypass_reader(vault, monkeypatch, alias):
    ops, store = vault
    hidden = ops.paths.staging / "cache.TRASH.JSON"
    hidden.mkdir()
    (hidden / "note.txt").write_text("SYNTHETIC HIDDEN", encoding="utf-8")
    original = Path.open

    def guarded_open(path, *args, **kwargs):
        if path.name == "note.txt":
            raise AssertionError("Windows alias bypassed service directory rule")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    with pytest.raises(VaultError):
        VaultAPI(ops, store, None, None).read_file("staging", alias + "/note.txt")


@pytest.mark.skipif(os.name != "nt", reason="Windows path casing")
def test_windows_pane_case_alias_has_same_visible_rule(vault):
    ops, store = vault
    source = ops.paths.staging / "note.txt"
    source.write_text("SYNTHETIC", encoding="utf-8")
    assert StagingReader(ops, store).read_text(ops.paths.root / "STAGING" / source.name) == "SYNTHETIC"


@pytest.mark.parametrize("method", ["read_file", "blob"])
@pytest.mark.parametrize("rel", ["../staging/file.txt", "nested/../file.txt", "C:file.txt", "file.txt:stream"])
def test_phone_rejects_path_aliases_before_io(vault, method, rel):
    ops, store = vault
    (ops.paths.staging / "file.txt").write_text("SYNTHETIC", encoding="utf-8")
    (ops.paths.staging / "nested").mkdir()
    with pytest.raises(VaultError):
        getattr(VaultAPI(ops, store, None, None), method)("staging", rel)
    assert ops.log.tail() == []


@pytest.mark.parametrize("method", ["reader", "ocr", "sort-selection"])
def test_selected_hidden_source_refused_before_content_or_cache(vault, monkeypatch, method):
    ops, store = vault
    source = ops.paths.staging / ".private" / "synthetic.pdf"
    source.parent.mkdir()
    source.write_bytes(b"SYNTHETIC, MUST NOT BE PARSED")
    original = Path.open

    def guarded_open(path, *args, **kwargs):
        if path == source or path.is_relative_to(ops.paths.root / ".text"):
            raise AssertionError("hidden source or OCR cache opened")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    with pytest.raises(VaultError):
        if method == "reader":
            StagingReader(ops, store).read_document(source)
        elif method == "ocr":
            DocumentTextCache(ops.paths, ops.log).read_snapshot(source)
        else:
            collect_inputs(ops.paths.staging, store, only=[source])
    assert ops.log.tail() == []


@pytest.mark.skipif(os.name != "nt", reason="Windows junction boundary")
@pytest.mark.parametrize("location", ["inside-pane", "vault-root", "ancestor-of-root"])
def test_all_readers_refuse_real_junction_before_open(vault, monkeypatch, tmp_path, location):
    import _winapi

    ops, store = vault
    source = ops.paths.staging / "plain" / "synthetic.pdf"
    source.parent.mkdir()
    source.write_bytes(b"SYNTHETIC, MUST NOT BE PARSED")
    if location == "inside-pane":
        link, target = ops.paths.staging / "linked", source.parent
        paths, rel = ops.paths, "linked/synthetic.pdf"
    elif location == "vault-root":
        link, target = tmp_path / "linked-root", ops.paths.root
        paths, rel = VaultPaths(link), "plain/synthetic.pdf"
    else:
        link, target = tmp_path.parent / (tmp_path.name + "-linked-parent"), tmp_path
        paths, rel = VaultPaths(link / ops.paths.root.name), "plain/synthetic.pdf"
    _winapi.CreateJunction(str(target), str(link))
    try:
        routed_ops = VaultOps(paths, read_only=True)
        routed_store = CardStore(paths.root / ".cards", routed_ops.log)
        routed_source = paths.staging / rel
        original = Path.open

        def guarded_open(path, *args, **kwargs):
            if path == routed_source or path == source:
                raise AssertionError("linked source was opened")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", guarded_open)
        api = VaultAPI(routed_ops, routed_store, None, None)
        calls = [lambda: api.blob("staging", rel), lambda: api.read_file("staging", rel),
                 lambda: StagingReader(routed_ops, routed_store).read_document(routed_source),
                 lambda: DocumentTextCache(paths, routed_ops.log).read_snapshot(routed_source),
                 lambda: collect_inputs(paths.staging, routed_store, only=[routed_source])]
        for call in calls:
            with pytest.raises(VaultError, match="Linked"):
                call()
        if location != "inside-pane":
            for call in (lambda: collect_docs(paths.staging, routed_store),
                         lambda: StagingReader(routed_ops, routed_store).list_staging(),
                         lambda: api.list_pane("staging")):
                with pytest.raises(VaultError, match="Linked"):
                    call()
    finally:
        link.rmdir()  # This synthetic junction only, never its target.


@pytest.mark.parametrize("pane", ["staging", "documents", "personal"])
def test_phone_visible_nested_files_still_read_without_receipts(vault, pane):
    ops, store = vault
    directory = ops.paths.pane(pane) / "plain"
    directory.mkdir()
    (directory / "note.txt").write_text("SYNTHETIC TEXT", encoding="utf-8")
    (directory / "image.png").write_bytes(b"SYNTHETIC IMAGE")
    api = VaultAPI(ops, store, None, None)
    assert api.read_file(pane, "plain/note.txt")["text"] == "SYNTHETIC TEXT"
    assert api.blob(pane, "plain/image.png")[0] == b"SYNTHETIC IMAGE"
    assert {r["name"] for r in api.list_pane(pane, "plain")["items"]} == {"note.txt", "image.png"}
    assert ops.log.tail() == []


def test_phone_supports_lexical_relative_vault_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    paths = VaultPaths(Path("synthetic-vault"))
    ops = VaultOps(paths)
    store = CardStore(paths.root / ".cards", ops.log)
    (paths.staging / "note.txt").write_text("SYNTHETIC TEXT", encoding="utf-8")
    (paths.staging / "image.png").write_bytes(b"SYNTHETIC IMAGE")
    api = VaultAPI(ops, store, None, None)
    assert len(api.list_pane("staging")["items"]) == 2
    assert api.read_file("staging", "note.txt")["text"] == "SYNTHETIC TEXT"
    assert api.blob("staging", "image.png")[0] == b"SYNTHETIC IMAGE"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction boundary")
@pytest.mark.parametrize("action", ["copy", "move", "trash"])
def test_phone_owner_operations_preserve_alias_for_link_refusal(vault, action):
    import _winapi

    ops, store = vault
    target = ops.paths.staging / "plain"
    target.mkdir()
    (target / "synthetic.txt").write_text("SYNTHETIC", encoding="utf-8")
    link = ops.paths.staging / "linked"
    _winapi.CreateJunction(str(target), str(link))
    try:
        api = VaultAPI(ops, store, None, None)
        with pytest.raises(VaultError, match="link or junction"):
            if action == "trash":
                api.trash("staging", "linked/synthetic.txt")
            else:
                api.transfer("staging", "linked/synthetic.txt", "documents", move=action == "move")
        assert (target / "synthetic.txt").read_text(encoding="utf-8") == "SYNTHETIC"
        assert list(ops.paths.documents.iterdir()) == []
        assert list(ops.paths.trash.iterdir()) == []
        assert ops.log.tail() == []
    finally:
        link.rmdir()  # This synthetic junction only.
