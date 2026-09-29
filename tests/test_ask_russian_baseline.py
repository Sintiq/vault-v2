"""Russian keyword matching through the agreed Ask interfaces."""
import json
from pathlib import Path

import pytest

from vault_v2.agent import Backend, BackendInfo
from vault_v2.ask import AskDoc, ask, baseline_match, collect_docs
from vault_v2.cards import Card, CardStore
from vault_v2.errors import VaultError
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.text_cache import DocumentTextCache
from vault_v2.ocr_process import OCRResult


def test_russian_keywords_match_names_topics_and_issuers_without_case_or_yo_differences():
    docs = [
        AskDoc("name", Path("МИГРЕНЬ.pdf"), None),
        AskDoc("topic", Path("report.pdf"), Card(
            "a", "report.pdf", "BINARY", "HEALTH", ("МиГрЕнЬ",),
            "UNCONFIRMED", None, (), "HUMAN", confirmed=True)),
        AskDoc("issuer", Path("visit.pdf"), Card(
            "b", "visit.pdf", "BINARY", "HEALTH", (),
            "Клиника Ёлка", None, (), "HUMAN", confirmed=True)),
        AskDoc("other", Path("Invoice.pdf"), None),
    ]

    assert baseline_match("мигрень ЕЛКА", docs) == ["name", "topic", "issuer"]


def test_ask_matches_only_the_bounded_already_extracted_russian_excerpt(tmp_path, monkeypatch):
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    store = CardStore(ops.paths.root / ".cards", ops.log)
    source = ops.paths.staging / "report.png"
    source.write_bytes(b"synthetic scan handled by the OCR boundary fake")
    monkeypatch.setattr("vault_v2.text_extract.recognize_image_result",
                        lambda _data: OCRResult("МИГРЕНЬ " + "я" * 5992 + " НЕВРОЛОГИЯ", "Tesseract 5.5.2"))
    DocumentTextCache(ops.paths, ops.log).read_snapshot(source)
    receipts_before = ops.log.tail()

    def extraction_not_allowed(_data):
        raise AssertionError("Ask must use existing cache without new OCR")

    monkeypatch.setattr("vault_v2.text_extract.recognize_image_result", extraction_not_allowed)
    docs = collect_docs(ops.paths.staging, store)

    assert ask("мигрень", docs, None).proposed_ids == ("doc-001",)
    assert baseline_match("неврология", docs) == []
    assert len(docs[0].excerpt) == 6000
    assert docs[0].total_chars == 6011
    assert "read 6 000 of 6 011 extracted characters — the rest was not checked" in docs[0].reading_notes
    assert ops.log.tail() == receipts_before


def test_keyword_matching_does_not_claim_russian_morphology():
    docs = [AskDoc("russian", Path("Мигрень.pdf"), None),
            AskDoc("latin", Path("Migraine.pdf"), None),
            AskDoc("yo", Path("Елка.pdf"), None)]

    assert baseline_match("мигрени", docs) == []
    assert baseline_match("MIGRAINE", docs) == ["latin"]
    assert baseline_match("ЁЛКА", docs) == ["yo"]
    assert baseline_match("", docs) == []


def test_ask_without_cache_does_not_extract_or_search_plaintext_contents(tmp_path, monkeypatch):
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    store = CardStore(ops.paths.root / ".cards", ops.log)
    (ops.paths.staging / "scan.png").write_bytes(b"synthetic uncached scan")
    (ops.paths.staging / "note.txt").write_text("МИГРЕНЬ", encoding="utf-8")
    (ops.paths.staging / "Мигрень.pdf").write_bytes(b"name-only unconfirmed document")

    def extraction_not_allowed(_data):
        raise AssertionError("A cache miss must not trigger OCR")

    monkeypatch.setattr("vault_v2.text_extract.recognize_image_result", extraction_not_allowed)
    docs = collect_docs(ops.paths.staging, store)
    by_name = {doc.path.name: doc for doc in docs}

    assert baseline_match("мигрень", docs) == [by_name["Мигрень.pdf"].id]
    assert all(doc.excerpt == "" and doc.total_chars is None for doc in docs)
    assert all("confirmed card only" in " ".join(doc.reading_notes) for doc in docs)
    assert not (ops.paths.root / ".text").exists()
    assert ops.log.tail() == []


@pytest.mark.parametrize("damage", ["source-changed", "text-changed", "metadata-missing", "old-profile"])
def test_ask_never_searches_stale_or_invalid_cached_text(tmp_path, monkeypatch, damage):
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    store = CardStore(ops.paths.root / ".cards", ops.log)
    source = ops.paths.staging / "scan.png"
    source.write_bytes(b"synthetic original scan")
    monkeypatch.setattr("vault_v2.text_extract.recognize_image_result", lambda _data: OCRResult("МИГРЕНЬ", "Tesseract 5.5.2"))
    DocumentTextCache(ops.paths, ops.log).read_snapshot(source)
    cache_dir = ops.paths.root / ".text"
    txt, meta = next(cache_dir.glob("*.txt")), next(cache_dir.glob("*.json"))
    if damage == "source-changed":
        source.write_bytes(b"synthetic modified scan")
    elif damage == "text-changed":
        txt.write_text("МИГРЕНЬ и чужой текст", encoding="utf-8")
    elif damage == "metadata-missing":
        meta.unlink()
    else:
        metadata = json.loads(meta.read_text(encoding="utf-8"))
        metadata["profile"] = "obsolete synthetic profile"
        meta.write_text(json.dumps(metadata), encoding="utf-8")
    cache_before = {path.name: path.read_bytes() for path in cache_dir.iterdir()}
    receipts_before = ops.log.tail()

    def extraction_not_allowed(_data):
        raise AssertionError("Invalid cache must not trigger OCR")

    monkeypatch.setattr("vault_v2.text_extract.recognize_image_result", extraction_not_allowed)
    docs = collect_docs(ops.paths.staging, store)

    assert baseline_match("мигрень", docs) == []
    assert docs[0].excerpt == "" and docs[0].total_chars is None
    assert {path.name: path.read_bytes() for path in cache_dir.iterdir()} == cache_before
    assert ops.log.tail() == receipts_before


def test_ask_rechecks_source_after_reading_cached_text(tmp_path, monkeypatch):
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    store = CardStore(ops.paths.root / ".cards", ops.log)
    source = ops.paths.staging / "scan.png"
    source.write_bytes(b"old scan")
    monkeypatch.setattr("vault_v2.text_extract.recognize_image_result", lambda _data: OCRResult("МИГРЕНЬ", "Tesseract 5.5.2"))
    DocumentTextCache(ops.paths, ops.log).read_snapshot(source)
    cache_text = next((ops.paths.root / ".text").glob("*.txt"))
    original_open = Path.open

    def change_source_at_cache_read(path, *args, **kwargs):
        if path == cache_text and args and args[0] == "rb":
            source.write_bytes(b"new scan")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", change_source_at_cache_read)
    docs = collect_docs(ops.paths.staging, store)

    assert baseline_match("мигрень", docs) == []
    assert docs[0].excerpt == ""


def test_ask_reports_cache_unavailable_when_cache_path_is_not_a_directory(tmp_path, monkeypatch):
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    store = CardStore(ops.paths.root / ".cards", ops.log)
    (ops.paths.staging / "scan.png").write_bytes(b"synthetic scan")
    cache_path = ops.paths.root / ".text"
    cache_path.write_text("МИГРЕНЬ", encoding="utf-8")

    def extraction_not_allowed(_data):
        raise AssertionError("Suspicious cache must not trigger OCR")

    monkeypatch.setattr("vault_v2.text_extract.recognize_image_result", extraction_not_allowed)
    docs = collect_docs(ops.paths.staging, store)

    assert baseline_match("мигрень", docs) == []
    assert docs[0].excerpt == ""
    assert "confirmed card only" in " ".join(docs[0].reading_notes)
    assert cache_path.read_text(encoding="utf-8") == "МИГРЕНЬ"
    assert ops.log.tail() == []


class RecordingBackend(Backend):
    def __init__(self):
        self.info = BackendInfo("fake", "fake", "fake")
        self.seen = []

    def chat(self, system, messages, on_chunk):
        self.seen.append((system, messages))
        return '{"documents": [], "recipient": "PERSONAL", "reason": "no supported cards"}'


def test_cached_excerpt_is_local_baseline_only_and_staging_scope_stays_closed(tmp_path, monkeypatch):
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    store = CardStore(ops.paths.root / ".cards", ops.log)
    source = ops.paths.staging / "scan.png"
    source.write_bytes(b"synthetic scan")
    (ops.paths.staging / ".hidden.txt").write_text("hidden", encoding="utf-8")
    (ops.paths.documents / "МИГРЕНЬ.pdf").write_bytes(b"archived")
    (ops.paths.personal / "МИГРЕНЬ.pdf").write_bytes(b"personal")
    monkeypatch.setattr("vault_v2.text_extract.recognize_image_result", lambda _data: OCRResult("МИГРЕНЬ СЕКРЕТНАЯВЫДЕРЖКА", "Tesseract 5.5.2"))
    DocumentTextCache(ops.paths, ops.log).read_snapshot(source)
    docs = collect_docs(ops.paths.staging, store)
    backend = RecordingBackend()
    result = ask("мигрень", docs, backend)

    assert [doc.path.name for doc in docs] == ["scan.png"]
    assert result.baseline_ids == ("doc-001",)
    assert "СЕКРЕТНАЯВЫДЕРЖКА" not in json.dumps(backend.seen, ensure_ascii=False)
    assert len(backend.seen) == 1
    with pytest.raises(VaultError, match="limited to Staging"):
        collect_docs(ops.paths.documents, store)
