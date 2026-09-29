"""Synthetic reading coverage through the public reader and receipt seams."""

import pytest

from vault_v2.cards import CardStore
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.reader import StagingReader


@pytest.fixture
def env(tmp_path):
    ops = VaultOps(VaultPaths(tmp_path / "synthetic-vault"))
    cards = CardStore(ops.paths.root / ".cards", ops.log)
    return ops, StagingReader(ops, cards, purpose="coverage-test")


def test_long_text_reports_exact_unchecked_remainder_and_receipt(env):
    ops, reader = env
    (ops.paths.staging / "long.txt").write_text("A" * 48210, encoding="utf-8")

    result = reader.read_document("long.txt")

    assert result.text == "A" * 6000
    assert result.total_chars == 48210
    assert result.warnings == (
        "read 6 000 of 48 210 characters — the rest was not checked",
    )
    receipt = ops.log.tail(1)[0]
    assert receipt["op"] == "agent_read"
    assert receipt["extra"] == {
        "chars": 6000, "purpose": "coverage-test", "read_chars": 6000,
        "total_chars": 48210, "truncated": True, "character_basis": "source",
    }


def test_pdf_counts_only_available_extracted_text_and_keeps_page_warning(env, tmp_path):
    from pypdf import PdfReader, PdfWriter
    from tools.synthetic_ocr_files import text_pdf

    ops, reader = env
    template = text_pdf(tmp_path / "page.pdf", "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    page = PdfReader(template).pages[0]
    writer = PdfWriter()
    for _ in range(51):
        writer.add_page(page)
    source = ops.paths.staging / "partial.pdf"
    with source.open("wb") as stream:
        writer.write(stream)

    result = reader.read_document(source.name, limit=10)

    assert result.text == "ABCDEFGHIJ"
    # Fifty 26-character alphabets plus 49 two-character page separators.
    assert result.total_chars == 1398
    assert result.warnings == (
        "Only the first 50 pages were read.",
        "read 10 of 1 398 extracted characters — the rest was not checked",
    )
    assert ops.log.tail(1)[0]["extra"] == {
        "chars": 10, "purpose": "coverage-test", "read_chars": 10,
        "total_chars": 1398, "truncated": True, "character_basis": "extracted",
    }


@pytest.mark.parametrize("method", ["read_document", "read_text"])
def test_custom_limit_counts_unicode_characters_not_source_bytes(env, method):
    ops, reader = env
    (ops.paths.staging / "unicode.txt").write_text("Я🙂e\u0301" * 1000, encoding="utf-8")

    result = getattr(reader, method)("unicode.txt", limit=3)

    if method == "read_document":
        assert result.text == "Я🙂e"
        assert result.total_chars == 4000
        assert result.warnings == (
            "read 3 of 4 000 characters — the rest was not checked",
        )
    else:
        assert result == "Я🙂e"
    assert ops.log.tail(1)[0]["extra"] == {
        "chars": 3, "purpose": "coverage-test", "read_chars": 3,
        "total_chars": 4000, "truncated": True, "character_basis": "source",
    }


@pytest.mark.parametrize("text,total", [("", 0), ("🙂éЯ", 3), ("A" * 6000, 6000)])
def test_complete_text_has_exact_total_without_unchecked_remainder(env, text, total):
    ops, reader = env
    (ops.paths.staging / "complete.txt").write_text(text, encoding="utf-8")

    result = reader.read_document("complete.txt")

    assert result.text == text
    assert result.total_chars == total
    assert result.warnings == ()
    receipt = ops.log.tail(1)[0]["extra"]
    assert receipt["read_chars"] == receipt["total_chars"] == total
    assert receipt["truncated"] is False


def test_photo_coverage_counts_available_ocr_text_without_claiming_source_total(env, monkeypatch):
    from PIL import Image
    from vault_v2 import text_extract

    ops, reader = env
    source = ops.paths.staging / "photo.png"
    with Image.new("RGB", (4, 4), "white") as image:
        image.save(source)
    # Only the external OCR process result is substituted; cache, source
    # checks, excerpt preparation and receipts use their real public paths.
    from vault_v2.ocr_process import OCRResult
    monkeypatch.setattr(text_extract, "recognize_image_result",
                        lambda _data: OCRResult("Я" * 48210, "Tesseract 5.5.2"))

    result = reader.read_document(source.name)

    assert result.text == "Я" * 6000
    assert result.total_chars == 48210
    assert result.warnings == (
        "read 6 000 of 48 210 extracted characters — the rest was not checked",
    )
    assert ops.log.tail(1)[0]["extra"] == {
        "chars": 6000, "purpose": "coverage-test", "read_chars": 6000,
        "total_chars": 48210, "truncated": True, "character_basis": "extracted",
    }
