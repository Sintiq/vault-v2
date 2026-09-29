"""Owner date chooser and plain-language date provenance for desktop rows."""
from __future__ import annotations

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QCheckBox, QDateEdit, QDialog, QDialogButtonBox, QLabel, QVBoxLayout, QWidget,
)

from .dates import find_dates


_FLAG_LABELS = {
    "date_not_in_quote": "date not in quote",
    "date_from_quote": "date from quote",
    "ambiguous_date": "ambiguous date (US month/day order)",
    "several_dates": "several dates in quote",
    "relative_deadline": "relative deadline in quote",
    "not_verified": "not verified",
}


def date_evidence(source: str, flags: tuple[str, ...] | list[str], quote: str = "") -> str:
    """Keep owner attribution visible even when the owner selected no date."""
    origin = {"owner": "set by you", "quote": "date from quote", "none": "no verified date",
              "device": "measured by the device named on the line"}
    parts = [origin.get(source, "not verified")]
    for flag in flags:
        if flag == "relative_deadline" and find_dates(quote, allow_partial=True):
            parts.append("in the quote there is a date and a relative term — check and set the date yourself")
        else:
            parts.append(_FLAG_LABELS.get(flag, flag.replace("_", " ")))
    return " · ".join(dict.fromkeys(parts))


class DateDialog(QDialog):
    def __init__(self, value: str | None, parent: QWidget | None = None,
                 *, title: str = "Edit due date…"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(300)
        layout = QVBoxLayout(self)
        explanation = QLabel("Set by you. Your choice takes priority over the document quote.")
        explanation.setWordWrap(True)
        explanation.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(explanation)
        current = QLabel(f"Current date: {value or 'no date'}")
        current.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(current)
        selected = QDate.fromString(value or "", "yyyy-MM-dd")
        self.date_edit = QDateEdit(selected if selected.isValid() else QDate.currentDate())
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setMaximumDate(QDate(9999, 12, 31))
        layout.addWidget(self.date_edit)
        self.no_date = QCheckBox("No date")
        representable = (selected.isValid() and
                         self.date_edit.minimumDate() <= selected <= self.date_edit.maximumDate())
        self.no_date.setChecked(not representable)
        self.date_edit.setEnabled(not self.no_date.isChecked())
        self.no_date.toggled.connect(lambda checked: self.date_edit.setEnabled(not checked))
        layout.addWidget(self.no_date)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def chosen_date(self) -> str | None:
        return None if self.no_date.isChecked() else self.date_edit.date().toString("yyyy-MM-dd")
