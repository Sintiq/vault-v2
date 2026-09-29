"""Shelves… — see the shelves, add one, remove one of your own."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import cards as cards_mod
from .cards import DEFAULT_SHELVES, CardError, CardStore
from .dialogs import confirm
from .errors import VaultError
from .paths import VaultPaths
from .shelves import add_shelf, remove_shelf


class ShelvesDialog(QDialog):
    def __init__(self, paths: VaultPaths, cards: CardStore, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Shelves")
        self.setMinimumWidth(420)
        self.paths, self.cards = paths, cards
        self.changed = False

        head = QLabel("Shelves")
        head.setObjectName("chatTitle")
        sub = QLabel(
            "Standard shelves stay. Yours are added below — one word, letters and digits; "
            "spaces become underscores. Removing a shelf moves its cards to INBOX, each with a receipt."
        )
        sub.setObjectName("chatSub")
        sub.setWordWrap(True)

        self.list = QListWidget()
        self.entry = QLineEdit()
        self.entry.setPlaceholderText("new shelf, e.g. IMMIGRATION or CAR_LOAN")
        self.entry.returnPressed.connect(self._add)
        add = QPushButton("Add")
        add.setObjectName("sendBtn")
        add.clicked.connect(self._add)
        row = QHBoxLayout()
        row.addWidget(self.entry, 1)
        row.addWidget(add)

        self.remove_btn = QPushButton("Remove selected")
        self.remove_btn.setObjectName("ghostBtn")
        self.remove_btn.clicked.connect(self._remove)
        close = QPushButton("Close")
        close.setObjectName("ghostBtn")
        close.clicked.connect(self.accept)
        bottom = QHBoxLayout()
        bottom.addWidget(self.remove_btn)
        bottom.addStretch(1)
        bottom.addWidget(close)

        self.status = QLabel("")
        self.status.setObjectName("chatSub")
        self.status.setWordWrap(True)

        lay = QVBoxLayout(self)
        lay.setSpacing(8)
        lay.addWidget(head)
        lay.addWidget(sub)
        lay.addWidget(self.list, 1)
        lay.addLayout(row)
        lay.addWidget(self.status)
        lay.addLayout(bottom)
        self._fill()

    def _fill(self) -> None:
        self.list.clear()
        counts: dict[str, int] = {}
        for c in self.cards.all():
            counts[c.shelf] = counts.get(c.shelf, 0) + 1
        for s in cards_mod.SHELVES:
            n = counts.get(s, 0)
            label = f"{s}   ·   {n} card(s)" if n else s
            item = QListWidgetItem(label + ("   (standard)" if s in DEFAULT_SHELVES else ""))
            item.setData(32, s)
            self.list.addItem(item)

    def _add(self) -> None:
        try:
            add_shelf(self.paths, self.entry.text(), self.cards.log)
        except (CardError, VaultError) as exc:
            self.status.setText(str(exc))
            return
        self.entry.clear()
        self.changed = True
        self.status.setText("added")
        self._fill()

    def _remove(self) -> None:
        item = self.list.currentItem()
        if item is None:
            self.status.setText("select a shelf first")
            return
        s = item.data(32)
        if s in DEFAULT_SHELVES:
            self.status.setText(f"{s} is standard and stays")
            return
        n = sum(1 for c in self.cards.all() if c.shelf == s)
        text = f"Remove {s}?" + (f" Its {n} card(s) move to INBOX, with receipts." if n else "")
        if not confirm(self, "Shelves", text):
            return
        try:
            _shelves, moved = remove_shelf(self.paths, self.cards, s)
        except (CardError, VaultError) as exc:
            self.status.setText(str(exc))
            return
        self.changed = True
        self.status.setText(f"removed {s}" + (f", {moved} card(s) moved to INBOX" if moved else ""))
        self._fill()
