"""Owner file actions through real Qt selection, menus and dialogs."""
from pathlib import Path

import pytest
from PySide6.QtCore import QItemSelectionModel, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QPlainTextEdit, QToolBar

from vault_v2 import cards as card_types
from vault_v2.cards import CardStore
from vault_v2.ops import VaultOps
from vault_v2.panes import Pane
from vault_v2.paths import VaultPaths


@pytest.fixture
def app():
    application = QApplication.instance() or QApplication([])
    yield application
    application.processEvents()


@pytest.fixture
def pane(tmp_path, app):
    card_types.load_shelves({"shelves": ["IMMIGRATION"]})
    ops = VaultOps(VaultPaths(tmp_path / "synthetic"))
    store = CardStore(ops.paths.root / ".cards", ops.log)
    for name in ("alpha.txt", "bravo.txt"):
        (ops.paths.staging / name).write_text("Synthetic " + name, encoding="utf-8")
    widget = Pane("staging", "Staging", ops.paths.staging, ops, store)
    widget.resize(700, 500)
    widget.show()
    try:
        yield widget
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()
        card_types.load_shelves({})


def select(pane, *names):
    pane.view.selectionModel().clearSelection()
    for name in names:
        for _ in range(100):
            source = pane.model.index(str(pane.root / name))
            index = pane.proxy.mapFromSource(source)
            if index.isValid():
                break
            QTest.qWait(10)
        assert index.isValid(), name
        pane.view.selectionModel().select(index, QItemSelectionModel.SelectionFlag.Select
                                         | QItemSelectionModel.SelectionFlag.Rows)
        pane.view.selectionModel().setCurrentIndex(index, QItemSelectionModel.SelectionFlag.NoUpdate)
    pane.view.setFocus()


def context(pane, inspect):
    result, errors = [], []

    def opened():
        menu = QApplication.activePopupWidget()
        try:
            result.append(inspect(menu))
        except BaseException as error:
            errors.append(error)
        finally:
            if menu is not None:
                menu.close()

    QTimer.singleShot(0, opened)
    pane.view.customContextMenuRequested.emit(pane.view.visualRect(pane.view.currentIndex()).center())
    if errors:
        raise errors[0]
    return result[0]


def shelf_menu(menu):
    submenu = next(action.menu() for action in menu.actions() if action.text() == "Shelf")
    submenu.popup(menu.mapToGlobal(menu.rect().topRight()))
    QApplication.processEvents()
    return submenu


def test_opening_context_menu_does_not_read_large_file_contents(pane, monkeypatch):
    source = pane.root / "large.bin"
    with source.open("wb") as output:
        output.truncate(51 * 1024 * 1024)
    select(pane, source.name)
    original, reads = Path.open, []

    def observed(path, mode="r", *args, **kwargs):
        if path == source and "r" in mode:
            reads.append(path)
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", observed)
    assert context(pane, lambda menu: menu.actions()[0].text()) == "Open"
    assert reads == []
    assert pane.ops.log.tail() == []


def test_context_shelves_include_owner_shelf_and_mark_current(pane):
    source = pane.root / "alpha.txt"
    pane.proxy.cards.set_shelf(source, "HEALTH")
    select(pane, source.name)

    rows = context(pane, lambda menu: [(action.text(), action.isChecked())
                                      for action in shelf_menu(menu).actions() if not action.isSeparator()])

    assert rows == [("INBOX", False), ("HEALTH", True), ("TAXES", False),
                    ("FINANCE", False), ("INSURANCE", False), ("IDENTITY", False),
                    ("PHOTOS", False), ("IMMIGRATION", False), ("Manage shelves…", False)]


def choose_shelf(menu, name):
    next(action for action in shelf_menu(menu).actions() if action.text() == name).trigger()


def test_one_menu_choice_sets_every_selected_file_with_one_receipt_each(pane):
    select(pane, "alpha.txt", "bravo.txt")
    messages = []
    pane.status.connect(messages.append)

    context(pane, lambda menu: choose_shelf(menu, "IMMIGRATION"))

    assert [pane.proxy.cards.for_path(pane.root / name).shelf
            for name in ("alpha.txt", "bravo.txt")] == ["IMMIGRATION", "IMMIGRATION"]
    assert [row["op"] for row in pane.ops.log.tail()] == ["card_confirm", "card_confirm"]
    assert messages[-1] == "2 set, 0 failed"


def test_failed_file_does_not_prevent_other_shelf_assignments(pane, monkeypatch):
    select(pane, "alpha.txt", "bravo.txt")
    source = pane.root / "alpha.txt"
    original = Path.lstat

    def denied(path, *args, **kwargs):
        if path == source:
            raise PermissionError("synthetic access denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", denied)
    warnings, messages = [], []
    monkeypatch.setattr(QMessageBox, "warning", lambda _parent, _title, text: warnings.append(text))
    pane.status.connect(messages.append)

    context(pane, lambda menu: choose_shelf(menu, "HEALTH"))

    assert pane.proxy.cards.for_path(source) is None
    assert pane.proxy.cards.for_path(pane.root / "bravo.txt").shelf == "HEALTH"
    assert [row["op"] for row in pane.ops.log.tail()] == ["card_confirm"]
    assert messages[-1] == "1 set, 1 failed"
    assert "alpha.txt" in warnings[0] and "synthetic access denied" in warnings[0]
    assert source.read_text(encoding="utf-8") == "Synthetic alpha.txt"


def test_readonly_shelf_actions_are_disabled_and_do_not_write(pane):
    select(pane, "alpha.txt", "bravo.txt")
    pane.ops.log.read_only = True
    rows = context(pane, lambda menu: [(action.text(), action.isEnabled())
                                      for action in shelf_menu(menu).actions() if not action.isSeparator()])
    assert rows and not any(enabled for _, enabled in rows)
    pane.set_shelf_selected("HEALTH")
    assert pane.proxy.cards.all() == [] and pane.ops.log.verify() == 0


def test_mixed_selection_does_not_mark_a_common_shelf(pane):
    pane.proxy.cards.set_shelf(pane.root / "alpha.txt", "HEALTH")
    pane.proxy.cards.set_shelf(pane.root / "bravo.txt", "TAXES")
    select(pane, "alpha.txt", "bravo.txt")
    assert not any(context(pane, lambda menu: [action.isChecked() for action in shelf_menu(menu).actions()]))


def test_shelf_management_moves_off_toolbar_but_retains_shortcut_and_menu(tmp_path, app, monkeypatch):
    import urllib.request
    from vault_v2.main import MainWindow

    def offline(*args, **kwargs):
        raise ConnectionRefusedError("synthetic UI test is offline")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", offline)
    window = MainWindow(tmp_path / "synthetic-window", start_services=False)
    window.show()
    try:
        toolbar = [action.text() for bar in window.findChildren(QToolBar) for action in bar.actions()]
        assert "Shelves…" not in toolbar and "Manage shelves…" not in toolbar
        shortcuts = [action for action in window.actions() if action.shortcut().toString() == "Ctrl+Shift+S"]
        assert len(shortcuts) == 1 and shortcuts[0].isEnabled()
        opened = []

        def close_manager():
            dialog = QApplication.activeModalWidget()
            if dialog is not None:
                opened.append(dialog.windowTitle())
                dialog.reject()

        def manage(menu):
            action = next(action for action in shelf_menu(menu).actions() if action.text() == "Manage shelves…")
            QTimer.singleShot(0, close_manager)
            action.trigger()

        context(window.panes["staging"], manage)
        app.processEvents()
        assert opened == ["Shelves"]
        QTimer.singleShot(0, close_manager)
        shortcuts[0].trigger()
        assert opened == ["Shelves", "Shelves"]
    finally:
        window.close()
        app.processEvents()


@pytest.mark.parametrize("action", ["enter", "double_click", "menu"])
@pytest.mark.parametrize("read_only", [False, True])
def test_file_activation_opens_internal_text_without_receipt(pane, app, action, read_only):
    pane.ops.log.read_only = read_only
    select(pane, "alpha.txt")
    before = pane.ops.log.verify()
    if action == "enter":
        QTest.keyClick(pane.view, Qt.Key.Key_Return)
    elif action == "double_click":
        QTest.mouseClick(pane.view.viewport(), Qt.MouseButton.LeftButton,
                         pos=pane.view.visualRect(pane.view.currentIndex()).center())
        QTest.mouseDClick(pane.view.viewport(), Qt.MouseButton.LeftButton,
                         pos=pane.view.visualRect(pane.view.currentIndex()).center())
    else:
        context(pane, lambda menu: next(item for item in menu.actions() if item.text() == "Open").trigger())
    app.processEvents()
    dialogs = [dialog for dialog in pane.findChildren(QDialog) if dialog.isVisible()]
    try:
        assert len(dialogs) == 1
        text = dialogs[0].findChild(QPlainTextEdit)
        assert text is not None and text.isReadOnly() and text.toPlainText() == "Synthetic alpha.txt"
        assert pane.ops.log.verify() == before
    finally:
        for dialog in dialogs:
            dialog.close()


def test_context_open_uses_selected_file_even_when_current_row_differs(pane, app):
    select(pane, "alpha.txt")
    other = pane.proxy.mapFromSource(pane.model.index(str(pane.root / "bravo.txt")))
    pane.view.selectionModel().setCurrentIndex(other, QItemSelectionModel.SelectionFlag.NoUpdate)
    assert pane.selected_paths() == [pane.root / "alpha.txt"]

    context(pane, lambda menu: next(item for item in menu.actions() if item.text() == "Open").trigger())
    app.processEvents()
    dialogs = [dialog for dialog in pane.findChildren(QDialog) if dialog.isVisible()]
    try:
        assert len(dialogs) == 1
        assert dialogs[0].findChild(QPlainTextEdit).toPlainText() == "Synthetic alpha.txt"
    finally:
        for dialog in dialogs:
            dialog.close()


def test_closing_vault_closes_its_modeless_previews(tmp_path, app, monkeypatch):
    import urllib.request
    from vault_v2.main import MainWindow

    def offline(*args, **kwargs):
        raise ConnectionRefusedError("synthetic lifecycle test is offline")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", offline)
    window = MainWindow(tmp_path / "synthetic-lifecycle", start_services=False)
    source = window.paths.staging / "alpha.txt"
    source.write_text("Synthetic lifecycle preview", encoding="utf-8")
    window.show()
    pane = window.panes["staging"]
    try:
        select(pane, source.name)
        QTest.keyClick(pane.view, Qt.Key.Key_Return)
        app.processEvents()
        assert any(dialog.isVisible() for dialog in window.findChildren(QDialog))
        receipts = window.ops.log.verify()

        assert window.close()
        app.processEvents()

        assert not window.isVisible()
        assert not any(dialog.isVisible() for dialog in window.findChildren(QDialog))
        assert window.ops.log.verify() == receipts
    finally:
        window.close()
        app.processEvents()
