"""Sort dialog — the agent proposes cards for Staging, the owner confirms.

Editable columns: shelf (combo), topics (comma list), issuer, year,
recipients (comma list). Origin shows who proposed the row; a flag column
says where the agent and the baseline disagree or where the agent's answer
was rejected. Only "Confirm" writes — one receipt per card.
"""

from __future__ import annotations

from dataclasses import replace
from html import escape
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .agent import Backend
from . import cards
from .cards import RECIPIENTS, Card, CardError, CardStore
from .sorting import Proposal, SortInput, propose
from .errors import VaultError
from .document_budget import DocumentRequestRefused
from .proposals import ProposalConflict

COLS = ("File", "Shelf", "Topics", "Issuer", "Year", "Recipients", "Origin", "Notes")


class _Worker(QObject):
    done = Signal(list)
    failed = Signal(str)
    refused = Signal(str)
    collected = Signal(list)

    def __init__(self, docs: list[SortInput] | Callable[[], list[SortInput]], backend: Backend | None):
        super().__init__()
        self.docs, self.backend = docs, backend

    def run(self) -> None:
        try:
            docs = self.docs() if callable(self.docs) else self.docs
            self.collected.emit(docs)
            self.done.emit(propose(docs, self.backend))
        except DocumentRequestRefused as exc:
            self.refused.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - surface anything, never hang the dialog
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class SortDialog(QDialog):
    def __init__(self, store: CardStore, docs: list[SortInput] | Callable[[], list[SortInput]], backend: Backend | None, parent: QWidget | None = None):
        super().__init__(parent)
        self.store, self.docs, self.backend = store, [] if callable(docs) else docs, backend
        self._collection = docs
        self.proposals: list[Proposal] = []
        self.confirmed = 0
        self._generation: int | None = None
        self._consumed_rows: set[int] = set()
        self._thread: QThread | None = None
        self._discarded = False
        self.setWindowTitle("Sort Staging")
        self.resize(1180, 560)

        self.head = QLabel("reading scans…" if callable(docs) else ("Asking the agent…" if backend else "No agent — baseline rules only"))
        self.head.setObjectName("chatTitle")
        self.sub = QLabel("Reading Staging documents locally…" if callable(docs) else
                          f"{len(docs)} document(s) in Staging · the agent sees names, sizes and text excerpts of these files only")
        self.sub.setObjectName("paneSub")
        self.sub.setTextFormat(Qt.TextFormat.PlainText)
        self.sub.setWordWrap(True)

        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(list(COLS))
        hdr = self.table.horizontalHeader()
        for i in range(len(COLS)):
            hdr.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)

        self.confirm_all = QPushButton("Confirm all")
        self.confirm_all.setObjectName("sendBtn")
        self.confirm_all.clicked.connect(lambda: self._confirm(all_rows=True))
        self.confirm_sel = QPushButton("Confirm selected")
        self.confirm_sel.setObjectName("ghostBtn")
        self.confirm_sel.clicked.connect(lambda: self._confirm(all_rows=False))
        close = QPushButton("Close")
        close.setObjectName("ghostBtn")
        close.clicked.connect(self.accept)
        for b in (self.confirm_all, self.confirm_sel):
            b.setEnabled(False)

        row = QHBoxLayout()
        row.addWidget(self.confirm_all)
        row.addWidget(self.confirm_sel)
        row.addStretch(1)
        row.addWidget(close)

        lay = QVBoxLayout(self)
        lay.addWidget(self.head)
        lay.addWidget(self.sub)
        lay.addWidget(self.table, 1)
        lay.addLayout(row)

        self._start()

    # -- proposal -------------------------------------------------------------

    def _start(self) -> None:
        self._thread = QThread(self)
        self._worker = _Worker(self._collection, self.backend)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.done.connect(self._fill)
        self._worker.collected.connect(self._collected)
        self._worker.failed.connect(self._failed)
        self._worker.refused.connect(self._refused)
        self._thread.start()

    def _collected(self, docs: list[SortInput]) -> None:
        if self._discarded:
            return
        self.docs = docs
        self.head.setText("Asking the agent…" if self.backend else "No agent — baseline rules only")
        notes = [f"{d.name}: {warning}" for d in docs for warning in d.warnings]
        summary = f"{len(docs)} document(s) in Staging · bounded text read locally"
        if notes:
            summary += " · " + " · ".join(notes[:3])
            if len(notes) > 3:
                summary += f" · {len(notes) - 3} more notes (hover to read)"
        self.sub.setText(summary)
        self.sub.setToolTip("<qt>" + escape("\n".join(notes)).replace("\n", "<br>") + "</qt>")

    def _stop_thread(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(2000)
            self._thread = None

    def _failed(self, err: str) -> None:
        self._stop_thread()
        if self._discarded:
            return
        self.head.setText("Agent failed — baseline rules only")
        self.sub.setText(err)
        self._fill(propose(self.docs, None))

    def _refused(self, message: str) -> None:
        self._stop_thread()
        if self._discarded:
            return
        self.proposals = []
        self._generation = None
        self._consumed_rows.clear()
        self.table.setRowCount(0)
        for button in (self.confirm_all, self.confirm_sel):
            button.setEnabled(False)
        self.head.setText("Request refused")
        self.sub.setText(message)
        self.sub.setToolTip("<qt>" + escape(message).replace("\n", "<br>") + "</qt>")

    def _fill(self, proposals: list[Proposal]) -> None:
        self._stop_thread()
        if self._discarded:
            return
        self.proposals = proposals
        try:
            with self.store.log.write("publish desktop sort"), self.store.mutation_lock:
                for p in proposals:  # visible in the panes as "SHELF ?" until confirmed
                    self.store.propose(p.card, p.doc.path)
                self._generation = self.store.generation
                self._consumed_rows.clear()
        except (VaultError, OSError) as exc:
            self.head.setText("Draft publication stopped")
            self.sub.setText(str(exc))
            self.proposals = []
            return
        self.table.setRowCount(len(proposals))
        for r, p in enumerate(proposals):
            c = p.card
            existing = self.store.get(c.sha256)
            self._set(r, 0, c.name, editable=False)
            combo = QComboBox()
            combo.addItems(cards.SHELVES)
            combo.setCurrentText(c.shelf)
            self.table.setCellWidget(r, 1, combo)
            self._set(r, 2, ", ".join(c.topics))
            self._set(r, 3, c.issuer)
            self._set(r, 4, "" if c.year is None else str(c.year))
            self._set(r, 5, ", ".join(c.recipients))
            self._set(r, 6, c.origin + (" · already confirmed" if existing and existing.confirmed else ""), editable=False)
            notes = list(p.flags)
            if p.differs and p.agent is not None:
                b = p.baseline
                notes.append(f"baseline: {b.shelf} / {', '.join(b.topics)} / {', '.join(b.recipients)}")
            if c.reason:
                notes.append(c.reason)
            self._set(r, 7, " · ".join(notes), editable=False)
        agent_n = sum(1 for p in proposals if p.agent is not None)
        self.head.setText(f"{len(proposals)} proposal(s) · agent: {agent_n} · baseline: {len(proposals) - agent_n}")
        for b in (self.confirm_all, self.confirm_sel):
            b.setEnabled(bool(proposals))

    def _set(self, r: int, col: int, text: str, editable: bool = True) -> None:
        it = QTableWidgetItem(text)
        if not editable:
            it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.table.setItem(r, col, it)

    # -- confirm --------------------------------------------------------------

    def _row_card(self, r: int) -> Card:
        p = self.proposals[r]
        base = p.card
        shelf = self.table.cellWidget(r, 1).currentText()
        topics = self.table.item(r, 2).text()
        issuer = self.table.item(r, 3).text()
        year = self.table.item(r, 4).text()
        recs = [x.strip() for x in self.table.item(r, 5).text().split(",") if x.strip()]
        edited = (shelf, topics, issuer, year, recs) != (
            base.shelf, ", ".join(base.topics), base.issuer, "" if base.year is None else str(base.year), list(base.recipients))
        return Card.build(
            base.sha256, base.name, base.kind, shelf=shelf, topics=topics, issuer=issuer, year=year or None,
            recipients=recs, origin="HUMAN" if edited else base.origin, reason=base.reason,
        )

    def _confirm(self, all_rows: bool) -> None:
        if self._discarded:
            return
        rows = ([r for r in range(len(self.proposals)) if r not in self._consumed_rows]
                if all_rows else sorted({i.row() for i in self.table.selectedIndexes()}))
        n, failed = 0, []
        try:
            with self.store.log.write("confirm desktop sort"), self.store.mutation_lock:
                if self._generation != self.store.generation:
                    raise ProposalConflict()
                if any(r in self._consumed_rows for r in rows):
                    raise ProposalConflict()
                for r in rows:
                    try:
                        card = self._row_card(r)
                        # Burn before the store attempt: a failed write may
                        # already have applied, and is not safely replayable.
                        self._consumed_rows.add(r)
                        self.store.confirm(card, self.proposals[r].doc.path)
                        self._set(r, 6, card.origin + " · confirmed", editable=False)
                        n += 1
                    except CardError as exc:
                        failed.append(f"{self.proposals[r].doc.name}: {exc}")
                    except (VaultError, OSError) as exc:
                        failed.append(str(exc))
                        break
                self._generation = self.store.generation
        except ProposalConflict as exc:
            self.sub.setText(str(exc))
            QMessageBox.warning(self, "Sort", str(exc))
            return
        except (VaultError, OSError) as exc:
            failed.append(str(exc))
        if not rows and not failed:
            QMessageBox.information(self, "Sort", "Select rows to confirm, or use Confirm all.")
            return
        self.confirmed += n
        for button in (self.confirm_all, self.confirm_sel):
            button.setEnabled(len(self._consumed_rows) < len(self.proposals))
        if failed:
            QMessageBox.warning(self, "Sort", "Not confirmed:\n" + "\n".join(failed))
        self.sub.setText(f"confirmed: {self.confirmed} · receipts recorded")

    def closeEvent(self, ev) -> None:  # noqa: N802
        # Hide immediately, but retain the child thread until its completion
        # callback. MainWindow still sees it and retains the runtime lease.
        self._discarded = True
        super().closeEvent(ev)

    def accept(self) -> None:
        self._discarded = True
        super().accept()

    def reject(self) -> None:
        self._discarded = True
        super().reject()
