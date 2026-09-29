"""Real Qt date controls, backed only by synthetic task/health stores."""
from __future__ import annotations

import json

import pytest
from PySide6.QtCore import QDate, QTimer, Qt
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox

from vault_v2.date_dialog import DateDialog, date_evidence
from vault_v2.health import HealthDoc, HealthStore, agent_entries
from vault_v2.health_pane import HealthPane
from vault_v2.receipts import ReceiptLog
from vault_v2.tasks import TaskDoc, TaskStore, agent_tasks
from vault_v2.tasks_pane import TasksPane


@pytest.fixture(scope="module")
def application():
    yield QApplication.instance() or QApplication([])


class _Reply:
    def __init__(self, rows):
        self.rows = rows

    def chat(self, *_args):
        return json.dumps(self.rows)


CASES = (
    ("correct", "Appointment scheduled for October 15, 2026.", "2026-10-15"),
    ("foreign", "Appointment scheduled for October 15, 2026.", "2027-01-01"),
    ("relative", "Schedule a follow-up within 30 days.", None),
    ("ambiguous", "Appointment scheduled for 10/11/2026.", "2026-10-11"),
    ("several", "Visit on 2026-10-15 or 2026-11-10.", None),
    ("mixed", "Visit on 2026-09-22; follow-up in 3 months.", "2026-09-22"),
)


def _models(family):
    docs, rows = [], []
    for name, quote, due in CASES:
        if family == "task":
            docs.append(TaskDoc(name, name + ".txt", name + ".txt", name * 16, quote))
            rows.append({"doc": name, "title": name, "quote": quote, "due": due})
        else:
            docs.append(HealthDoc(name, name + ".txt", name * 16, quote))
            rows.append({"doc": name, "kind": "VISIT", "label": name, "quote": quote, "date": due})
    models, dropped = (agent_tasks if family == "task" else agent_entries)(_Reply(rows), docs)
    assert not dropped
    assert len(models) == len(CASES)
    return models


@pytest.fixture(params=["task", "health"])
def pane_data(request, tmp_path, application):
    log = ReceiptLog(tmp_path / ".receipts")
    if request.param == "task":
        store = TaskStore(tmp_path / ".tasks", log)
        pane_class = TasksPane
    else:
        store = HealthStore(tmp_path / ".health", log)
        pane_class = HealthPane
    models = _models(request.param)
    for model in models:
        store.add(model)
    pane = pane_class(store, None, lambda: None)
    try:
        yield request.param, pane, store, models, log
    finally:
        pane.close()
        pane.deleteLater()
        application.processEvents()


def _row(pane, identifier):
    return next(pane.list.item(index) for index in range(pane.list.count())
                if pane.list.item(index).data(Qt.ItemDataRole.UserRole) == identifier)


def _choose(pane, *, value=None, cancel=False, before_save=lambda: None):
    inspected = []
    errors = []

    def inspect_dialog():
        dialog = QApplication.activeModalWidget()
        try:
            assert isinstance(dialog, DateDialog)
            inspected.append(dialog.windowTitle())
            if cancel:
                dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel).click()
                return
            dialog.no_date.setChecked(value is None)
            if value:
                dialog.date_edit.setDate(QDate.fromString(value, "yyyy-MM-dd"))
            before_save()
            dialog.buttons.button(QDialogButtonBox.StandardButton.Save).click()
        except Exception as error:
            errors.append(error)
            if isinstance(dialog, QDialog):
                dialog.reject()

    QTimer.singleShot(0, inspect_dialog)
    pane.edit_date_btn.click()
    assert inspected, "Edit due date did not open the actual modal chooser"
    assert not errors, errors


def test_saved_and_proposed_rows_show_date_evidence_and_quote(pane_data):
    _family, pane, _store, models, _log = pane_data
    for model, case in zip(models, CASES):
        assert case[1] in _row(pane, model.id).text()
    assert "2026-10-15" in _row(pane, models[0].id).text()
    assert "from quote" in _row(pane, models[0].id).text()
    foreign = _row(pane, models[1].id).text()
    assert "date not in quote" in foreign
    assert "2027-01-01" not in foreign
    assert "relative deadline" in _row(pane, models[2].id).text()
    assert "ambiguous date (US month/day order)" in _row(pane, models[3].id).text()
    assert "several dates in quote" in _row(pane, models[4].id).text()
    mixed_warning = "in the quote there is a date and a relative term — check and set the date yourself"
    assert mixed_warning in _row(pane, models[5].id).text()
    assert "relative deadline in quote" not in _row(pane, models[5].id).text()
    # Exercise the actual proposal rendering independently of already-saved ids.
    pane.store.has = lambda _identifier: False
    pane._show(models, [])
    assert "date not in quote" in pane.prop_list.item(1).text()
    assert CASES[2][1] in pane.prop_list.item(2).text()
    assert "relative deadline" in pane.prop_list.item(2).text()
    assert mixed_warning in pane.prop_list.item(5).text()


def test_owner_date_and_explicit_no_date_use_real_modal_store_and_receipts(pane_data):
    family, pane, store, models, log = pane_data
    foreign = models[1]
    assert not pane.edit_date_btn.isEnabled()
    pane.list.setCurrentItem(_row(pane, foreign.id))
    assert pane.edit_date_btn.isEnabled()
    generation = store.generation
    _choose(pane, value="2028-02-29")
    edited = next(item for item in store.all() if item.id == foreign.id)
    assert getattr(edited, "due" if family == "task" else "date") == "2028-02-29"
    assert edited.due_source == "owner"
    assert "date_not_in_quote" not in edited.flags
    assert "set by you" in _row(pane, foreign.id).text()
    assert "not verified" not in _row(pane, foreign.id).text()
    assert store.generation == generation + 1
    assert log.tail()[-1]["op"] == ("task_due_set" if family == "task" else "health_date_set")
    pane.list.setCurrentItem(_row(pane, foreign.id))
    _choose(pane, value=None)
    edited = next(item for item in store.all() if item.id == foreign.id)
    assert getattr(edited, "due" if family == "task" else "date") is None
    assert edited.due_source == "owner"
    assert "set by you" in _row(pane, foreign.id).text()
    assert "no verified date" not in _row(pane, foreign.id).text()
    assert store.generation == generation + 2
    assert log.verify() == len(models) + 2


def test_cancel_does_not_write_and_guard_refusal_is_visible(pane_data):
    _family, pane, store, models, log = pane_data
    target = models[0]
    pane.list.setCurrentItem(_row(pane, target.id))
    generation, before = store.generation, store.file.read_bytes()
    receipts = log.verify()
    _choose(pane, cancel=True)
    assert store.file.read_bytes() == before
    assert store.generation == generation
    assert log.verify() == receipts
    statuses = []
    pane.status.connect(statuses.append)
    _choose(pane, value="2027-01-01", before_save=lambda: setattr(log, "read_only", True))
    assert store.file.read_bytes() == before
    assert store.generation == generation
    assert log.verify() == receipts
    assert any("not completed" in status and "read-only" in status for status in statuses)
    assert not pane.edit_date_btn.isEnabled()


def test_historical_invalid_date_is_visible_without_rewriting_saved_file(pane_data):
    family, pane, store, models, _log = pane_data
    identifier = models[1].id
    data = json.loads(store.file.read_text(encoding="utf-8"))
    row = data["tasks" if family == "task" else "entries"][identifier]
    row.pop("flags", None)
    row.pop("due_source", None)
    row["due" if family == "task" else "date"] = "2027-01-01"
    store.file.write_text(json.dumps(data), encoding="utf-8")
    before = store.file.read_bytes()
    store._load()
    pane.reload()
    assert "not verified" in _row(pane, identifier).text()
    assert "date not in quote" in _row(pane, identifier).text()
    assert store.file.read_bytes() == before


def test_dialog_no_date_and_year_boundaries(application):
    dialog = DateDialog(None)
    try:
        assert dialog.no_date.isChecked()
        assert not dialog.date_edit.isEnabled()
        assert dialog.chosen_date() is None
        dialog.no_date.setChecked(False)
        assert dialog.date_edit.isEnabled()
        for value in ("2026-12-31", "2027-01-01", "2028-02-29", "9999-12-31"):
            dialog.date_edit.setDate(QDate.fromString(value, "yyyy-MM-dd"))
            assert dialog.chosen_date() == value
    finally:
        dialog.close()
        dialog.deleteLater()


def test_owner_without_date_does_not_claim_quote_provenance():
    assert date_evidence("owner", ()) == "set by you"


@pytest.mark.parametrize("value", ["2026", "0001-01-01"])
def test_unrepresentable_calendar_value_is_never_silently_replaced(application, value):
    dialog = DateDialog(value)
    try:
        assert dialog.no_date.isChecked()
        assert dialog.chosen_date() is None
    finally:
        dialog.close()
        dialog.deleteLater()


def test_narrow_lists_keep_full_quote_available(pane_data, application):
    family, pane, _store, models, _log = pane_data
    pane.setFixedWidth(320)
    pane.resize(320, 700)
    pane.show()
    application.processEvents()
    model = models[0]
    saved = _row(pane, model.id)
    assert model.quote in saved.text()
    assert model.quote in saved.toolTip()
    # A narrow date row must grow vertically to show the wrapped source quote.
    assert pane.list.visualItemRect(saved).height() >= pane.list.fontMetrics().height() * 3
    pane.store.has = lambda _identifier: False
    pane._show(models, [])
    application.processEvents()
    proposed = pane.prop_list.item(0)
    assert model.quote in proposed.text()
    assert model.quote in proposed.toolTip()
    assert pane.prop_list.visualItemRect(proposed).height() >= pane.prop_list.fontMetrics().height() * 3
    assert pane.edit_date_btn.text() == ("Edit due date…" if family == "task" else "Edit date…")
