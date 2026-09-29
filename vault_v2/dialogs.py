"""Small Russian-labelled dialogs (explicit button labels, consistent across platforms)."""

from __future__ import annotations

from PySide6.QtWidgets import QMessageBox, QWidget


def confirm(parent: QWidget | None, title: str, text: str, yes: str = "Yes", no: str = "No") -> bool:
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    box.setIcon(QMessageBox.Icon.Question)
    b_yes = box.addButton(yes, QMessageBox.ButtonRole.YesRole)
    box.addButton(no, QMessageBox.ButtonRole.NoRole)
    box.setDefaultButton(b_yes)
    box.exec()
    return box.clickedButton() is b_yes
