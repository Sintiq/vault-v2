"""Synthetic public reader, collection and desktop worker boundaries."""
import hashlib
from pathlib import Path

import pytest

from vault_v2.cards import CardStore
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.reader import StagingReader, ReadRefused


@pytest.fixture
def env(tmp_path):
    ops = VaultOps(VaultPaths(tmp_path / "synthetic-vault"))
    cards = CardStore(ops.paths.root / ".cards", ops.log)
    return ops, cards, StagingReader(ops, cards)


def test_reader_never_lists_or_reads_hidden_ancestors_or_traversal(env):
    ops, cards, reader = env
    hidden = ops.paths.staging / ".private"
    hidden.mkdir()
    (hidden / "secret.txt").write_text("hidden synthetic", encoding="utf-8")
    (ops.paths.staging / "visible.txt").write_text("visible synthetic", encoding="utf-8")
    assert [e.rel for e in reader.list_staging()] == ["visible.txt"]
    for rel in (".private/secret.txt", "../staging/visible.txt"):
        with pytest.raises(ReadRefused):
            reader.read_text(rel)
    assert ops.log.tail() == []


def test_reader_prunes_real_junction_even_with_target_inside_staging(env):
    import os
    if os.name != "nt":
        pytest.skip("Windows junction boundary")
    import _winapi

    ops, cards, reader = env
    target = ops.paths.staging / "plain"
    target.mkdir()
    (target / "document.txt").write_text("synthetic", encoding="utf-8")
    link = ops.paths.staging / "linked"
    _winapi.CreateJunction(str(target), str(link))
    try:
        assert [entry.rel for entry in reader.list_staging()] == ["plain/document.txt"]
        with pytest.raises(ReadRefused, match="Linked"):
            reader.read_text("linked/document.txt")
        assert ops.log.tail() == []
    finally:
        link.rmdir()  # Remove this test junction only, never its target.


def test_handoff_hash_binds_actual_text_not_card_hash_cache(env):
    ops, cards, reader = env
    source = ops.paths.staging / "due.txt"
    source.write_bytes(b"old text")
    cards.hash_of(source)
    source.write_bytes(b"Payment due October 15, 2026")
    result = reader.read_document(source)
    assert result.text == "Payment due October 15, 2026"
    assert result.source_sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert result.source_size == len(source.read_bytes())
    assert ops.log.tail()[-1]["sha256"] == result.source_sha256


def test_unreadable_scans_surface_notes_in_all_proposal_flows(env):
    from vault_v2.sorting import collect_inputs, propose as sort
    from vault_v2.tasks import propose as tasks
    from vault_v2.health import propose as health

    ops, cards, reader = env
    (ops.paths.staging / "broken.pdf").write_bytes(b"not a PDF")
    for propose in (tasks, health):
        proposals, notes = propose(reader, None)
        assert not proposals
        assert any("broken.pdf" in note and "not read" in note for note in notes)
    proposals = sort(collect_inputs(ops.paths.staging, cards, reader=reader), None)
    assert proposals[0].doc.kind == "BINARY"
    assert any("not read" in flag for flag in proposals[0].flags)


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication
    application = QApplication.instance() or QApplication([])
    application.setQuitOnLastWindowClosed(False)
    return application


def test_sort_collects_document_text_off_gui_thread_and_close_discards_drafts(env, app):
    import threading
    from PySide6.QtTest import QTest
    from vault_v2.sort_dialog import SortDialog
    from vault_v2.sorting import collect_inputs

    ops, cards, reader = env
    source = ops.paths.staging / "due.txt"
    source.write_text("Payment is due October 15, 2026", encoding="utf-8")
    entered, release = threading.Event(), threading.Event()
    thread_ids = []

    def collect():
        thread_ids.append(threading.get_ident())
        entered.set()
        assert release.wait(5)
        return collect_inputs(ops.paths.staging, cards, reader=reader)

    dialog = SortDialog(cards, collect, None)
    try:
        assert entered.wait(2)
        assert thread_ids != [threading.get_ident()]
        assert "reading scans" in dialog.head.text()
        dialog.close()
    finally:
        release.set()
        for _ in range(150):
            QTest.qWait(20)
            if dialog._thread is None:
                break
        dialog.close()
    assert dialog._thread is None
    assert cards.all() == []
    assert not any(row["op"] == "card_propose" for row in ops.log.tail())


def test_change_between_snapshot_and_handoff_refuses_without_agent_read(env, monkeypatch):
    ops, cards, reader = env
    source = ops.paths.staging / "changing.txt"
    source.write_text("old synthetic text", encoding="utf-8")
    read_bytes = Path.read_bytes

    def replace_after_read(path):
        data = read_bytes(path)
        if path == source:
            source.write_text("new synthetic text", encoding="utf-8")
        return data

    monkeypatch.setattr(Path, "read_bytes", replace_after_read)
    with pytest.raises(ReadRefused, match="changed while reading"):
        reader.read_document(source)
    assert not any(row["op"] == "agent_read" for row in ops.log.tail())


def test_native_pdf_is_text_only_for_agent_and_snapshot_hash_survives_collectors(env):
    from test_local_text import text_pdf
    from vault_v2.cards import content_kind
    from vault_v2.sorting import collect_inputs
    from vault_v2.tasks import collect_docs as tasks
    from vault_v2.health import collect_docs as health

    ops, cards, reader = env
    source = text_pdf(ops.paths.staging / "scan.pdf", "Payment is due October 15, 2026. Visit at clinic.")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    assert content_kind(source) == "BINARY", "owner preview still receives original PDF bytes"
    assert reader.list_staging()[0].kind == "EXTRACTABLE"
    assert ops.log.tail() == [], "enumeration does not run extraction"
    sorted_doc = collect_inputs(ops.paths.staging, cards, reader=reader)[0]
    assert sorted_doc.kind == "TEXT" and "Payment is due" in sorted_doc.excerpt
    assert sorted_doc.sha256 == digest
    for collect in (tasks, health):
        document = collect(reader)[0]
        assert "Payment is due" in document.text and document.sha256 == digest
    for row in ops.log.tail():
        assert "Payment is due" not in str(row)


def test_sort_explicit_selection_cannot_bypass_staging_reader(env):
    from vault_v2.sorting import collect_inputs

    ops, cards, reader = env
    outside = ops.paths.personal / "private.txt"
    outside.write_text("private synthetic", encoding="utf-8")
    with pytest.raises(ReadRefused):
        collect_inputs(ops.paths.staging, cards, only=[outside], reader=reader)
    assert ops.log.tail() == []


@pytest.mark.parametrize("workflow", ["tasks", "health"])
def test_all_unreadable_document_notes_remain_accessible_in_panes(env, app, workflow):
    import time
    from vault_v2.tasks import TaskStore
    from vault_v2.tasks_pane import TasksPane
    from vault_v2.health import HealthStore
    from vault_v2.health_pane import HealthPane

    ops, cards, reader = env
    for index in range(4):
        (ops.paths.staging / f"broken-{index}.pdf").write_bytes(b"bad synthetic PDF")
    if workflow == "tasks":
        pane = TasksPane(TaskStore(ops.paths.root / ".tasks", ops.log), reader, lambda: None)
        button = pane.find_btn
    else:
        pane = HealthPane(HealthStore(ops.paths.root / ".health", ops.log), reader, lambda: None)
        button = pane.read_btn
    try:
        button.click()
        assert "reading scans" in pane.prop_label.text()
        for _ in range(150):
            app.processEvents()
            time.sleep(0.02)
            if pane._thread is None:
                break
        assert pane._thread is None
        assert "more" in pane.prop_label.text()
        assert "broken-3.pdf" in pane.prop_label.toolTip()
        assert "not read" in pane.prop_label.toolTip()
    finally:
        pane.close()
