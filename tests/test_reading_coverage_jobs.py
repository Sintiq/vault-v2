"""Completeness follows real document jobs to the owner-visible phone notes."""

import pytest

from vault_v2.agent_api import AgentAPI
from vault_v2.cards import CardStore
from vault_v2.health import HealthStore
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.reader import StagingReader
from vault_v2.tasks import TaskStore


@pytest.fixture
def jobs(tmp_path):
    ops = VaultOps(VaultPaths(tmp_path / "synthetic-vault"))
    cards = CardStore(ops.paths.root / ".cards", ops.log)
    reader = StagingReader(ops, cards)
    tasks = TaskStore(ops.paths.root / ".tasks", ops.log)
    health = HealthStore(ops.paths.root / ".health", ops.log)
    (ops.paths.staging / "long.txt").write_text("a" * 48210, encoding="utf-8")
    return AgentAPI(ops.paths.staging, reader, cards, tasks, health, lambda: None)


@pytest.mark.parametrize("method", ["sort_propose", "tasks_propose", "health_propose"])
def test_phone_jobs_report_exact_read_coverage_in_top_level_notes(jobs, method):
    result = getattr(jobs, method)()
    assert any("long.txt" in note and
               "read 6 000 of 48 210 characters — the rest was not checked" in note
               for note in result["notes"])


def test_desktop_sort_caption_reports_exact_coverage(jobs):
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from vault_v2.sort_dialog import SortDialog
    from vault_v2.sorting import collect_inputs

    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    dialog = SortDialog(jobs.cards,
                        lambda: collect_inputs(jobs.staging, jobs.cards, reader=jobs.reader), None)
    try:
        for _ in range(150):
            QTest.qWait(20)
            if dialog._thread is None:
                break
        assert dialog._thread is None
        assert "read 6 000 of 48 210 characters — the rest was not checked" in dialog.sub.text()
    finally:
        dialog.close()


def test_phone_ask_notes_limit_model_excerpts_to_owner_scope(jobs, monkeypatch):
    from vault_v2.text_cache import DocumentTextCache
    from vault_v2.ocr_process import OCRResult

    source = jobs.staging / "мигрень.png"
    source.write_bytes(b"synthetic OCR input")
    monkeypatch.setattr("vault_v2.text_extract.recognize_image_result", lambda _data: OCRResult("а" * 48210, "Tesseract 5.5.2"))
    DocumentTextCache(jobs.reader.ops.paths, jobs.cards.log).read_snapshot(source)
    result = jobs.ask("МИГРЕНЬ")
    assert any(row["name"] == "мигрень.png" for row in result["matches"])
    assert any("мигрень.png" in note and
               "read 6 000 of 48 210 extracted characters — the rest was not checked" in note
               for note in result["notes"])
    assert "Ask model input: cards everywhere; cached excerpts only in Staging and owner-marked folders." in result["notes"]


def test_public_sort_collection_without_reader_keeps_same_coverage(jobs):
    from vault_v2.sorting import collect_inputs, propose

    proposals = propose(collect_inputs(jobs.staging, jobs.cards), None)
    assert any("read 6 000 of 48 210 characters — the rest was not checked" in flag
               for flag in proposals[0].flags)
