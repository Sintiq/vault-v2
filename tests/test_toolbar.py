"""The toolbar: the order the owner asked for, and Windows' own glyphs on every button."""
from __future__ import annotations

import urllib.request

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QToolBar

from vault_v2.icons import GLYPHS, glyph_icon, icon_font

EXPECTED = [
    "Upload…", "Export…", "|",
    "F5 Copy", "F6 Move", "F7 Folder", "F8 Trash", "|",
    "Sort…", "Ask…", "|",
    "Clear Staging", "Trash…", "Backup…", "|",
    "Setup…", "Phone…", "Receipts", "Pending…", "Theme", "|",
]


@pytest.fixture
def app():
    application = QApplication.instance() or QApplication([])
    yield application
    application.processEvents()


@pytest.fixture
def window(tmp_path, app, monkeypatch):
    from vault_v2.main import MainWindow

    def offline(*args, **kwargs):
        raise ConnectionRefusedError("synthetic UI test is offline")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", offline)
    w = MainWindow(tmp_path / "synthetic-window", start_services=False)
    w.show()
    try:
        yield w
    finally:
        w.close()
        w.deleteLater()
        app.processEvents()


def _toolbar_actions(window):
    bar = next(b for b in window.findChildren(QToolBar) if b.windowTitle() == "main")
    return bar, [a for a in bar.actions()]


def test_export_sits_next_to_upload_and_the_groups_are_separated(window) -> None:
    _bar, actions = _toolbar_actions(window)
    texts = ["|" if a.isSeparator() else a.text() for a in actions if a.text() or a.isSeparator()]
    # The filter box is a widget action with no text; everything else is listed.
    assert texts == EXPECTED


def test_working_buttons_show_glyph_and_word_while_the_records_are_glyph_only(window) -> None:
    bar, actions = _toolbar_actions(window)
    assert bar.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextBesideIcon
    assert bar.iconSize().width() == 16
    styles = {a.text(): bar.widgetForAction(a).toolButtonStyle() for a in actions if a.text()}
    for text in ("Upload…", "Export…", "F5 Copy", "Sort…", "Ask…", "Backup…"):
        assert styles[text] == Qt.ToolButtonStyle.ToolButtonTextBesideIcon, text
    for text in ("Setup…", "Phone…", "Receipts", "Pending…", "Theme"):
        assert styles[text] == Qt.ToolButtonStyle.ToolButtonIconOnly, text
    tips = {a.text(): a.toolTip() for a in actions if a.text()}
    assert tips["Phone…"].startswith("Phone") and "Ctrl+P" in tips["Phone…"]
    assert tips["Theme"].startswith("Dark theme")
    # The glyphs that could be misread say what they do: Undo on Trash… is "restore", not "undo".
    assert tips["Trash…"] == "View Trash and restore files"
    assert tips["Sort…"].startswith("Let the agent suggest shelves")
    assert tips["Export…"].endswith("(Ctrl+E)")
    # Every button explains itself when the mouse rests on it.
    assert all(tips[text] for text in tips), [text for text in tips if not tips[text]]
    assert tips["F8 Trash"].startswith("Send the selected files to Trash") and tips["F8 Trash"].endswith("(F8)")


def test_every_button_has_a_windows_glyph_that_follows_the_theme(window) -> None:
    if icon_font() is None:
        pytest.skip("Windows icon font not installed here")
    _bar, actions = _toolbar_actions(window)
    buttons = [a for a in actions if a.text() and not a.isSeparator()]
    assert all(not a.icon().isNull() for a in buttons), [a.text() for a in buttons if a.icon().isNull()]
    assert all(a.property("icon_name") in GLYPHS for a in buttons)
    assert window.theme_action.property("icon_name") == "moon"
    window.toggle_theme()
    assert window.theme_action.property("icon_name") == "sun" and window.theme_action.toolTip().startswith("Light theme")
    assert not window.theme_action.icon().isNull()
    window.toggle_theme()
    assert window.theme_action.property("icon_name") == "moon"


def test_a_glyph_is_drawn_in_the_colour_asked_for(app) -> None:
    if icon_font() is None:
        pytest.skip("Windows icon font not installed here")
    icon = glyph_icon("upload", "#d97757", 16)
    assert not icon.isNull()
    image: QImage = icon.pixmap(16, 16).toImage()
    painted = [QColor(image.pixelColor(x, y)) for x in range(image.width()) for y in range(image.height())
               if image.pixelColor(x, y).alpha() > 200]
    assert painted, "the glyph left no opaque pixels"
    assert all(abs(c.red() - 0xD9) < 8 and abs(c.green() - 0x77) < 8 and abs(c.blue() - 0x57) < 8 for c in painted)


def test_an_unknown_glyph_or_missing_font_is_just_no_icon(app, monkeypatch) -> None:
    assert glyph_icon("no-such-glyph", "#000000").isNull()
    # Asked before any application exists, the answer is "no font", not a dead process.
    import PySide6.QtGui as gui
    monkeypatch.setattr(gui.QGuiApplication, "instance", staticmethod(lambda: None))
    from vault_v2 import icons as icons_module
    assert icons_module.icon_font() is None
    monkeypatch.undo()
    import vault_v2.icons as icons
    monkeypatch.setattr(icons, "icon_font", lambda: None)
    assert icons.glyph_icon("upload", "#000000").isNull()
