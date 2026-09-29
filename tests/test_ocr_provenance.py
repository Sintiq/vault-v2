"""OCR provenance through the agreed document-cache read interfaces."""
from pathlib import Path
import subprocess
import json
from io import BytesIO

import pytest
from pypdf import PdfReader, PdfWriter

from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.text_cache import DocumentTextCache
from vault_v2.errors import VaultError
from tools.synthetic_ocr_files import scan_file, image_bytes


def report_engine_version(monkeypatch, version):
    """Replace only the native engine's version report in real OCR children."""
    original = subprocess.Popen
    versions = iter(version) if isinstance(version, tuple) else None

    def launch(argv, **kwargs):
        if isinstance(argv, list) and Path(argv[-1]).name == "ocr_worker.py":
            reported = next(versions) if versions is not None else version
            script = (
                "import runpy, tesserocr; "
                f"tesserocr.tesseract_version=lambda: {reported!r}; "
                f"runpy.run_path({argv[-1]!r}, run_name='__main__')"
            )
            argv = [*argv[:3], "-c", script]
        return original(argv, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", launch)


@pytest.mark.parametrize("version", ["5.5.2", "5.5.3"])
@pytest.mark.parametrize("suffix", [".png", ".pdf"])
def test_cached_ocr_provenance_uses_the_recognizing_child_version(tmp_path, monkeypatch, version, suffix):
    report_engine_version(monkeypatch, "tesseract " + version)
    ops = VaultOps(VaultPaths(tmp_path / "synthetic-vault"))
    source = scan_file(ops.paths.staging / ("scan" + suffix))
    cache = DocumentTextCache(ops.paths, ops.log)

    result = cache.read_snapshot(source)
    assert "Payment is due by October 15, 2026" in result.text
    recorded = result.extraction_metadata["version"]
    assert recorded.split("; ")[-1] == "Tesseract " + version
    assert cache.read_cached_snapshot(source) == result


def test_legacy_inaccurate_profile_is_not_reused_or_relabelled(tmp_path):
    from vault_v2.text_extract import PDF_VERSION

    ops = VaultOps(VaultPaths(tmp_path / "synthetic-vault"))
    source = scan_file(ops.paths.staging / "scan.png")
    cache = DocumentTextCache(ops.paths, ops.log)
    first = cache.read_snapshot(source)
    metadata_path = cache.directory / (first.source_sha256 + ".json")
    legacy = dict(first.extraction_metadata)
    legacy["profile"] = f"vault-text-v1-{PDF_VERSION}-tesseract5.5.2-eng+rus-fast87416418-native20-pages50"
    legacy["version"] = "Tesseract 5.5.2"
    metadata_path.write_text(json.dumps(legacy), encoding="utf-8")
    before = metadata_path.read_bytes()
    assert cache.read_cached_snapshot(source) is None
    assert metadata_path.read_bytes() == before
    refreshed = cache.read_snapshot(source)
    assert refreshed.extraction_metadata["profile"] != legacy["profile"]
    assert cache.read_cached_snapshot(source) == refreshed
    assert len([r for r in ops.log.tail() if r["op"] == "text_extracted"]) == 2


@pytest.mark.parametrize("reply", [
    {"text": "synthetic text"},
    {"text": "synthetic text", "version": "tesseract 5.5.30"},
    {"text": "synthetic text", "version": []},
    {"text": "synthetic text", "version": "tesseract 5.5.3", "extra": True},
])
def test_invalid_child_provenance_cannot_publish_text_or_metadata(tmp_path, monkeypatch, reply):
    from test_ocr_process import synthetic_child

    synthetic_child(monkeypatch, "print(" + repr(json.dumps(reply)) + ")")
    ops = VaultOps(VaultPaths(tmp_path / "synthetic-vault"))
    source = scan_file(ops.paths.staging / "scan.png")
    cache = DocumentTextCache(ops.paths, ops.log)
    with pytest.raises(VaultError, match="invalid child result"):
        cache.read_snapshot(source)
    assert not cache.directory.exists()
    assert ops.log.tail() == []


def test_pdf_with_different_ocr_versions_refuses_without_partial_cache(tmp_path, monkeypatch):
    report_engine_version(monkeypatch, ("tesseract 5.5.2", "tesseract 5.5.3"))
    ops = VaultOps(VaultPaths(tmp_path / "synthetic-vault"))
    source = ops.paths.staging / "two-scans.pdf"
    writer = PdfWriter()
    page = PdfReader(BytesIO(image_bytes(format="PDF"))).pages[0]
    writer.add_page(page)
    writer.add_page(page)
    with source.open("wb") as stream:
        writer.write(stream)
    cache = DocumentTextCache(ops.paths, ops.log)
    with pytest.raises(VaultError, match="OCR engine changed between pages"):
        cache.read_snapshot(source)
    assert not cache.directory.exists()
    assert ops.log.tail() == []
