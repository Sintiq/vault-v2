"""Health tab — a timeline read out of the owner's own documents."""

from __future__ import annotations

from html import escape

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .agent import Backend
from .date_dialog import DateDialog, date_evidence
from .health import Entry, HealthStore, propose
from .errors import VaultError
from .document_budget import DocumentRequestRefused
from .reader import StagingReader

DISCLAIMER = "a reading of your own documents · not a medical record, not advice · every line quotes its source"


class _Worker(QObject):
    done = Signal(list, list)
    failed = Signal(str)
    refused = Signal(str)

    def __init__(self, reader: StagingReader, backend: Backend | None):
        super().__init__()
        self.reader, self.backend = reader, backend

    def run(self) -> None:
        try:
            entries, notes = propose(self.reader, self.backend)
            self.done.emit(entries, notes)
        except DocumentRequestRefused as exc:
            self.refused.emit(str(exc))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


def _line(e: Entry) -> str:
    when = e.date or "undated"
    evidence = date_evidence(e.due_source, e.flags, e.quote)
    return f"{when}  ·  {e.kind.title()}  ·  {e.label}  ·  {evidence}\n    {e.doc_name} — “{e.quote}”"


class HealthPane(QFrame):
    status = Signal(str)

    def __init__(self, store: HealthStore, reader: StagingReader, get_backend, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("chat")
        self.store, self.reader, self.get_backend = store, reader, get_backend
        self._thread: QThread | None = None
        self._proposals: list[Entry] = []

        title = QLabel("Health")
        title.setObjectName("chatTitle")
        self.sub = QLabel("")
        self.sub.setObjectName("chatSub")
        self.sub.setWordWrap(True)

        self.list = QListWidget()
        self.list.setWordWrap(True)
        self.list.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.list.setResizeMode(QListView.ResizeMode.Adjust)
        self.list.setMinimumHeight(90)

        self.read_btn = QPushButton("Read documents")
        self.read_btn.setObjectName("sendBtn")
        self.read_btn.setToolTip("Read the text documents in Staging and propose timeline entries")
        self.read_btn.clicked.connect(self._read)
        self.edit_date_btn = QPushButton("Edit date…")
        self.edit_date_btn.setObjectName("ghostBtn")
        self.edit_date_btn.setEnabled(False)
        self.edit_date_btn.clicked.connect(self._edit_date)
        self.list.itemSelectionChanged.connect(self._update_date_action)
        self.remove_btn = QPushButton("Remove selected")
        self.remove_btn.setObjectName("ghostBtn")
        self.remove_btn.clicked.connect(self._remove)
        row = QHBoxLayout()
        row.addWidget(self.read_btn)
        row.addStretch(1)
        row.addWidget(self.remove_btn)

        self.prop_label = QLabel("")
        self.prop_label.setObjectName("chatSub")
        self.prop_label.setTextFormat(Qt.TextFormat.PlainText)
        self.prop_label.setWordWrap(True)
        self.prop_list = QListWidget()
        self.prop_list.setWordWrap(True)
        self.prop_list.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.prop_list.setResizeMode(QListView.ResizeMode.Adjust)
        self.prop_list.setMinimumHeight(90)
        self.add_btn = QPushButton("Add ticked")
        self.add_btn.setObjectName("sendBtn")
        self.add_btn.clicked.connect(self._add)
        for w in (self.prop_label, self.prop_list, self.add_btn):
            w.hide()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 10)
        lay.setSpacing(6)
        lay.addWidget(title)
        lay.addWidget(self.sub)
        inner = QWidget()
        il = QVBoxLayout(inner)
        il.setContentsMargins(12, 0, 12, 0)
        il.addWidget(self.list, 1)
        il.addWidget(self.edit_date_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        il.addLayout(row)
        il.addWidget(self.prop_label)
        il.addWidget(self.prop_list, 1)
        il.addWidget(self.add_btn)
        lay.addWidget(inner, 1)
        self.reload()

    # -- timeline -------------------------------------------------------------

    def reload(self) -> None:
        self.list.clear()
        for year, entries in self.store.by_year():
            head = QListWidgetItem(year)
            head.setFlags(Qt.ItemFlag.NoItemFlags)
            font = head.font()
            font.setBold(True)
            head.setFont(font)
            self.list.addItem(head)
            for e in entries:
                li = QListWidgetItem("    " + _line(e))
                li.setToolTip("<qt>" + escape(li.text()).replace("\n", "<br>") + "</qt>")
                li.setData(Qt.ItemDataRole.UserRole, e.id)
                self.list.addItem(li)
        self.sub.setText(self.store.summary() + "\n" + DISCLAIMER)
        self._update_date_action()

    def _selected_id(self) -> str | None:
        items = self.list.selectedItems()
        return items[0].data(Qt.ItemDataRole.UserRole) if len(items) == 1 else None

    def _update_date_action(self) -> None:
        log = self.store.log
        writable = not (log.read_only or log.guard.busy or log.guard.blocked)
        self.edit_date_btn.setEnabled(bool(self._selected_id()) and writable)

    def _edit_date(self) -> None:
        identifier = self._selected_id()
        item = next((item for item in self.store.all() if item.id == identifier), None)
        if item is None:
            return
        dialog = DateDialog(item.date, self, title="Edit date…")
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            try:
                self.store.set_date(item.id, dialog.chosen_date())
            except (VaultError, OSError, ValueError, KeyError) as exc:
                self.status.emit(f"health entry date change not completed: {exc}")
            else:
                self.status.emit("health entry date set by you; recorded in Receipts")
            self.reload()
        finally:
            dialog.deleteLater()

    def _remove(self) -> None:
        for li in self.list.selectedItems():
            eid = li.data(Qt.ItemDataRole.UserRole)
            if eid:
                try:
                    self.store.remove(eid)
                except (VaultError, OSError) as exc:
                    self.status.emit(f"health removal not completed: {exc}")
                    break
        self.reload()

    # -- proposals ------------------------------------------------------------

    def _read(self) -> None:
        if self._thread is not None:
            return
        try:
            backend = self.get_backend()
        except RuntimeError as exc:
            self.prop_label.setText(str(exc))
            self.prop_label.show()
            return
        self.read_btn.setEnabled(False)
        self._clear_proposals()
        self.prop_label.setToolTip("")
        self.prop_label.setText("reading scans… Staging documents" + ("" if backend else " (no agent — keyword baseline)"))
        self.prop_label.show()
        self._thread = QThread(self)
        self._worker = _Worker(self.reader, backend)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.done.connect(self._show)
        self._worker.failed.connect(self._failed)
        self._worker.refused.connect(self._refused)
        self._thread.start()

    def _stop(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(2000)
            self._thread = None
        self.read_btn.setEnabled(True)

    def _failed(self, err: str) -> None:
        self._stop()
        self.prop_label.setText(f"failed: {err}")

    def _clear_proposals(self) -> None:
        self._proposals = []
        self.prop_list.clear()
        self.prop_list.hide()
        self.add_btn.hide()
        self.add_btn.setEnabled(False)

    def _refused(self, message: str) -> None:
        self._stop()
        self._clear_proposals()
        self.prop_label.setText(message)
        self.prop_label.setToolTip("<qt>" + escape(message).replace("\n", "<br>") + "</qt>")
        self.prop_label.show()

    def _show(self, entries: list, notes: list) -> None:
        self._stop()
        self._proposals = [e for e in entries if not self.store.has(e.id)]
        self.prop_list.clear()
        for e in self._proposals:
            li = QListWidgetItem(f"[{e.origin}] " + _line(e))
            li.setToolTip("<qt>" + escape(li.text()).replace("\n", "<br>") + "</qt>")
            li.setFlags(li.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            li.setCheckState(Qt.CheckState.Checked if e.origin == "AGENT" else Qt.CheckState.Unchecked)
            self.prop_list.addItem(li)
        msg = f"{len(self._proposals)} new entry(ies)"
        if notes:
            msg += " · " + " · ".join(notes[:3])
            if len(notes) > 3:
                msg += f" · {len(notes) - 3} more notes (hover to read)"
        self.prop_label.setToolTip("<qt>" + escape("\n".join(notes)).replace("\n", "<br>") + "</qt>")
        self.prop_label.setText(msg + (" — tick what you want and Add." if self._proposals else ""))
        self.prop_list.setVisible(bool(self._proposals))
        self.add_btn.setVisible(bool(self._proposals))
        log = self.store.log
        self.add_btn.setEnabled(bool(self._proposals) and not
                                (log.read_only or log.guard.busy or log.guard.blocked))
        self.status.emit("agent reads recorded in Receipts")

    def _add(self) -> None:
        n = 0
        error = None
        for i, e in enumerate(self._proposals):
            if self.prop_list.item(i).checkState() == Qt.CheckState.Checked:
                try:
                    self.store.add(e)
                except (VaultError, OSError) as exc:
                    error = str(exc)
                    break
                n += 1
        self._proposals = []
        for w in (self.prop_list, self.add_btn):
            w.hide()
        self.prop_label.setText(f"added: {n}" if error is None else f"add stopped after {n} completed additions: {error}")
        self.reload()
