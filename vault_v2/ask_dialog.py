"""Ask dialog — a phrase in, a proposed set + recipient out, then the gate.

The owner ticks the final set and picks the recipient; "Export selected…"
hands exactly those Staging files to the Gatekeeper's export dialog.
"""

from __future__ import annotations

from datetime import datetime
from dataclasses import replace
from html import escape
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .agent import Backend
from .ask import AskDoc, AskResult, ask, copy_to_staging, default_recipient
from .cards import RECIPIENTS
from .export_dialog import ExportDialog
from .gatekeeper import Gatekeeper
from .document_budget import DocumentRequestRefused
from .errors import VaultError


class _Worker(QObject):
    done = Signal(object)
    failed = Signal(str)
    refused = Signal(str)

    def __init__(self, phrase: str, docs: list[AskDoc] | Callable[[], list[AskDoc]], backend: Backend | None):
        super().__init__()
        self.phrase, self.docs, self.backend = phrase, docs, backend

    def run(self) -> None:
        try:
            docs = self.docs() if callable(self.docs) else self.docs
            self.done.emit(ask(self.phrase, docs, self.backend))
        except DocumentRequestRefused as exc:
            self.refused.emit(str(exc))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class AskDialog(QDialog):
    def __init__(self, gate: Gatekeeper, docs: list[AskDoc] | Callable[[], list[AskDoc]], backend: Backend | None, parent: QWidget | None = None):
        super().__init__(parent)
        self.gate, self.backend = gate, backend
        self._source = docs
        self.docs = [] if callable(docs) else docs
        self.result: AskResult | None = None
        self.exported = None
        self._thread: QThread | None = None
        self._discarded = False
        self.setWindowTitle("Ask — collect documents across the vault")
        self.resize(900, 560)

        self.head = QLabel("What do you need?")
        self.head.setObjectName("chatTitle")
        self.head.setTextFormat(Qt.TextFormat.PlainText)
        self.sub = QLabel("Each Propose refreshes all vault panes · local text excerpts come only from Staging and permitted folders")
        self.sub.setObjectName("paneSub")

        self.phrase = QLineEdit()
        self.phrase.setPlaceholderText("e.g. everything about my migraine for the neurologist")
        self.phrase.returnPressed.connect(self._propose)
        self.propose_btn = QPushButton("Propose")
        self.propose_btn.setObjectName("sendBtn")
        self.propose_btn.clicked.connect(self._propose)
        top = QHBoxLayout()
        top.addWidget(self.phrase, 1)
        top.addWidget(self.propose_btn)

        self.list = QListWidget()
        self.list.itemChanged.connect(self._on_check)
        self.status = QLabel("")
        self.status.setObjectName("paneSub")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.model_note = QLabel("")
        self.model_note.setObjectName("paneSub")
        self.model_note.setTextFormat(Qt.TextFormat.PlainText)
        self.model_note.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.model_note.hide()

        self.recipient = QComboBox()
        self.recipient.addItems(RECIPIENTS)
        self.export_btn = QPushButton("Export selected…")
        self.export_btn.setObjectName("sendBtn")
        self.export_btn.clicked.connect(self._export)
        self.export_btn.setEnabled(False)
        self.copy_btn = QPushButton("Copy to Staging")
        self.copy_btn.clicked.connect(self._copy_to_staging)
        self.copy_btn.setEnabled(False)
        close = QPushButton("Close")
        close.setObjectName("ghostBtn")
        close.clicked.connect(self.accept)
        bottom = QHBoxLayout()
        bottom.addWidget(QLabel("Recipient:"))
        bottom.addWidget(self.recipient)
        bottom.addStretch(1)
        bottom.addWidget(self.copy_btn)
        bottom.addWidget(self.export_btn)
        bottom.addWidget(close)

        lay = QVBoxLayout(self)
        lay.addWidget(self.head)
        lay.addWidget(self.sub)
        lay.addLayout(top)
        lay.addWidget(self.list, 1)
        lay.addWidget(self.status)
        lay.addWidget(self.model_note)
        lay.addLayout(bottom)
        self._fill_list(set())

    # -- list -----------------------------------------------------------------

    def _fill_list(self, checked: set[str], marks: dict[str, str] | None = None) -> None:
        self.list.clear()
        marks = marks or {}
        for d in self.docs:
            card = d.card
            desc = f"{d.pane}/{d.rel or d.path.name}  —  " + (f"{card.shelf} / {', '.join(card.topics) or 'UNTAGGED'} / {card.issuer}" + (f" / {card.year}" if card.year else "") if card else "no confirmed card (run Sort first)")
            if d.id in marks:
                desc += f"   [{marks[d.id]}]"
            li = QListWidgetItem(desc)
            li.setFlags(li.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            li.setCheckState(Qt.CheckState.Checked if d.id in checked else Qt.CheckState.Unchecked)
            li.setData(Qt.ItemDataRole.UserRole, d.id)
            self.list.addItem(li)
        self._on_check()

    def _checked_ids(self) -> list[str]:
        out = []
        for i in range(self.list.count()):
            li = self.list.item(i)
            if li.checkState() == Qt.CheckState.Checked:
                out.append(li.data(Qt.ItemDataRole.UserRole))
        return out

    def _on_check(self, *_a) -> None:
        ids = set(self._checked_ids())
        selected = [d for d in self.docs if d.id in ids]
        log = self.gate.ops.log
        ready = (self.list.isEnabled() and self._thread is None and not log.read_only
                 and not log.guard.busy and not log.guard.blocked)
        archived = any(not self._in_staging(d) for d in selected)
        self.export_btn.setEnabled(ready and bool(selected) and not archived)
        self.export_btn.setToolTip("Copy archive documents to Staging first" if archived else "")
        self.copy_btn.setEnabled(ready and archived)

    def _in_staging(self, doc: AskDoc) -> bool:
        return doc.path.absolute().is_relative_to(self.gate.ops.paths.staging.absolute())

    # -- propose --------------------------------------------------------------

    def _propose(self) -> None:
        phrase = self.phrase.text().strip()
        if not phrase or self._thread is not None or self._discarded:
            return
        self.propose_btn.setEnabled(False)
        self.result = None
        self.model_note.clear()
        self.model_note.setToolTip("")
        self.model_note.hide()
        self.list.setEnabled(False)
        self._fill_list(set())
        self.export_btn.setEnabled(False)
        self.status.setToolTip("")
        self.status.setText("asking the agent…" if self.backend else "no agent — baseline keywords")
        self._thread = QThread(self)
        self._worker = _Worker(phrase, self._source, self.backend)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.done.connect(self._show)
        self._worker.failed.connect(self._failed)
        self._worker.refused.connect(self._refused)
        self._thread.start()

    def _stop_thread(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(2000)
            self._thread = None
        self.propose_btn.setEnabled(True)

    def _failed(self, err: str) -> None:
        self._refused(f"Request failed: {err}. No previous result was reused; try Propose again.")

    def _refused(self, message: str) -> None:
        self._stop_thread()
        if self._discarded:
            return
        self.result = None
        self.model_note.clear()
        self.model_note.setToolTip("")
        self.model_note.hide()
        self.list.setEnabled(False)
        self._fill_list(set())
        self.export_btn.setEnabled(False)
        self.head.setText("Request refused")
        self.status.setText(message)
        self.status.setToolTip("<qt>" + escape(message).replace("\n", "<br>") + "</qt>")

    def _show(self, res: AskResult) -> None:
        self._stop_thread()
        if self._discarded:
            return
        self.result = res
        rank = {did: index for index, did in enumerate(res.proposed_ids)}
        self.docs = sorted(res.docs, key=lambda doc: rank.get(doc.id, len(rank)))
        self.list.setEnabled(True)
        marks = {i: "added by agent" for i in res.added_by_agent}
        marks.update({i: "omitted by agent" for i in res.omitted_by_agent})
        marks.update({i: "not sent to model — budget" for i in res.not_seen_by_agent})
        self._fill_list(set(res.proposed_ids), marks)
        rec = res.agent_recipient or default_recipient(list(res.docs), list(res.proposed_ids))
        self.recipient.setCurrentText(rec)
        parts = [f"proposed: {len(res.proposed_ids)}", f"baseline: {len(res.baseline_ids)}"]
        if res.agent_ids is not None:
            parts.append(f"agent: {len(res.agent_ids)}")
        model_note = "model's note, unverified: " + res.agent_reason if res.agent_reason else ""
        self.model_note.setText(model_note)
        self.model_note.setToolTip("<qt>" + escape(model_note) + "</qt>" if model_note else "")
        self.model_note.setVisible(bool(model_note))
        if res.error:
            parts.append(res.error)
        notes = res.notes
        parts.extend(notes[:3])
        if len(notes) > 3:
            parts.append(f"{len(notes) - 3} more notes (hover to read)")
        self.status.setToolTip("<qt>" + escape("\n".join(notes)).replace("\n", "<br>") + "</qt>")
        self.status.setText(" · ".join(parts) + " — tick the final set; copy archive files to Staging before Export.")
        self.head.setText(f"“{res.phrase}”")

    # -- export through the gate ---------------------------------------------

    def _copy_to_staging(self) -> None:
        ops = self.gate.ops
        if (self._discarded or not self.list.isEnabled() or self._thread is not None
                or ops.log.read_only or ops.log.guard.busy or ops.log.guard.blocked):
            return
        ids = set(self._checked_ids())
        replacements, failures = {}, []
        for doc in self.docs:
            if doc.id not in ids or self._in_staging(doc):
                continue
            try:
                for copied in copy_to_staging(ops, [doc]):
                    replacements[doc.id] = replace(doc, path=copied.dst, pane="staging",
                                                    rel=copied.dst.relative_to(ops.paths.staging).as_posix())
            except (VaultError, OSError) as exc:
                failures.append(f"{doc.pane}/{doc.rel or doc.path.name}: {exc}")
        self.docs = [replacements.get(doc.id, doc) for doc in self.docs]
        self._fill_list(ids)
        self.status.setText(f"Copied to Staging: {len(replacements)}; failed: {len(failures)}. "
                            + ("Uncopied archive selections cannot be exported." if failures else
                               "Only the selected Staging files will go through the export gate."))
        self.status.setToolTip("<qt>" + escape("\n".join(failures)).replace("\n", "<br>") + "</qt>")

    def _export(self) -> None:
        log = self.gate.ops.log
        if (self._discarded or not self.list.isEnabled() or self._thread is not None
                or log.read_only or log.guard.busy or log.guard.blocked):
            return
        ids = set(self._checked_ids())
        selected = [d for d in self.docs if d.id in ids]
        if any(not self._in_staging(d) for d in selected):
            return
        paths = [d.path for d in selected]
        if not paths:
            return
        rec = self.recipient.currentText()
        dlg = ExportDialog(self.gate, paths, self)
        dlg.dest.setText(str(Path.home() / "Desktop" / f"Vault pack {rec} {datetime.now().strftime('%Y-%m-%d %H-%M')}"))
        dlg.exec()
        if dlg.result is not None:
            self.exported = dlg.result
            self.status.setText(f"exported {len(dlg.result.written)} file(s) for {rec} → {dlg.result.destination}")

    def closeEvent(self, ev) -> None:  # noqa: N802
        # Keep the hidden dialog and child thread alive until completion;
        # MainWindow retains its lease while this model call is outstanding.
        self._discarded = True
        super().closeEvent(ev)

    def accept(self) -> None:
        self._discarded = True
        super().accept()

    def reject(self) -> None:
        self._discarded = True
        super().reject()
