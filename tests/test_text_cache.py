"""Derived text reuse through the agreed StagingReader interface."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from test_local_text import ENGLISH, text_pdf
from vault_v2.cards import CardStore
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.reader import StagingReader
from vault_v2.receipts import sha256_file
from vault_v2.errors import VaultError


def reader_vault(tmp_path):
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    reader = StagingReader(ops, CardStore(ops.paths.root / ".cards", ops.log))
    source = text_pdf(ops.paths.staging / "synthetic.pdf")
    return ops, reader, source


def test_repeated_agent_reads_reuse_one_receipted_source_cache(tmp_path):
    ops, reader, source = reader_vault(tmp_path)
    assert ENGLISH in reader.read_text(source.name)
    assert ENGLISH in reader.read_text(source.name)

    extracted = [row for row in ops.log.tail() if row["op"] == "text_extracted"]
    assert len(extracted) == 1
    assert extracted[0]["sha256"] == sha256_file(source)
    assert ENGLISH not in str(ops.log.tail())
    directory = ops.paths.root / ".text"
    assert sorted(path.suffix for path in directory.iterdir()) == [".json", ".txt"]
    metadata = json.loads(next(directory.glob("*.json")).read_text(encoding="utf-8"))
    assert metadata["source_sha256"] == extracted[0]["sha256"]
    assert metadata["pages"] == metadata["total_pages"] == 1
    assert metadata["engine"] == "pdfium+pypdf"
    assert not ops.log.pending()


@pytest.mark.parametrize("damage", ["missing-text", "missing-metadata", "invalid-json", "text-bytes",
                                   "old-profile", "source-digest", "bad-pages", "bad-languages",
                                   "bad-timestamp", "unexpected-field", "unknown-engine",
                                   "wrong-version", "wrong-engine-languages"])
def test_damaged_cache_never_replaces_the_document_text(tmp_path, damage):
    ops, reader, source = reader_vault(tmp_path)
    reader.read_text(source.name)
    directory = ops.paths.root / ".text"
    txt, meta = next(directory.glob("*.txt")), next(directory.glob("*.json"))
    if damage == "missing-text":
        txt.unlink()
    elif damage == "missing-metadata":
        meta.unlink()
    elif damage == "invalid-json":
        meta.write_text("{", encoding="utf-8")
    elif damage == "text-bytes":
        txt.write_text("NOT THE DOCUMENT TEXT", encoding="utf-8")
    else:
        metadata = json.loads(meta.read_text(encoding="utf-8"))
        field, value = {
            "old-profile": ("profile", "old extractor"),
            "source-digest": ("source_sha256", "0" * 64),
            "bad-pages": ("pages", True),
            "bad-languages": ("languages", ["untrusted"]),
            "bad-timestamp": ("timestamp", "not a time"),
            "unexpected-field": ("unknown", "untrusted"),
            "unknown-engine": ("engine", "unqualified-engine"),
            "wrong-version": ("version", "invented-version"),
            "wrong-engine-languages": ("languages", ["eng", "rus"]),
        }[damage]
        metadata[field] = value
        meta.write_text(json.dumps(metadata), encoding="utf-8")

    assert ENGLISH in reader.read_text(source.name)
    assert len([row for row in ops.log.tail() if row["op"] == "text_extracted"]) == 2


def test_changed_source_bytes_do_not_reuse_a_previous_cached_digest(tmp_path):
    ops, reader, source = reader_vault(tmp_path)
    reader.read_text(source.name)
    text_pdf(source, "A new synthetic invoice must be paid tomorrow")
    actual = reader.read_text(source.name)
    assert "A new synthetic invoice" in actual and ENGLISH not in actual
    assert ops.log.tail()[-1]["sha256"] == sha256_file(source)
    assert len(list((ops.paths.root / ".text").glob("*.json"))) == 2


def test_cache_write_failure_does_not_hand_text_to_agent_and_keeps_intention(tmp_path, monkeypatch):
    ops, reader, source = reader_vault(tmp_path)
    real_replace = __import__("os").replace

    def fail_metadata(src, dst, *args, **kwargs):
        if Path(dst).parent == ops.paths.root / ".text" and Path(dst).suffix == ".json":
            raise PermissionError("synthetic disk publication failure")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr("os.replace", fail_metadata)
    with pytest.raises(VaultError):
        reader.read_text(source.name)
    assert not [row for row in ops.log.tail() if row["op"] in ("agent_read", "text_extracted")]
    assert len(ops.log.pending()) == 1
    assert ops.log.guard.blocked


@pytest.mark.parametrize("change", ["purge", "replace"])
def test_source_changed_during_extraction_cannot_publish_or_authorize_text(tmp_path, monkeypatch, change):
    ops, reader, source = reader_vault(tmp_path)
    captured, release = Event(), Event()
    original_open = Path.open

    class PausedSnapshot(BytesIO):
        def read(self, *args):
            data = super().read(*args)
            captured.set()
            assert release.wait(10)
            return data

    def delayed_read(path, *args, **kwargs):
        if path == source and args and args[0] == "rb" and not captured.is_set():
            with original_open(path, *args, **kwargs) as stream:
                data = stream.read()
            return PausedSnapshot(data)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", delayed_read)
    with ThreadPoolExecutor(max_workers=1) as pool:
        reading = pool.submit(reader.read_text, source.name)
        try:
            assert captured.wait(5)
            if change == "purge":
                ops.trash(source)
                assert ops.purge_trash().removed == 1
            else:
                text_pdf(source, "Replacement document must not get old source text")
        finally:
            release.set()
        with pytest.raises(VaultError):
            reading.result(timeout=5)
    assert not (ops.paths.root / ".text").exists()
    assert not [row for row in ops.log.tail() if row["op"] in ("agent_read", "text_extracted")]


@pytest.mark.skipif(os.name != "nt", reason="Windows junction boundary")
def test_linked_cache_directory_is_never_read_or_written(tmp_path, monkeypatch):
    import _winapi

    ops, reader, source = reader_vault(tmp_path)
    outside = tmp_path / "outside-cache"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_bytes(b"outside synthetic marker")
    linked = ops.paths.root / ".text"
    _winapi.CreateJunction(str(outside), str(linked))
    original_open = Path.open

    def no_cache_open(path, *args, **kwargs):
        if linked in path.parents or outside in path.parents:
            raise AssertionError("linked cache must not be opened")
        return original_open(path, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "open", no_cache_open)
            with pytest.raises(VaultError):
                reader.read_text(source.name)
        assert marker.read_bytes() == b"outside synthetic marker"
        assert list(outside.iterdir()) == [marker]
        assert ops.log.tail() == []
    finally:
        linked.rmdir()  # Remove only the synthetic junction, not its target.


def test_simultaneous_reads_publish_one_cache_receipt(tmp_path, monkeypatch):
    from threading import Barrier

    ops, reader, source = reader_vault(tmp_path)
    ready = Barrier(2)
    original_open = Path.open
    seen_threads = set()

    class SimultaneousSnapshot(BytesIO):
        def read(self, *args):
            data = super().read(*args)
            ready.wait(timeout=5)
            return data

    def capture_both(path, *args, **kwargs):
        from threading import get_ident

        identity = get_ident()
        if path == source and args and args[0] == "rb" and identity not in seen_threads:
            seen_threads.add(identity)
            with original_open(path, *args, **kwargs) as stream:
                data = stream.read()
            return SimultaneousSnapshot(data)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", capture_both)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(reader.read_text, source.name) for _ in range(2)]
        assert all(ENGLISH in result.result(timeout=10) for result in results)
    assert len([row for row in ops.log.tail() if row["op"] == "text_extracted"]) == 1
    assert len([row for row in ops.log.tail() if row["op"] == "agent_read"]) == 2


def test_oversized_document_is_refused_before_reading_source_bytes(tmp_path, monkeypatch):
    ops, reader, source = reader_vault(tmp_path)
    original_stat, original_open = Path.stat, Path.open
    opened = []

    def huge_source(path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        if path == source:
            return SimpleNamespace(st_size=512 * 1024**2 + 1, st_mode=result.st_mode,
                                   st_file_attributes=getattr(result, "st_file_attributes", 0))
        return result

    def observed_open(path, *args, **kwargs):
        if path == source:
            opened.append(path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", huge_source)
    monkeypatch.setattr(Path, "open", observed_open)
    with pytest.raises(VaultError, match="512 MiB"):
        reader.read_text(source.name)
    assert opened == []
    assert ops.log.tail() == []
    assert not (ops.paths.root / ".text").exists()


def test_renamed_source_reuses_the_same_byte_addressed_cache(tmp_path):
    ops, reader, source = reader_vault(tmp_path)
    assert ENGLISH in reader.read_text(source.name)
    renamed = source.with_name("renamed-synthetic.pdf")
    source.rename(renamed)
    assert ENGLISH in reader.read_text(renamed.name)
    assert len([row for row in ops.log.tail() if row["op"] == "text_extracted"]) == 1
