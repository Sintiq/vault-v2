"""Archive Ask through owner-facing Qt actions on a synthetic vault."""
from time import monotonic
import json
from pathlib import Path
import shutil
import threading

import pytest
from PySide6.QtCore import QItemSelectionModel, QThread, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from vault_v2.ask import AskDoc, collect_archive_docs, collect_docs
from vault_v2.ask_dialog import AskDialog
from vault_v2.agent_scope import AgentTextScope
from vault_v2.cards import CardStore
from vault_v2.gatekeeper import Gatekeeper
from vault_v2.export_dialog import ExportDialog
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.panes import Pane
from vault_v2.main import MainWindow
from vault_v2.text_cache import DocumentTextCache
from vault_v2.agent import Backend, BackendInfo


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    application.setQuitOnLastWindowClosed(False)
    return application


@pytest.fixture
def vault(tmp_path):
    ops = VaultOps(VaultPaths(tmp_path / "synthetic-vault"))
    return ops, CardStore(ops.paths.root / ".cards", ops.log)


def wait_for(app, condition):
    deadline = monotonic() + 5
    while not condition() and monotonic() < deadline:
        app.processEvents()
        threading.Event().wait(0.01)
    assert condition(), "the owner action did not finish"


def select(pane, path):
    for _ in range(100):
        index = pane.proxy.mapFromSource(pane.model.index(str(path)))
        if index.isValid():
            break
        QTest.qWait(10)
    assert index.isValid()
    pane.view.selectionModel().select(index, QItemSelectionModel.SelectionFlag.ClearAndSelect
                                     | QItemSelectionModel.SelectionFlag.Rows)
    pane.view.selectionModel().setCurrentIndex(index, QItemSelectionModel.SelectionFlag.NoUpdate)
    return index


def context(pane, inspect):
    errors = []

    def opened():
        menu = QApplication.activePopupWidget()
        try:
            inspect(menu)
        except BaseException as exc:
            errors.append(exc)
        finally:
            if menu is not None:
                menu.close()

    QTimer.singleShot(0, opened)
    pane.view.customContextMenuRequested.emit(pane.view.visualRect(pane.view.currentIndex()).center())
    if errors:
        raise errors[0]


def trigger(menu, text):
    action = next((a for a in menu.actions() if a.text() == text), None)
    assert action is not None, text
    assert action.isEnabled()
    action.trigger()


def test_each_propose_refreshes_documents_in_an_already_open_dialog(vault, app):
    ops, cards = vault
    first = ops.paths.staging / "immigration-first.txt"
    first.write_text("Synthetic first document", encoding="utf-8")
    dialog = AskDialog(Gatekeeper(ops), lambda: collect_docs(ops.paths.staging, cards), None)
    try:
        dialog.show()
        dialog.phrase.setText("immigration")
        QTest.mouseClick(dialog.propose_btn, Qt.MouseButton.LeftButton)
        wait_for(app, lambda: dialog.propose_btn.isEnabled() and dialog.list.count() == 1)
        assert "immigration-first.txt" in dialog.list.item(0).text()

        first.rename(ops.paths.staging / "immigration-current.txt")
        QTest.mouseClick(dialog.propose_btn, Qt.MouseButton.LeftButton)
        wait_for(app, lambda: dialog.propose_btn.isEnabled()
                 and "immigration-current.txt" in dialog.list.item(0).text())
        assert "immigration-first.txt" not in dialog.list.item(0).text()
        assert dialog.export_btn.isEnabled()
    finally:
        dialog.close()


def test_ask_preserves_baseline_ranks_rows_and_labels_model_note_as_unverified(vault, app):
    class WrongLocation(Backend):
        info = BackendInfo("ollama", "synthetic", "local test adapter")
        reply = {"documents": ["semantic"], "reason": "Document is in the personal pane."}

        def chat(self, system, messages, on_chunk):
            return json.dumps(self.reply)

    ops, _cards = vault
    docs = [AskDoc("keyword", ops.paths.staging / "migraine.txt", None),
            AskDoc("semantic", ops.paths.documents / "referral.txt", None, pane="documents")]
    backend = WrongLocation()
    dialog = AskDialog(Gatekeeper(ops), docs, backend)
    try:
        dialog.show()
        dialog.phrase.setText("migraine")
        QTest.mouseClick(dialog.propose_btn, Qt.MouseButton.LeftButton)
        wait_for(app, lambda: dialog.result is not None and dialog.propose_btn.isEnabled())
        assert [dialog.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(2)] == ["semantic", "keyword"]
        assert all(dialog.list.item(i).checkState() == Qt.CheckState.Checked for i in range(2))
        assert "omitted by agent" not in " ".join(dialog.list.item(i).text() for i in range(2))
        assert "Proposed sources: Staging (1), Documents (1)." in dialog.status.text()
        assert "personal pane" not in dialog.status.text() + dialog.status.toolTip()
        assert dialog.model_note.text() == "model's note, unverified: Document is in the personal pane."
        assert dialog.model_note.textFormat() == Qt.TextFormat.PlainText
        assert dialog.model_note.isVisible()

        # The owner may change the final set. The summary honestly names the
        # original proposal, not the current checkbox state.
        dialog.list.item(0).setCheckState(Qt.CheckState.Unchecked)
        assert dialog.export_btn.isEnabled()
        assert "Proposed sources:" in dialog.status.text()
        assert "Selected sources:" not in dialog.status.text()

        backend.reply = {"documents": []}
        QTest.mouseClick(dialog.propose_btn, Qt.MouseButton.LeftButton)
        wait_for(app, lambda: dialog.result is not None and dialog.propose_btn.isEnabled())
        assert not dialog.model_note.isVisible()
        assert dialog.model_note.text() == ""
        assert dialog.result.proposed_ids == ("keyword",)
    finally:
        dialog.close()


def test_failed_fresh_collection_never_reuses_previous_export_selection(vault, app):
    ops, cards = vault
    (ops.paths.staging / "synthetic.txt").write_text("Synthetic document", encoding="utf-8")
    dialog = AskDialog(Gatekeeper(ops), lambda: collect_archive_docs(ops.paths, cards), None)
    try:
        dialog.show()
        dialog.phrase.setText("synthetic")
        QTest.mouseClick(dialog.propose_btn, Qt.MouseButton.LeftButton)
        wait_for(app, lambda: dialog.export_btn.isEnabled())
        ops.paths.settings_file.write_text('{"agent_text_folders":123}', encoding="utf-8")
        QTest.mouseClick(dialog.propose_btn, Qt.MouseButton.LeftButton)
        wait_for(app, lambda: dialog.head.text() == "Request refused")
        assert dialog.result is None
        assert not dialog.export_btn.isEnabled()
        assert not dialog.copy_btn.isEnabled()
        assert "No previous result was reused" in dialog.status.text()
        assert all(dialog.list.item(i).checkState() == Qt.CheckState.Unchecked
                   for i in range(dialog.list.count()))
    finally:
        dialog.close()


def test_folder_context_grant_marks_folder_and_can_revoke(vault, app):
    ops, cards = vault
    folder = ops.paths.documents / "Synthetic archive"
    folder.mkdir()
    pane = Pane("documents", "Documents", ops.paths.documents, ops, cards)
    granted = []
    try:
        pane.show()
        index = select(pane, folder)
        pane.scope_granted.connect(granted.append)
        context(pane, lambda menu: trigger(menu, "Allow agent to read text here"))
        assert AgentTextScope(ops.paths, ops.log).grants() == (folder,)
        assert granted == [folder]
        index = select(pane, folder)
        assert index.data() == "Synthetic archive [A]"
        context(pane, lambda menu: trigger(menu, "Stop reading text here"))
        assert AgentTextScope(ops.paths, ops.log).grants() == ()
        assert "Agent text" not in select(pane, folder).data()
    finally:
        pane.close()
        pane.deleteLater()
        app.processEvents()


def test_inherited_permission_names_ancestor_and_read_only_menu_cannot_revoke(vault, app):
    ops, cards = vault
    parent = ops.paths.documents / "Parent"
    child = parent / "Child"
    child.mkdir(parents=True)
    pane = Pane("documents", "Documents", ops.paths.documents, ops, cards)
    try:
        pane.show()
        select(pane, parent)
        context(pane, lambda menu: trigger(menu, "Allow agent to read text here"))
        pane.set_dir(parent)
        select(pane, child)
        ancestor = str(parent.relative_to(ops.paths.root))

        def inherited(menu):
            labels = [a.text() for a in menu.actions()]
            assert f"Agent text allowed by {ancestor}" in labels
            assert "Stop reading text here" not in labels
            trigger(menu, f"Stop reading text at {ancestor}")

        context(pane, inherited)
        assert AgentTextScope(ops.paths, ops.log).grants() == ()
        context(pane, lambda menu: trigger(menu, "Allow agent to read text here"))
        pane.set_write_enabled(False)

        def disabled(menu):
            action = next(a for a in menu.actions() if a.text() == "Stop reading text here")
            assert not action.isEnabled()
            action.trigger()

        context(pane, disabled)
        assert AgentTextScope(ops.paths, ops.log).grants() == (child,)
    finally:
        pane.close()
        pane.deleteLater()
        app.processEvents()


def test_grant_warms_in_background_and_retains_window_lease(tmp_path, app, monkeypatch):
    root = tmp_path / "synthetic-vault"
    root.mkdir()
    (root / "vault.json").write_text(json.dumps({"agent_backend": "unsupported"}), encoding="utf-8")
    window = MainWindow(root, start_services=False)
    folder = window.paths.documents / "Synthetic archive"
    folder.mkdir()
    source = folder / "synthetic.txt"
    source.write_text("Synthetic permitted archive text", encoding="utf-8")
    entered, release = threading.Event(), threading.Event()
    original_open = Path.open

    def gated_open(path, mode="r", *args, **kwargs):
        if path == source and "r" in mode and threading.current_thread() is not threading.main_thread():
            entered.set()
            assert release.wait(8)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", gated_open)
    try:
        window.show()
        assert not list((root / ".text").glob("*.txt"))
        pane = window.panes["documents"]
        select(pane, folder)
        context(pane, lambda menu: trigger(menu, "Allow agent to read text here"))
        wait_for(app, entered.is_set)
        window.close()
        assert window.isVisible(), "closing must wait for the extraction worker"
        assert json.loads((root / ".vault.lock").read_text())["state"] == "active"
        release.set()
        wait_for(app, lambda: not any(t.isRunning() for t in window.findChildren(QThread)))
        snapshot = DocumentTextCache(window.paths, window.ops.log).read_cached_snapshot(source)
        assert snapshot is not None
        assert snapshot.text == "Synthetic permitted archive text"
    finally:
        release.set()
        wait_for(app, lambda: not any(t.isRunning() for t in window.findChildren(QThread)))
        window.close()
        app.processEvents()


def test_archive_selection_copies_uniquely_before_staging_only_export(vault, app):
    ops, _cards = vault
    original = ops.paths.documents / "synthetic.txt"
    original.write_text("Synthetic archive version", encoding="utf-8")
    staged = ops.paths.staging / original.name
    staged.write_text("Synthetic staging version", encoding="utf-8")
    docs = [AskDoc("staging", staged, None),
            AskDoc("archive", original, None, pane="documents", rel=original.name)]
    dialog = AskDialog(Gatekeeper(ops), docs, None)
    try:
        dialog.show()
        dialog.phrase.setText("synthetic")
        QTest.mouseClick(dialog.propose_btn, Qt.MouseButton.LeftButton)
        wait_for(app, lambda: dialog.result is not None)
        assert "documents/synthetic.txt" in dialog.list.item(1).text()
        assert not dialog.export_btn.isEnabled()
        QTest.mouseClick(dialog.copy_btn, Qt.MouseButton.LeftButton)
        assert original.read_text() == "Synthetic archive version"
        assert staged.read_text() == "Synthetic staging version"
        copy = ops.paths.staging / "synthetic (1).txt"
        assert copy.read_text() == original.read_text()
        assert dialog.export_btn.isEnabled()
        assert not dialog.copy_btn.isEnabled()
        assert "staging/synthetic (1).txt" in dialog.list.item(1).text()
        assert len([row for row in ops.log.tail() if row["op"] == "copy"]) == 1
        seen = []

        def close_export():
            export = QApplication.activeModalWidget()
            try:
                assert isinstance(export, ExportDialog)
                seen.extend(export.sources)
            finally:
                export.reject()

        QTimer.singleShot(0, close_export)
        QTest.mouseClick(dialog.export_btn, Qt.MouseButton.LeftButton)
        assert set(seen) == {staged, copy}
        assert not any(row["op"] == "export_done" for row in ops.log.tail())
    finally:
        dialog.close()


def test_revoke_while_ask_is_open_removes_text_from_next_proposal(tmp_path, app):
    root = tmp_path / "synthetic-vault"
    root.mkdir()
    (root / "vault.json").write_text(json.dumps({"agent_backend": "unsupported"}), encoding="utf-8")
    window = MainWindow(root, start_services=False)
    folder = window.paths.documents / "Archive"
    folder.mkdir()
    (folder / "neutral.txt").write_text("uniquesyntheticneedle", encoding="utf-8")
    dialog = AskDialog(window.gate, lambda: collect_archive_docs(window.paths, window.cards), None, window)
    try:
        window.show()
        pane = window.panes["documents"]
        select(pane, folder)
        context(pane, lambda menu: trigger(menu, "Allow agent to read text here"))
        wait_for(app, lambda: not any(t.isRunning() for t in window.findChildren(QThread)))
        dialog.show()
        dialog.phrase.setText("uniquesyntheticneedle")
        QTest.mouseClick(dialog.propose_btn, Qt.MouseButton.LeftButton)
        wait_for(app, lambda: dialog.copy_btn.isEnabled())
        assert dialog.docs[0].excerpt == "uniquesyntheticneedle"
        wait_for(app, lambda: pane.write_enabled)
        select(pane, folder)
        context(pane, lambda menu: trigger(menu, "Stop reading text here"))
        QTest.mouseClick(dialog.propose_btn, Qt.MouseButton.LeftButton)
        wait_for(app, lambda: dialog.result is not None and dialog.propose_btn.isEnabled())
        assert dialog.docs[0].excerpt == ""
        assert dialog.list.item(0).checkState() == Qt.CheckState.Unchecked
        assert not dialog.copy_btn.isEnabled()
        assert not dialog.export_btn.isEnabled()
    finally:
        dialog.close()
        wait_for(app, lambda: not any(t.isRunning() for t in window.findChildren(QThread)))
        window.close()
        app.processEvents()


def test_partial_copy_reports_success_and_failure_without_exporting_archive(vault, app, monkeypatch):
    ops, _cards = vault
    first = ops.paths.documents / "synthetic-first.txt"
    second = ops.paths.personal / "synthetic-second.txt"
    first.write_text("Synthetic first", encoding="utf-8")
    second.write_text("Synthetic second", encoding="utf-8")
    original_copy = shutil.copy2

    def fail_second(source, destination, *args, **kwargs):
        if Path(source) == second:
            raise PermissionError("Synthetic unreadable source")
        return original_copy(source, destination, *args, **kwargs)

    monkeypatch.setattr(shutil, "copy2", fail_second)
    docs = [AskDoc("first", first, None, pane="documents", rel=first.name),
            AskDoc("second", second, None, pane="personal", rel=second.name)]
    dialog = AskDialog(Gatekeeper(ops), docs, None)
    try:
        dialog.show()
        dialog.phrase.setText("synthetic")
        QTest.mouseClick(dialog.propose_btn, Qt.MouseButton.LeftButton)
        wait_for(app, lambda: dialog.copy_btn.isEnabled())
        QTest.mouseClick(dialog.copy_btn, Qt.MouseButton.LeftButton)
        assert (ops.paths.staging / first.name).read_text() == "Synthetic first"
        assert first.exists() and second.exists()
        assert "Copied to Staging: 1; failed: 1" in dialog.status.text()
        assert "operation may have applied; writes blocked until restart" in dialog.status.toolTip()
        assert "staging/synthetic-first.txt" in dialog.list.item(0).text()
        assert "personal/synthetic-second.txt" in dialog.list.item(1).text()
        assert not dialog.export_btn.isEnabled()
        assert not dialog.copy_btn.isEnabled()
        assert len([row for row in ops.log.tail() if row["op"] == "copy"]) == 1
    finally:
        dialog.close()


def test_startup_warms_saved_marked_folders_without_blocking_window(tmp_path, app, monkeypatch):
    ops = VaultOps(VaultPaths(tmp_path / "synthetic-startup"))
    ops.paths.settings_file.write_text(json.dumps({"agent_backend": "unsupported"}), encoding="utf-8")
    folder = ops.paths.personal / "Previously marked"
    folder.mkdir()
    source = folder / "new-synthetic.txt"
    source.write_text("Synthetic startup text", encoding="utf-8")
    AgentTextScope(ops.paths, ops.log).set_folder(folder, True)
    entered, release = threading.Event(), threading.Event()
    original_open = Path.open

    def gated_open(path, mode="r", *args, **kwargs):
        if path == source and "r" in mode and threading.current_thread() is not threading.main_thread():
            entered.set()
            assert release.wait(8)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", gated_open)
    window = MainWindow(ops.paths.root, start_services=False)
    try:
        window.show()
        wait_for(app, entered.is_set)
        assert window.isVisible()
        assert window.text_queue.busy
        release.set()
        wait_for(app, lambda: not window.text_queue.busy
                 and not any(t.isRunning() for t in window.findChildren(QThread)))
        snapshot = DocumentTextCache(window.paths, window.ops.log).read_cached_snapshot(source)
        assert snapshot is not None and snapshot.text == "Synthetic startup text"
    finally:
        release.set()
        wait_for(app, lambda: not any(t.isRunning() for t in window.findChildren(QThread)))
        window.close()
        app.processEvents()


def test_closed_ask_keeps_window_lease_for_its_background_queue(tmp_path, app, monkeypatch):
    root = tmp_path / "synthetic-queued-ask"
    root.mkdir()
    (root / "vault.json").write_text(json.dumps({"agent_backend": "unsupported"}), encoding="utf-8")
    window = MainWindow(root, start_services=False)
    folder = window.paths.personal / "Marked"
    folder.mkdir()
    entered, release = threading.Event(), threading.Event()
    dialog = None
    try:
        window.show()
        pane = window.panes["personal"]
        select(pane, folder)
        context(pane, lambda menu: trigger(menu, "Allow agent to read text here"))
        wait_for(app, lambda: not window.text_queue.busy
                 and not any(t.isRunning() for t in window.findChildren(QThread)))
        source = folder / "new-neutral.txt"
        source.write_text("uniquenewneedle", encoding="utf-8")
        original_open = Path.open

        class GatedContentRead:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                self.stream.__enter__()
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def read(self, size=-1):
                # SHA reads stay available; hold only the full bounded content
                # read at the filesystem seam, without replacing our modules.
                if size > 1024 * 1024:
                    entered.set()
                    assert release.wait(8)
                return self.stream.read(size)

        def gated_open(path, mode="r", *args, **kwargs):
            stream = original_open(path, mode, *args, **kwargs)
            return GatedContentRead(stream) if path == source and mode == "rb" else stream

        monkeypatch.setattr(Path, "open", gated_open)
        dialog = AskDialog(window.gate, lambda: collect_archive_docs(window.paths, window.cards), None, window)
        dialog.show()
        dialog.phrase.setText("uniquenewneedle")
        QTest.mouseClick(dialog.propose_btn, Qt.MouseButton.LeftButton)
        wait_for(app, lambda: dialog.result is not None and entered.is_set())
        assert any("still being read" in note for note in dialog.result.notes)
        assert dialog.result.proposed_ids == ()
        assert not any(t.isRunning() for t in window.findChildren(QThread))
        assert window.text_queue.busy
        dialog.close()
        window.close()
        assert window.isVisible()
        assert json.loads((root / ".vault.lock").read_text())["state"] == "active"
        release.set()
        wait_for(app, lambda: not window.text_queue.busy)
        assert DocumentTextCache(window.paths, window.ops.log).read_cached_snapshot(source).text == "uniquenewneedle"
    finally:
        release.set()
        if dialog is not None:
            dialog.close()
        wait_for(app, lambda: not window.text_queue.busy
                 and not any(t.isRunning() for t in window.findChildren(QThread)))
        window.close()
        app.processEvents()
