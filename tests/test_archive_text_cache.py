"""Archive text preparation through the agreed DocumentTextCache interface."""
from contextlib import contextmanager
from pathlib import Path

import pytest

from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.errors import VaultError
from vault_v2.text_cache import DocumentTextCache


@pytest.mark.parametrize("suffix", [".txt", ".md", ".csv", ".json", ".log"])
def test_plain_text_can_be_prepared_and_reused_as_utf8_replacement_snapshot(tmp_path, suffix):
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    source = ops.paths.personal / f"synthetic{suffix}"
    source.write_bytes("Иммиграция\n".encode("utf-8") + b"invalid: \xff")
    cache = DocumentTextCache(ops.paths, ops.log)

    assert cache.read_cached_snapshot(source) is None
    prepared = cache.read_snapshot(source)
    assert prepared.text == "Иммиграция\ninvalid: \ufffd"
    assert cache.read_cached_snapshot(source) == prepared
    assert len([row for row in ops.log.tail() if row["op"] == "text_extracted"]) == 1


@pytest.mark.parametrize("method", ["read_snapshot", "read_cached_snapshot"])
def test_revoked_permission_refuses_before_source_or_existing_cache_read(tmp_path, monkeypatch, method):
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    source = ops.paths.personal / "synthetic.txt"
    source.write_text("sensitive synthetic text", encoding="utf-8")
    cache = DocumentTextCache(ops.paths, ops.log)
    cache.read_snapshot(source)
    original_open = Path.open
    refusal = VaultError("text permission revoked")

    def authorize():
        raise refusal

    def no_document_read(path, *args, **kwargs):
        if path == source or cache.directory in path.parents:
            pytest.fail("revoked source and cache must not be opened")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", no_document_read)
    with pytest.raises(VaultError) as result:
        getattr(cache, method)(source, authorize=authorize)
    assert result.value is refusal


@pytest.mark.parametrize("method", ["read_snapshot", "read_cached_snapshot"])
def test_permission_revoked_after_source_capture_does_not_open_cached_text(tmp_path, monkeypatch, method):
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    source = ops.paths.personal / "synthetic.txt"
    source.write_text("sensitive synthetic text", encoding="utf-8")
    cache = DocumentTextCache(ops.paths, ops.log)
    cache.read_snapshot(source)
    original_open = Path.open
    revoked = False

    def authorize():
        if revoked:
            raise VaultError("text permission revoked")

    @contextmanager
    def revoke_after_source(path, *args, **kwargs):
        nonlocal revoked
        if cache.directory in path.parents and revoked:
            pytest.fail("cache content must not be opened after revocation")
        with original_open(path, *args, **kwargs) as stream:
            yield stream
        if path == source:
            revoked = True

    monkeypatch.setattr(Path, "open", revoke_after_source)
    with pytest.raises(VaultError, match="text permission revoked"):
        getattr(cache, method)(source, authorize=authorize)


def test_publication_requires_authorization_under_the_root_guard(tmp_path):
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    source = ops.paths.personal / "synthetic.txt"
    source.write_text("sensitive synthetic text", encoding="utf-8")
    cache = DocumentTextCache(ops.paths, ops.log)

    def authorize():
        if ops.log.guard.busy:
            raise VaultError("permission refused at publication")

    with pytest.raises(VaultError, match="permission refused at publication"):
        cache.read_snapshot(source, authorize=authorize)
    assert cache.read_cached_snapshot(source) is None
    assert not any(row["op"] == "text_extracted" for row in ops.log.tail())


@pytest.mark.parametrize("method", ["read_snapshot", "read_cached_snapshot"])
def test_permission_revoked_during_cache_read_cannot_return_the_text(tmp_path, monkeypatch, method):
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    source = ops.paths.personal / "synthetic.txt"
    source.write_text("sensitive synthetic text", encoding="utf-8")
    cache = DocumentTextCache(ops.paths, ops.log)
    cache.read_snapshot(source)
    original_open = Path.open
    revoked = False

    def authorize():
        if revoked:
            raise VaultError("text permission revoked")

    @contextmanager
    def revoke_after_cache(path, *args, **kwargs):
        nonlocal revoked
        if path == source and revoked:
            pytest.fail("source must not be reread after revocation")
        with original_open(path, *args, **kwargs) as stream:
            yield stream
        if path.parent == cache.directory and path.suffix == ".txt":
            revoked = True

    monkeypatch.setattr(Path, "open", revoke_after_cache)
    with pytest.raises(VaultError, match="text permission revoked"):
        getattr(cache, method)(source, authorize=authorize)


@pytest.mark.parametrize("method", ["read_snapshot", "read_cached_snapshot"])
def test_permission_is_checked_again_before_handing_snapshot_to_caller(tmp_path, monkeypatch, method):
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    source = ops.paths.personal / "synthetic.txt"
    source.write_text("sensitive synthetic text", encoding="utf-8")
    cache = DocumentTextCache(ops.paths, ops.log)
    if method == "read_cached_snapshot":
        cache.read_snapshot(source)
    original_open = Path.open
    revoked = cache_read = False

    def authorize():
        if revoked:
            raise VaultError("text permission revoked")

    @contextmanager
    def revoke_after_last_io(path, *args, **kwargs):
        nonlocal revoked, cache_read
        with original_open(path, *args, **kwargs) as stream:
            yield stream
        if path.parent == cache.directory and path.suffix == ".txt":
            cache_read = True
        if ((method == "read_snapshot" and path == ops.log.file and args[0] == "a")
                or (method == "read_cached_snapshot" and path == source and cache_read)):
            revoked = True

    monkeypatch.setattr(Path, "open", revoke_after_last_io)
    with pytest.raises(VaultError, match="text permission revoked"):
        getattr(cache, method)(source, authorize=authorize)


@pytest.mark.parametrize("suffix", [".png", ".pdf", ".unknown"])
def test_plaintext_cache_does_not_authorize_another_extractor_profile(tmp_path, suffix):
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    source = ops.paths.personal / "synthetic.txt"
    source.write_bytes(b"synthetic plain bytes")
    cache = DocumentTextCache(ops.paths, ops.log)
    cache.read_snapshot(source)
    other = source.with_suffix(suffix)
    other.write_bytes(source.read_bytes())
    assert cache.read_cached_snapshot(other) is None


@pytest.mark.parametrize("claimed_suffix", [".pdf", ".unknown", []])
def test_plaintext_cache_requires_a_supported_source_suffix_in_metadata(tmp_path, claimed_suffix):
    import json
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    source = ops.paths.personal / "synthetic.txt"
    source.write_bytes(b"synthetic plain bytes")
    cache = DocumentTextCache(ops.paths, ops.log)
    cache.read_snapshot(source)
    manifest = next(cache.directory.glob("*.json"))
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["source_suffix"] = claimed_suffix
    manifest.write_text(json.dumps(data), encoding="utf-8")
    assert cache.read_cached_snapshot(source) is None
