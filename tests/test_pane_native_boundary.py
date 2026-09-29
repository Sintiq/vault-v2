"""Real QFileSystemModel paths must not launder a Windows junction alias."""
import os
from pathlib import Path

import pytest
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from vault_v2.cards import CardStore
from vault_v2.ops import VaultOps
from vault_v2.panes import Pane
from vault_v2.paths import VaultPaths
from vault_v2.errors import VaultError


@pytest.mark.skipif(os.name != "nt", reason="Windows junction and Qt boundary")
def test_native_pane_does_not_publish_or_hash_junction_alias(tmp_path, monkeypatch):
    import _winapi

    app = QApplication.instance() or QApplication([])
    ops = VaultOps(VaultPaths(tmp_path / "synthetic-vault"))
    store = CardStore(ops.paths.root / ".cards", ops.log)
    target = ops.paths.personal / "target"
    target.mkdir()
    source = target / "synthetic.txt"
    source.write_text("SYNTHETIC PERSONAL", encoding="utf-8")
    visible = ops.paths.staging / "ordinary.txt"
    visible.write_text("SYNTHETIC VISIBLE", encoding="utf-8")
    link = ops.paths.staging / "linked"
    _winapi.CreateJunction(str(target), str(link))
    widget = None
    original = Path.open

    def no_target_read(path, *args, **kwargs):
        if path == source or path.is_relative_to(link):
            raise AssertionError("Qt pane caused a linked document hash/read")
        return original(path, *args, **kwargs)

    try:
        monkeypatch.setattr(Path, "open", no_target_read)
        widget = Pane("staging", "Staging", ops.paths.staging, ops, store)
        widget.show()
        for _ in range(100):
            alias_index = widget.model.index(str(link))
            ordinary_index = widget.proxy.mapFromSource(widget.model.index(str(visible)))
            if alias_index.isValid() and ordinary_index.isValid():
                break
            QTest.qWait(10)
        assert alias_index.isValid() and ordinary_index.isValid()
        assert Path(widget.model.filePath(alias_index)) == link
        assert not widget.proxy.mapFromSource(alias_index).isValid()
        assert ops.log.tail() == []
    finally:
        if widget is not None:
            widget.close()
            widget.deleteLater()
            app.processEvents()
        link.rmdir()  # This test junction only.


def test_read_only_missing_pane_is_empty_without_creating_directories(tmp_path):
    app = QApplication.instance() or QApplication([])
    paths = VaultPaths(tmp_path / "synthetic-vault")
    paths.root.mkdir()
    ops = VaultOps(paths, read_only=True)
    widget = Pane("staging", "Staging", paths.staging, ops)
    try:
        app.processEvents()
        assert widget.view.model().rowCount() == 0
        assert "unavailable" in widget.sub.text().lower()
        assert not paths.staging.exists() and ops.log.tail() == []
        with pytest.raises(VaultError):
            widget.set_dir(paths.staging / "missing-child")
        assert widget.current_dir() == paths.staging
        # Synthetic stand-in for the first window completing initialization.
        paths.staging.mkdir()
        (paths.staging / "visible.txt").write_text("SYNTHETIC", encoding="utf-8")
        widget.refresh()
        assert widget.view.model() is widget.proxy
        assert widget.view.rootIndex().isValid()
        assert ops.log.tail() == []
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction boundary")
def test_read_only_missing_pane_below_junction_is_not_an_empty_placeholder(tmp_path):
    import _winapi

    app = QApplication.instance() or QApplication([])
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    _winapi.CreateJunction(str(actual), str(alias))
    try:
        paths = VaultPaths(alias)
        ops = VaultOps(paths, read_only=True)
        with pytest.raises(VaultError, match="Linked"):
            Pane("staging", "Staging", paths.staging, ops)
        assert list(actual.iterdir()) == []
    finally:
        app.processEvents()
        alias.rmdir()  # This test junction only.
