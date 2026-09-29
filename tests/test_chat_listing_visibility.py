"""Chat listings retain their stricter filter plus the common ancestor check."""
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from vault_v2.agent_door import DoorError
from vault_v2.chat import ChatPane


def test_chat_refuses_reparse_ancestor_before_any_staging_enumeration(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    staging = tmp_path / "synthetic-vault" / "staging"
    staging.mkdir(parents=True)
    (staging / "private-name.txt").write_text("synthetic", encoding="utf-8")
    # Unsupported backend avoids any network or warmup; listing is public and
    # independent from model availability.
    pane = ChatPane(staging, {"agent_backend": "unsupported"})
    original_lstat, original_scandir = Path.lstat, os.scandir

    def reparse(path, *args, **kwargs):
        if path == staging.parent:
            return SimpleNamespace(st_mode=stat.S_IFDIR,
                                   st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return original_lstat(path, *args, **kwargs)

    def refused_scandir(path):
        if isinstance(path, (str, os.PathLike)) and Path(path) == staging:
            pytest.fail("chat enumerated a staging directory behind a reparse ancestor")
        return original_scandir(path)

    monkeypatch.setattr(Path, "lstat", reparse)
    monkeypatch.setattr(os, "scandir", refused_scandir)
    try:
        with pytest.raises(DoorError, match="^unusable reply$"):
            pane.staging_listing()
    finally:
        pane.close()
        pane.deleteLater()
        app.processEvents()


def test_relative_staging_listing_preserves_stricter_hidden_filter(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.chdir(tmp_path)
    staging = Path("synthetic-relative") / "staging"
    staging.mkdir(parents=True)
    (staging / "ordinary.txt").write_text("synthetic", encoding="utf-8")
    hidden = staging / "system.txt"
    hidden.write_text("synthetic", encoding="utf-8")
    original = Path.lstat

    def system_file(path, *args, **kwargs):
        if path.absolute() == hidden.absolute():
            return SimpleNamespace(st_mode=stat.S_IFREG,
                                   st_file_attributes=stat.FILE_ATTRIBUTE_SYSTEM)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", system_file)
    pane = ChatPane(staging, {"agent_backend": "unsupported"})
    try:
        listing = pane.staging_listing()
        assert "ordinary.txt" in listing and "system.txt" not in listing
    finally:
        pane.close()
        pane.deleteLater()
        app.processEvents()
