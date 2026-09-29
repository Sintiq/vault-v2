"""Trash recovery via the real dialog and synthetic VaultOps storage."""

import json
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.trash_dialog import TrashDialog


@pytest.fixture(scope="module")
def application():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def ops(tmp_path: Path):
    return VaultOps(VaultPaths(tmp_path / "synthetic-vault"))


def _trash_file(ops, name: str, text: str = "synthetic recoverable bytes"):
    source = ops.paths.staging / name
    source.write_text(text, encoding="utf-8")
    return source, ops.trash(source).dst


def _close(dialog, application):
    dialog.close()
    dialog.deleteLater()
    application.processEvents()


def test_restore_requires_a_selection_and_restores_healthy_file(application, ops):
    source, slot = _trash_file(ops, "healthy.txt")
    dialog = TrashDialog(ops)
    try:
        assert dialog.restore_btn.isEnabled() is False
        dialog.list.item(0).setSelected(True)
        assert dialog.restore_btn.isEnabled() is True
        dialog.restore_btn.click()
        assert source.read_text(encoding="utf-8") == "synthetic recoverable bytes"
        assert not slot.exists()
        assert dialog.restored == 1
        assert dialog.list.count() == 0
        assert dialog.restore_btn.isEnabled() is False
    finally:
        _close(dialog, application)


def test_empty_trash_requires_permanent_delete_confirmation(application, ops, monkeypatch):
    _source, slot = _trash_file(ops, "keep-on-cancel.txt")
    receipts_before = ops.log.verify()
    prompts = []

    def cancel(_parent, title, text, buttons, default):
        prompts.append((title, text, buttons, default))
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", cancel)
    dialog = TrashDialog(ops)
    try:
        dialog.purge_btn.click()
        assert len(prompts) == 1
        title, text, buttons, default = prompts[0]
        assert title == "Empty trash"
        assert "Permanently delete" in text and "cannot be undone" in text
        assert "Damaged entries" in text and "will be kept" in text
        assert buttons == QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        assert default == QMessageBox.StandardButton.No
        assert slot.read_text(encoding="utf-8") == "synthetic recoverable bytes"
        assert ops.log.verify() == receipts_before
        assert dialog.list.count() == 1
    finally:
        _close(dialog, application)


def test_damaged_and_orphan_rows_are_visible_but_never_restorable(application, ops):
    _source, healthy = _trash_file(ops, "healthy.txt")
    _source, damaged = _trash_file(ops, "damaged.txt")
    damaged.with_name(damaged.name + ".trash.json").write_text("{broken", encoding="utf-8")
    orphan = ops.paths.trash / "orphan.txt"
    orphan.write_bytes(b"synthetic orphan bytes")
    dialog = TrashDialog(ops)
    try:
        rows = {dialog.list.item(i).data(Qt.ItemDataRole.UserRole): dialog.list.item(i)
                for i in range(dialog.list.count())}
        assert set(rows) == {str(healthy), str(damaged), str(orphan)}
        assert "manifest damaged" in rows[str(damaged)].text()
        assert "no manifest" in rows[str(orphan)].text()
        rows[str(damaged)].setSelected(True)
        assert dialog.restore_btn.isEnabled() is False
        rows[str(healthy)].setSelected(True)
        assert dialog.restore_btn.isEnabled() is False, "mixed selection must not restore partly"
        rows[str(damaged)].setSelected(False)
        assert dialog.restore_btn.isEnabled() is True
        rows[str(healthy)].setSelected(False)
        rows[str(orphan)].setSelected(True)
        assert dialog.restore_btn.isEnabled() is False
    finally:
        _close(dialog, application)


def test_confirmed_empty_trash_keeps_damaged_entries_and_summary(application, ops, monkeypatch):
    _source, healthy = _trash_file(ops, "healthy.txt")
    _source, damaged = _trash_file(ops, "damaged.txt")
    manifest = damaged.with_name(damaged.name + ".trash.json")
    manifest.write_text("{broken", encoding="utf-8")
    orphan = ops.paths.trash / "orphan.txt"
    orphan.write_bytes(b"synthetic orphan bytes")
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    dialog = TrashDialog(ops)
    try:
        dialog.purge_btn.click()
        assert not healthy.exists()
        assert damaged.read_text(encoding="utf-8") == "synthetic recoverable bytes"
        assert manifest.read_text(encoding="utf-8") == "{broken"
        assert orphan.read_bytes() == b"synthetic orphan bytes"
        assert dialog.list.count() == 2
        assert dialog.info.text() == "1 removed, 2 damaged kept"
        assert dialog.restore_btn.isEnabled() is False
        assert ops.log.tail(1)[0]["op"] == "purge"
    finally:
        _close(dialog, application)


def test_empty_trash_reports_a_journal_refusal_without_deleting(application, ops, monkeypatch):
    _source, slot = _trash_file(ops, "safe.txt")
    ops.log.file.write_text("{broken", encoding="utf-8")
    warnings = []
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda _parent, _title, text: warnings.append(text))
    dialog = TrashDialog(ops)
    try:
        dialog.purge_btn.click()
        assert slot.read_text(encoding="utf-8") == "synthetic recoverable bytes"
        assert warnings and "Receipt journal is invalid" in warnings[0]
        assert dialog.info.text().startswith("Empty trash failed:")
        assert dialog.list.count() == 1
    finally:
        _close(dialog, application)


def test_unreadable_trash_reports_failure_and_disables_actions(application, ops, monkeypatch):
    _trash_file(ops, "safe.txt")
    original_iterdir = Path.iterdir

    def denied(path):
        if path == ops.paths.trash:
            raise PermissionError("synthetic trash access denied")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", denied)
    dialog = TrashDialog(ops)
    try:
        assert "Cannot read Trash" in dialog.info.text()
        assert dialog.restore_btn.isEnabled() is False
        assert dialog.purge_btn.isEnabled() is False
    finally:
        _close(dialog, application)


def test_legacy_origin_is_displayed_without_resolving_external_filesystem(application, ops, monkeypatch):
    _source, slot = _trash_file(ops, "legacy.txt")
    origin = Path(r"\\synthetic-server.invalid\share\old-vault\staging\legacy.txt")
    manifest = slot.with_name(slot.name + ".trash.json")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["origin"] = str(origin)
    manifest.write_text(json.dumps(data), encoding="utf-8")
    original_resolve = Path.resolve

    def reject_external_resolution(path, *args, **kwargs):
        if path == origin:
            raise AssertionError("display must not resolve the legacy origin")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", reject_external_resolution)
    dialog = TrashDialog(ops)
    try:
        assert dialog.list.count() == 1
        text = dialog.list.item(0).text()
        assert text.startswith("legacy.txt")
        assert "?/" in text
        assert str(origin.parent) in text
    finally:
        _close(dialog, application)


def test_origin_equal_to_a_pane_root_does_not_crash_the_dialog(application, ops):
    _source, slot = _trash_file(ops, "malformed-origin.txt")
    manifest = slot.with_name(slot.name + ".trash.json")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["origin"] = str(ops.paths.staging)
    manifest.write_text(json.dumps(data), encoding="utf-8")
    dialog = TrashDialog(ops)
    try:
        assert dialog.list.count() == 1
        text = dialog.list.item(0).text()
        assert "?/" in text or "manifest damaged" in text
    finally:
        _close(dialog, application)
