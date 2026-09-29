"""Synthetic document extraction through the agreed StagingReader seam."""
from pathlib import Path
import pytest

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from vault_v2.cards import CardStore
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.reader import StagingReader, ReadRefused
from tools.synthetic_viewer_files import form_pdf
from tools.synthetic_ocr_files import scan_file, RUSSIAN, text_pdf

ENGLISH = "Payment is due by October 15, 2026"


def test_agent_reads_native_pdf_text_with_bounded_receipt(tmp_path):
    ops = VaultOps(VaultPaths(tmp_path / "synthetic-vault"))
    cards = CardStore(ops.paths.root / ".cards", ops.log)
    source = text_pdf(ops.paths.staging / "synthetic-invoice.pdf")
    before = source.read_bytes()
    text = StagingReader(ops, cards).read_text(source.name)
    assert ENGLISH in text and len(text) <= 6000
    assert source.read_bytes() == before
    receipt = ops.log.tail()[-1]
    assert receipt["op"] == "agent_read"
    assert receipt["sha256"] == cards.hash_of(source)
    assert receipt["extra"]["chars"] == len(text)
    assert ENGLISH not in str(ops.log.tail())


@pytest.mark.parametrize("suffix,phrase", [(".png", ENGLISH), (".pdf", ENGLISH), (".png", RUSSIAN)])
def test_reader_reads_local_scan_or_photo(tmp_path, suffix, phrase, monkeypatch):
    import socket
    def no_network(*args, **kwargs):
        raise AssertionError("Unexpected Python network in document extraction")
    monkeypatch.setattr(socket, "socket", no_network)
    ops = VaultOps(VaultPaths(tmp_path / "synthetic-vault"))
    cards = CardStore(ops.paths.root / ".cards", ops.log)
    source = scan_file(ops.paths.staging / ("scan" + suffix), phrase)
    before = source.read_bytes()
    text = StagingReader(ops, cards).read_text(source.name)
    assert phrase in text
    assert source.read_bytes() == before
    receipt = ops.log.tail()[-1]
    assert receipt["op"] == "agent_read"
    assert receipt["sha256"] == cards.hash_of(source)
    assert receipt["extra"]["chars"] == len(text)
    assert ENGLISH not in str(ops.log.tail())


@pytest.mark.parametrize("encrypted,appearance", [(False, True), (False, False), (True, False)])
def test_reader_reads_actual_form_value_not_only_appearance(tmp_path, encrypted, appearance):
    ops = VaultOps(VaultPaths(tmp_path / "synthetic-vault"))
    cards = CardStore(ops.paths.root / ".cards", ops.log)
    source = form_pdf(ops.paths.staging / "form.pdf", value=ENGLISH,
                      encrypted=encrypted, with_appearance=appearance,
                      encryption_algorithm="AES-256" if encrypted else None)
    before = source.read_bytes()
    text = StagingReader(ops, cards).read_text(source.name)
    assert "synthetic_field: " + ENGLISH in text
    assert source.read_bytes() == before


def test_scanned_background_is_not_hidden_by_a_filled_form_widget(tmp_path):
    ops = VaultOps(VaultPaths(tmp_path / "synthetic-vault"))
    cards = CardStore(ops.paths.root / ".cards", ops.log)
    template = form_pdf(tmp_path / "template.pdf", value="Approved control total")
    scan = scan_file(tmp_path / "scan.pdf")
    writer = PdfWriter(clone_from=template)
    page = writer.pages[0]
    page.mediabox.upper_right = (1600, 500)
    page.merge_page(PdfReader(scan).pages[0])
    source = ops.paths.staging / "scan-with-form.pdf"
    with source.open("wb") as stream:
        writer.write(stream)
    text = StagingReader(ops, cards).read_text(source.name)
    assert "synthetic_field: Approved control total" in text
    assert ENGLISH in text


def test_valid_indirect_annotation_array_is_supported(tmp_path):
    ops = VaultOps(VaultPaths(tmp_path / "synthetic-vault"))
    cards = CardStore(ops.paths.root / ".cards", ops.log)
    template = form_pdf(tmp_path / "template.pdf", value=ENGLISH)
    writer = PdfWriter(clone_from=template)
    page = writer.pages[0]
    page[NameObject("/Annots")] = writer._add_object(page["/Annots"])
    source = ops.paths.staging / "indirect.pdf"
    with source.open("wb") as stream:
        writer.write(stream)
    assert "synthetic_field: " + ENGLISH in StagingReader(ops, cards).read_text(source.name)


def test_native_pdf_first_fifty_pages_only_explicit_warning_no_ocr(tmp_path, monkeypatch):
    import json
    import subprocess
    ops = VaultOps(VaultPaths(tmp_path / "synthetic-vault"))
    cards = CardStore(ops.paths.root / ".cards", ops.log)
    template = text_pdf(tmp_path / "native.pdf")
    writer = PdfWriter()
    reader = PdfReader(template)
    for _ in range(51):
        writer.add_page(reader.pages[0])
    source = ops.paths.staging / "long.pdf"
    with source.open("wb") as stream:
        writer.write(stream)
    original_popen = subprocess.Popen
    def no_ocr_child(argv, **kwargs):
        assert not any(str(part).endswith("ocr_worker.py") for part in argv), "Native text PDF unnecessarily launched OCR"
        return original_popen(argv, **kwargs)
    monkeypatch.setattr(subprocess, "Popen", no_ocr_child)
    result = StagingReader(ops, cards).read_document(source.name)
    assert result.text.count(ENGLISH) == 50
    assert "Only the first 50 pages were read." in result.warnings
    metadata = json.loads((ops.paths.root / ".text" / f"{result.source_sha256}.json").read_text())
    assert metadata["pages"] == 50 and metadata["total_pages"] == 51
    assert metadata["engine"] == "pdfium+pypdf"


def test_xfa_warning_is_returned_and_saved_in_cache(tmp_path):
    ops = VaultOps(VaultPaths(tmp_path / "synthetic-vault"))
    cards = CardStore(ops.paths.root / ".cards", ops.log)
    source = form_pdf(ops.paths.staging / "xfa.pdf", xfa_only=True)
    reader = StagingReader(ops, cards)
    first = reader.read_document(source.name)
    assert any("XFA form data was not extracted" in warning for warning in first.warnings)
    assert reader.read_document(source.name).warnings == first.warnings


@pytest.mark.parametrize("suffix,data", [(".pdf", b"not a PDF"), (".png", b"not a photo"), (".heic", b"unsupported HEIC")])
def test_failed_extraction_has_no_success_cache_or_read_receipt(tmp_path, suffix, data):
    ops = VaultOps(VaultPaths(tmp_path / "synthetic-vault"))
    cards = CardStore(ops.paths.root / ".cards", ops.log)
    source = ops.paths.staging / ("bad" + suffix)
    source.write_bytes(data)
    with pytest.raises(ReadRefused):
        StagingReader(ops, cards).read_text(source.name)
    assert not (ops.paths.root / ".text").exists()
    assert not any(row["op"] in {"agent_read", "text_extracted"} for row in ops.log.tail())
    assert source.read_bytes() == data
