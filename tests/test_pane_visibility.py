"""Visible pane navigation is lexical and never admits service paths."""
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from vault_v2.cards import CardStore
from vault_v2.errors import VaultError
from vault_v2.ops import VaultOps
from vault_v2.panes import Pane
from vault_v2.paths import VaultPaths


@pytest.fixture
def pane(tmp_path):
    app = QApplication.instance() or QApplication([])
    ops = VaultOps(VaultPaths(tmp_path / "synthetic-vault"))
    store = CardStore(ops.paths.root / ".cards", ops.log)
    (ops.paths.staging / "ordinary.txt").write_text("synthetic document", encoding="utf-8")
    widget = Pane("staging", "Staging", ops.paths.staging, ops, store)
    widget.show()
    try:
        yield widget
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_activating_a_directory_replaced_by_reparse_shows_refusal(pane, monkeypatch):
    directory = pane.root / "visible"
    directory.mkdir()
    for _ in range(100):
        index = pane.proxy.mapFromSource(pane.model.index(str(directory)))
        if index.isValid():
            break
        QTest.qWait(10)
    assert index.isValid()
    original = Path.lstat
    def replaced(path, *args, **kwargs):
        if path == directory:
            return SimpleNamespace(st_mode=stat.S_IFDIR,
                                   st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", replaced)
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _parent, _title, text: warnings.append(text))
    pane.view.doubleClicked.emit(index)
    assert pane.current_dir() == pane.root
    assert warnings and "linked" in warnings[0].lower()


@pytest.mark.parametrize("name", [".private", "case.TRASH.JSON"])
def test_hidden_directory_navigation_refuses_and_keeps_current_directory(pane, name):
    target = pane.root / name
    target.mkdir()
    with pytest.raises(VaultError):
        pane.set_dir(target)
    assert pane.current_dir() == pane.root
    assert pane.view.rootIndex().isValid()


@pytest.mark.parametrize("kind", ["manifest", "reparse"])
def test_filter_prunes_denied_files_but_keeps_normal_rows(pane, monkeypatch, kind):
    forbidden = pane.root / ("private.TRASH.JSON" if kind == "manifest" else "linked.txt")
    forbidden.write_text("synthetic excluded document", encoding="utf-8")
    original = Path.lstat
    if kind == "reparse":
        def lstat(path, *args, **kwargs):
            if path == forbidden:
                return SimpleNamespace(st_mode=stat.S_IFREG,
                                       st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, "lstat", lstat)
    for _ in range(100):
        source = pane.model.index(str(forbidden))
        ordinary = pane.proxy.mapFromSource(pane.model.index(str(pane.root / "ordinary.txt")))
        if source.isValid() and ordinary.isValid():
            break
        QTest.qWait(10)
    pane.set_filter("")
    assert ordinary.isValid(), "normal file and Qt structural ancestors remain mapped"
    assert not pane.proxy.mapFromSource(source).isValid()


def test_relative_configured_root_keeps_pane_navigation_working(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.chdir(tmp_path)
    ops = VaultOps(VaultPaths(Path("synthetic-relative")))
    child = ops.paths.staging / "ordinary"
    child.mkdir()
    widget = Pane("staging", "Staging", ops.paths.staging, ops)
    try:
        assert widget.current_dir() == ops.paths.staging.absolute()
        widget.set_dir(child)
        assert widget.current_dir() == child.absolute()
        widget.go_up()
        assert widget.current_dir() == ops.paths.staging.absolute()
        assert widget.view.rootIndex().isValid()
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_dot_named_vault_root_keeps_structural_qt_ancestors(tmp_path):
    app = QApplication.instance() or QApplication([])
    ops = VaultOps(VaultPaths(tmp_path / ".synthetic-vault"))
    source = ops.paths.staging / "ordinary.txt"
    source.write_text("synthetic", encoding="utf-8")
    widget = Pane("staging", "Staging", ops.paths.staging, ops)
    try:
        assert widget.view.rootIndex().isValid()
        for _ in range(100):
            mapped = widget.proxy.mapFromSource(widget.model.index(str(source)))
            if mapped.isValid():
                break
            QTest.qWait(10)
        assert mapped.isValid()
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()
