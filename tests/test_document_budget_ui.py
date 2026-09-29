"""Typed whole-request refusal through the public desktop widget boundaries."""

from html import escape
from time import monotonic

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from vault_v2.agent import Backend, BackendInfo
from vault_v2.cards import CardStore
from vault_v2.document_budget import DocumentBudgetExceeded
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.reader import StagingReader


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    application.setQuitOnLastWindowClosed(False)
    return application


@pytest.fixture
def env(tmp_path):
    ops = VaultOps(VaultPaths(tmp_path / "synthetic-vault"))
    cards = CardStore(ops.paths.root / ".cards", ops.log)
    (ops.paths.staging / "clinical.txt").write_text(
        "Schedule a follow-up visit before 2026-10-15.", encoding="utf-8")
    return ops, cards, StagingReader(ops, cards)


class RefusingBackend(Backend):
    def __init__(self):
        self.info = BackendInfo("fake", "fake", "fake")
        self.calls = 0
        self.refusal = DocumentBudgetExceeded(
            "Request too large: 9 000 units exceed the 8 192 unit limit.",
            documents=("clinical.txt", "<b>summary.txt</b>"),
            reading_notes=("read 6 000 of 48 210 characters — the rest was not checked",),
        )

    def chat(self, system, messages, on_chunk):
        self.calls += 1
        raise self.refusal


def wait_for(app, condition):
    deadline = monotonic() + 3
    while not condition() and monotonic() < deadline:
        app.processEvents()
        QTest.qWait(10)
    assert condition(), "desktop operation did not reach its expected visible state"


def test_sort_refusal_never_publishes_baseline_drafts_or_enables_confirmation(env, app):
    from vault_v2.sort_dialog import SortDialog
    from vault_v2.sorting import collect_inputs

    ops, cards, reader = env
    backend = RefusingBackend()
    dialog = SortDialog(cards, collect_inputs(ops.paths.staging, cards, reader=reader), backend)
    try:
        wait_for(app, lambda: "Request too large" in dialog.sub.text())
        assert dialog.head.text() == "Request refused"
        assert dialog.sub.text() == str(backend.refusal)
        assert dialog.sub.textFormat() == Qt.TextFormat.PlainText
        assert escape("<b>summary.txt</b>") in dialog.sub.toolTip()
        assert dialog.table.rowCount() == 0
        assert not dialog.confirm_all.isEnabled() and not dialog.confirm_sel.isEnabled()
        assert cards.all() == []
        assert not any(row["op"] == "card_propose" for row in ops.log.tail())
        assert backend.calls == 1
    finally:
        dialog.close()


def test_ask_refusal_clears_previous_selection_and_does_not_offer_baseline_export(env, app):
    from vault_v2.ask import collect_docs
    from vault_v2.ask_dialog import AskDialog
    from vault_v2.gatekeeper import Gatekeeper

    ops, cards, _reader = env
    backend = RefusingBackend()
    dialog = AskDialog(Gatekeeper(ops), collect_docs(ops.paths.staging, cards), None)
    try:
        dialog.phrase.setText("clinical")
        dialog.propose_btn.click()
        wait_for(app, lambda: dialog.export_btn.isEnabled())
        assert dialog.list.item(0).checkState() == Qt.CheckState.Checked
        receipts_before = ops.log.tail()

        dialog.backend = backend
        dialog.propose_btn.click()
        wait_for(app, lambda: "Request too large" in dialog.status.text())

        assert dialog.result is None
        assert dialog.status.text() == str(backend.refusal)
        assert dialog.status.textFormat() == Qt.TextFormat.PlainText
        assert escape("<b>summary.txt</b>") in dialog.status.toolTip()
        assert not dialog.export_btn.isEnabled()
        assert not dialog.list.isEnabled()
        assert all(dialog.list.item(i).checkState() == Qt.CheckState.Unchecked
                   for i in range(dialog.list.count()))
        assert "showing baseline" not in dialog.status.text()
        assert ops.log.tail() == receipts_before
        assert backend.calls == 1

        dialog.backend = None
        dialog.propose_btn.click()
        wait_for(app, lambda: dialog.export_btn.isEnabled())
        assert dialog.list.isEnabled()
        assert dialog.result is not None
    finally:
        dialog.close()


@pytest.mark.parametrize("workflow", ["tasks", "health"])
def test_pane_refusal_clears_previous_proposals_but_preserves_saved_items(env, app, workflow):
    from vault_v2.tasks import Task, TaskStore
    from vault_v2.tasks_pane import TasksPane
    from vault_v2.health import Entry, HealthStore
    from vault_v2.health_pane import HealthPane

    ops, _cards, reader = env
    selected_backend = [None]
    if workflow == "tasks":
        store = TaskStore(ops.paths.root / ".tasks", ops.log)
        store.add(Task("saved", "f" * 64, "earlier.txt", "Previously saved", None,
                       "Saved synthetic quote", "HUMAN"))
        pane = TasksPane(store, reader, lambda: selected_backend[0])
        button = pane.find_btn
    else:
        store = HealthStore(ops.paths.root / ".health", ops.log)
        store.add(Entry("saved", "f" * 64, "earlier.txt", None, "VISIT", "Previously saved",
                        "Saved synthetic quote", "HUMAN"))
        pane = HealthPane(store, reader, lambda: selected_backend[0])
        button = pane.read_btn
    saved_before = store.all()
    try:
        button.click()
        wait_for(app, lambda: pane.prop_list.count() == 1)
        backend = RefusingBackend()
        selected_backend[0] = backend
        button.click()
        wait_for(app, lambda: "Request too large" in pane.prop_label.text())

        assert pane.prop_list.count() == 0
        assert pane.prop_list.isHidden() and pane.add_btn.isHidden()
        assert not pane.add_btn.isEnabled()
        assert pane.prop_label.text() == str(backend.refusal)
        assert pane.prop_label.textFormat() == Qt.TextFormat.PlainText
        assert escape("<b>summary.txt</b>") in pane.prop_label.toolTip()
        assert store.all() == saved_before
        assert backend.calls == 1

        selected_backend[0] = None
        button.click()
        wait_for(app, lambda: pane.prop_list.count() == 1 and pane.add_btn.isEnabled())
        assert store.all() == saved_before
    finally:
        pane.close()


def test_successful_ask_shows_reading_coverage_and_all_notes_in_safe_tooltip(env, app):
    from dataclasses import replace
    from vault_v2.ask import collect_docs
    from vault_v2.ask_dialog import AskDialog
    from vault_v2.gatekeeper import Gatekeeper

    ops, cards, _reader = env
    coverage = "read 6 000 of 48 210 extracted characters — the rest was not checked"
    docs = [replace(collect_docs(ops.paths.staging, cards)[0], reading_notes=(
        coverage, "second extraction note", "third extraction note", "<b>last extraction note</b>",
    ))]
    dialog = AskDialog(Gatekeeper(ops), docs, None)
    try:
        dialog.phrase.setText("clinical")
        dialog.propose_btn.click()
        wait_for(app, lambda: dialog.export_btn.isEnabled())

        assert coverage in dialog.status.text()
        assert "3 more notes" in dialog.status.text()
        assert "Ask model input: cards only" in dialog.status.text()
        assert escape("<b>last extraction note</b>") in dialog.status.toolTip()
        assert "local text excerpts come only from Staging and permitted folders" in dialog.sub.text()
        assert dialog.status.textFormat() == Qt.TextFormat.PlainText
    finally:
        dialog.close()


@pytest.mark.parametrize("workflow", ["sort", "ask"])
def test_closed_dialog_discards_late_refusal_without_publishing_results(env, app, workflow):
    from threading import Event
    from PySide6.QtCore import QThread
    from vault_v2.ask import collect_docs
    from vault_v2.ask_dialog import AskDialog
    from vault_v2.gatekeeper import Gatekeeper
    from vault_v2.sort_dialog import SortDialog
    from vault_v2.sorting import collect_inputs

    class WaitingBackend(RefusingBackend):
        def __init__(self):
            super().__init__()
            self.entered, self.release = Event(), Event()

        def chat(self, system, messages, on_chunk):
            self.entered.set()
            assert self.release.wait(3)
            return super().chat(system, messages, on_chunk)

    ops, cards, reader = env
    backend = WaitingBackend()
    if workflow == "sort":
        dialog = SortDialog(cards, collect_inputs(ops.paths.staging, cards, reader=reader), backend)
    else:
        dialog = AskDialog(Gatekeeper(ops), collect_docs(ops.paths.staging, cards), backend)
        dialog.phrase.setText("clinical")
        dialog.propose_btn.click()
    try:
        assert backend.entered.wait(1)
        dialog.close()
        closed_caption = dialog.head.text()
    finally:
        backend.release.set()
        wait_for(app, lambda: all(not thread.isRunning() for thread in dialog.findChildren(QThread)))
        dialog.close()

    assert dialog.head.text() == closed_caption
    assert cards.all() == []
    assert backend.calls == 1


def test_incomplete_model_output_is_refused_without_claiming_pre_model_rejection(env, app):
    from vault_v2.document_budget import DocumentOutputIncomplete
    from vault_v2.sort_dialog import SortDialog
    from vault_v2.sorting import collect_inputs

    ops, cards, reader = env
    backend = RefusingBackend()
    backend.refusal = DocumentOutputIncomplete(
        "Model response incomplete: output limit reached; no proposals were published.",
        documents=("clinical.txt",),
    )
    dialog = SortDialog(cards, collect_inputs(ops.paths.staging, cards, reader=reader), backend)
    try:
        wait_for(app, lambda: "output limit reached" in dialog.sub.text())
        assert dialog.head.text() == "Request refused"
        assert dialog.sub.text() == str(backend.refusal)
        assert dialog.table.rowCount() == 0
        assert not dialog.confirm_all.isEnabled() and not dialog.confirm_sel.isEnabled()
        assert cards.all() == []
        assert backend.calls == 1
    finally:
        dialog.close()
