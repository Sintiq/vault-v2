"""Owner-visible Sort approval must not replay over a later owner decision."""
import time

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from vault_v2.cards import CardStore
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.sort_dialog import SortDialog
from vault_v2.sorting import collect_inputs


@pytest.fixture
def app():
    application = QApplication.instance() or QApplication([])
    application.setQuitOnLastWindowClosed(False)
    return application


@pytest.fixture
def env(tmp_path, app, monkeypatch):
    ops = VaultOps(VaultPaths(tmp_path / "synthetic-vault"))
    cards = CardStore(ops.paths.root / ".cards", ops.log)
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: warnings.append(text))
    monkeypatch.setattr(QMessageBox, "information", lambda parent, title, text: warnings.append(text))
    dialogs = []

    def open_sort(count=1):
        for index in range(count):
            (ops.paths.staging / f"invoice-{index}.txt").write_text(
                f"Synthetic invoice {index}. Payment due October 15, 2026.", encoding="utf-8")
        dialog = SortDialog(cards, collect_inputs(ops.paths.staging, cards), None)
        dialogs.append(dialog)
        deadline = time.monotonic() + 5
        while not dialog.confirm_all.isEnabled() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert dialog.confirm_all.isEnabled()
        return dialog

    yield ops, cards, warnings, open_sort
    for dialog in dialogs:
        dialog.close()
    app.processEvents()


def test_later_owner_shelf_edit_rejects_stale_desktop_confirm_without_writes(env):
    ops, cards, warnings, open_sort = env
    dialog = open_sort()
    source = ops.paths.staging / "invoice-0.txt"
    before_source = source.read_bytes()
    cards.set_shelf(source, "HEALTH")
    before_receipts = ops.log.tail()
    generation = cards.generation
    dialog.confirm_all.click()
    assert warnings == ["proposals changed — refresh the list"]
    assert cards.for_path(source).shelf == "HEALTH"
    assert cards.generation == generation and ops.log.tail() == before_receipts
    assert source.read_bytes() == before_source


def test_confirmed_row_is_one_use_even_while_another_row_remains(env):
    ops, cards, warnings, open_sort = env
    dialog = open_sort(2)
    dialog.table.selectRow(0)
    dialog.confirm_sel.click()
    before = ops.log.tail()
    generation = cards.generation
    dialog.confirm_sel.click()
    assert warnings == ["proposals changed — refresh the list"]
    assert cards.generation == generation and ops.log.tail() == before
    assert dialog.confirmed == 1


def test_stale_generation_is_checked_before_empty_selection(env):
    ops, cards, warnings, open_sort = env
    dialog = open_sort()
    cards.set_shelf(ops.paths.staging / "invoice-0.txt", "HEALTH")
    before = ops.log.tail()
    dialog.table.clearSelection()
    dialog.confirm_sel.click()
    assert warnings == ["proposals changed — refresh the list"]
    assert ops.log.tail() == before


def test_owner_can_edit_confirm_selected_then_confirm_remaining_once(env):
    ops, cards, warnings, open_sort = env
    dialog = open_sort(2)
    first = ops.paths.staging / "invoice-0.txt"
    second = ops.paths.staging / "invoice-1.txt"
    dialog.table.cellWidget(0, 1).setCurrentText("HEALTH")
    dialog.table.item(0, 2).setText("synthetic-owner-topic")
    dialog.table.selectRow(0)
    dialog.confirm_sel.click()
    assert cards.for_path(first).shelf == "HEALTH"
    assert cards.for_path(first).topics == ("synthetic-owner-topic",)
    assert cards.for_path(first).origin == "HUMAN"
    assert not cards.for_path(second).confirmed
    dialog.confirm_all.click()
    assert cards.for_path(first).shelf == "HEALTH", "Confirm all cannot replay the first row"
    assert cards.for_path(second).shelf == "FINANCE" and cards.for_path(second).confirmed
    assert len([row for row in ops.log.tail() if row["op"] == "card_confirm"]) == 2
    assert warnings == [] and dialog.confirmed == 2
    before = ops.log.tail()
    dialog.confirm_all.click()
    assert ops.log.tail() == before
    assert not dialog.confirm_all.isEnabled() and not dialog.confirm_sel.isEnabled()


def test_mixed_selection_of_consumed_and_new_row_refuses_whole_click(env):
    ops, cards, warnings, open_sort = env
    dialog = open_sort(2)
    dialog.table.selectRow(0)
    dialog.confirm_sel.click()
    before = ops.log.tail()
    generation = cards.generation
    dialog.table.selectAll()
    dialog.confirm_sel.click()
    assert warnings == ["proposals changed — refresh the list"]
    assert ops.log.tail() == before and cards.generation == generation
    assert not cards.for_path(ops.paths.staging / "invoice-1.txt").confirmed


def test_phone_confirmation_invalidates_an_already_shown_desktop_proposal(env):
    from vault_v2.agent_api import AgentAPI
    from vault_v2.health import HealthStore
    from vault_v2.reader import StagingReader
    from vault_v2.tasks import TaskStore

    ops, cards, warnings, open_sort = env
    dialog = open_sort()
    phone = AgentAPI(ops.paths.staging, StagingReader(ops, cards), cards,
                     TaskStore(ops.paths.root / ".tasks", ops.log),
                     HealthStore(ops.paths.root / ".health", ops.log), lambda: None)
    shown = phone.sort_propose()["proposals"][0]
    assert phone.sort_confirm([{"id": shown["id"], "shelf": "HEALTH"}])["confirmed"] == 1
    before = ops.log.tail()
    generation = cards.generation
    dialog.confirm_all.click()
    assert warnings == ["proposals changed — refresh the list"]
    assert cards.for_path(ops.paths.staging / "invoice-0.txt").shelf == "HEALTH"
    assert cards.generation == generation and ops.log.tail() == before


def test_second_desktop_sort_publication_invalidates_the_first_dialog(env):
    ops, cards, warnings, open_sort = env
    old_dialog = open_sort()
    new_dialog = open_sort()
    new_dialog.table.cellWidget(0, 1).setCurrentText("IDENTITY")
    new_dialog.confirm_all.click()
    before = ops.log.tail()
    old_dialog.confirm_all.click()
    assert warnings == ["proposals changed — refresh the list"]
    assert cards.for_path(ops.paths.staging / "invoice-0.txt").shelf == "IDENTITY"
    assert ops.log.tail() == before


def test_stale_generation_is_checked_before_invalid_edit_validation(env):
    ops, cards, warnings, open_sort = env
    dialog = open_sort()
    cards.set_shelf(ops.paths.staging / "invoice-0.txt", "HEALTH")
    dialog.table.item(0, 4).setText("invalid year")
    before = ops.log.tail()
    dialog.confirm_all.click()
    assert warnings == ["proposals changed — refresh the list"]
    assert ops.log.tail() == before
