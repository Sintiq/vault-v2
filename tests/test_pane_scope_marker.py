"""Folder permission presentation through owner actions and public Qt roles."""
import pytest
from PySide6.QtCore import QItemSelectionModel, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from vault_v2.ops import VaultOps
from vault_v2.panes import Pane
from vault_v2.paths import VaultPaths


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    application.setQuitOnLastWindowClosed(False)
    return application


def select_folder(pane, path):
    model = pane.view.model()
    for _ in range(100):
        root = pane.view.rootIndex()
        for row in range(model.rowCount(root)):
            index = model.index(row, 0, root)
            if pane.path_for(index) == path:
                selection = pane.view.selectionModel()
                selection.select(index, QItemSelectionModel.SelectionFlag.ClearAndSelect
                                 | QItemSelectionModel.SelectionFlag.Rows)
                selection.setCurrentIndex(index, QItemSelectionModel.SelectionFlag.NoUpdate)
                return index
        QTest.qWait(10)
    pytest.fail(f"Folder was not displayed: {path.name}")


def choose_context_action(pane, label):
    errors = []

    def opened():
        menu = QApplication.activePopupWidget()
        try:
            action = next((action for action in menu.actions() if action.text() == label), None)
            assert action is not None, label
            assert action.isEnabled()
            action.trigger()
        except BaseException as exc:
            errors.append(exc)
        finally:
            if menu is not None:
                menu.close()

    QTimer.singleShot(0, opened)
    pane.view.customContextMenuRequested.emit(pane.view.visualRect(pane.view.currentIndex()).center())
    if errors:
        raise errors[0]


def test_scope_marker_preserves_folder_name_and_describes_direct_and_inherited_permission(tmp_path, app):
    ops = VaultOps(VaultPaths(tmp_path / "synthetic-vault"))
    parent = ops.paths.documents / "Synthetic archive"
    child = parent / "Child archive"
    child.mkdir(parents=True)
    pane = Pane("documents", "Documents", ops.paths.documents, ops)
    try:
        pane.show()
        assert select_folder(pane, parent).data() == "Synthetic archive"
        choose_context_action(pane, "Allow agent to read text here")

        direct = select_folder(pane, parent)
        assert direct.data() == "Synthetic archive [A]"
        assert direct.data(Qt.ItemDataRole.ToolTipRole) == "Agent text is allowed here"
        assert direct.data(Qt.ItemDataRole.AccessibleTextRole) == (
            "Synthetic archive. Agent text is allowed here"
        )
        assert direct.data(Qt.ItemDataRole.AccessibleDescriptionRole) == "Agent text is allowed here"

        pane.set_dir(parent)
        inherited = select_folder(pane, child)
        ancestor = str(parent.relative_to(ops.paths.root))
        explanation = f"Agent text is allowed by {ancestor}"
        assert inherited.data() == "Child archive [A]"
        assert inherited.data(Qt.ItemDataRole.ToolTipRole) == explanation
        assert inherited.data(Qt.ItemDataRole.AccessibleTextRole) == f"Child archive. {explanation}"
        assert inherited.data(Qt.ItemDataRole.AccessibleDescriptionRole) == explanation

        choose_context_action(pane, f"Stop reading text at {ancestor}")
        revoked = select_folder(pane, child)
        assert revoked.data() == "Child archive"
        for role in (Qt.ItemDataRole.ToolTipRole, Qt.ItemDataRole.AccessibleTextRole,
                     Qt.ItemDataRole.AccessibleDescriptionRole):
            assert "Agent text is allowed" not in str(revoked.data(role))
        pane.go_up()
        assert select_folder(pane, parent).data() == "Synthetic archive"
    finally:
        pane.close()
        pane.deleteLater()
        app.processEvents()
