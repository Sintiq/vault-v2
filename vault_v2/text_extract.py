"""Local document text from immutable source bytes; no vault writes or models."""
from dataclasses import dataclass
from importlib.metadata import version

from .errors import VaultError
from .pdf_preview import render_pdf_bytes_page, read_pdf_bytes_text_page
from .ocr_process import ENGINE_VERSIONS, recognize_image_result

PDF_VERSION = f"pypdfium2 {version('pypdfium2')}; pypdf {version('pypdf')}"
EXTRACTION_ENGINES = {
    "utf8-replacement": frozenset({("1", ())}),
    "pdfium+pypdf": frozenset({(PDF_VERSION, ())}),
    "pdfium+pypdf+tesseract": frozenset((PDF_VERSION + "; " + value, ("eng", "rus"))
                                       for value in ENGINE_VERSIONS.values()),
    "tesseract": frozenset((value, ("eng", "rus")) for value in ENGINE_VERSIONS.values()),
}
EXTRACTION_PROFILE = f"vault-text-v2-{PDF_VERSION}-tesseract5.5.2-or-5.5.3-child-version-eng+rus-fast87416418-native20-pages50"
EXTRACTABLE_SUFFIXES = frozenset({".pdf", ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp", ".heic", ".heif"})
PAGE_LIMIT = 50
PAGE_TEXT_LIMIT = 100000


@dataclass(frozen=True)
class ExtractedText:
    text: str
    engine: str
    version: str
    languages: tuple[str, ...]
    pages: int
    total_pages: int
    warnings: tuple[str, ...] = ()


def extract_text(data: bytes, suffix: str) -> ExtractedText:
    if suffix.lower() not in EXTRACTABLE_SUFFIXES:
        raise VaultError("This document type has no local text extraction.")
    if suffix.lower() != ".pdf":
        result = recognize_image_result(data)
        return ExtractedText(result.text, "tesseract", result.version, ("eng", "rus"), 1, 1)
    texts, warnings = [], []
    ocr_version = None
    count = None
    for index in range(PAGE_LIMIT):
        actual_count, native, fields, page_warnings = read_pdf_bytes_text_page(data, index)
        if count is not None and actual_count != count:
            raise VaultError("PDF text not read: inconsistent child page count.")
        count = actual_count
        for warning in page_warnings:
            if warning not in warnings:
                warnings.append(warning)
        if sum(character.isalnum() for character in native) < 20:
            # PDF admission was released; OCR owns a separate bounded child.
            raster = render_pdf_bytes_page(data, index).png
            recognized = recognize_image_result(raster)
            if ocr_version is not None and recognized.version != ocr_version:
                raise VaultError("PDF text not read: OCR engine changed between pages.")
            ocr_version = recognized.version
            native = "\n".join(part for part in (native, recognized.text) if part)
        text = "\n".join(part for part in (native, fields) if part)
        if len(text) > PAGE_TEXT_LIMIT:
            raise VaultError("PDF page text exceeds the local extraction limit.")
        texts.append(text)
        if index + 1 >= count:
            break
    if count > PAGE_LIMIT:
        warnings.append("Only the first 50 pages were read.")
    engine = "pdfium+pypdf+tesseract" if ocr_version else "pdfium+pypdf"
    engine_version = PDF_VERSION + "; " + ocr_version if ocr_version else PDF_VERSION
    languages = ("eng", "rus") if ocr_version else ()
    return ExtractedText("\n\n".join(texts), engine, engine_version, languages,
                         min(count, PAGE_LIMIT), count, tuple(warnings))
