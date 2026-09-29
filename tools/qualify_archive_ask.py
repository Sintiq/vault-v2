"""Synthetic offscreen desktop walkthrough, not a real-model qualification.

Creates its own temporary vault. No owner-root argument, network, or Ollama.
The only model seam is an explicitly scripted in-process Backend. Output must
be a new directory; existing evidence is never overwritten.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QItemSelectionModel, QThread, QTimer, Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tools.synthetic_ocr_files import text_pdf
from vault_v2.agent import Backend, BackendInfo
from vault_v2.ask_dialog import AskDialog
from vault_v2.cards import Card
from vault_v2.errors import VaultError
from vault_v2.main import MainWindow
from vault_v2.pdf_preview import render_pdf_bytes_page
from vault_v2.receipts import sha256_file


NEEDLE = "visaneedle"
FORBIDDEN = "FORBIDDEN_UNMARKED_TEXT"


class ScriptedBackend(Backend):
    """Deterministic fixture oracle; deliberately not evidence of model quality."""

    info = BackendInfo("synthetic", "scripted-no-model", "SCRIPTED synthetic qualification")

    def __init__(self):
        self.calls = []

    def chat(self, system, messages, on_chunk):
        records = json.loads(messages[0]["content"].split("Documents:\n", 1)[1])
        assert FORBIDDEN not in json.dumps(records), "unmarked text reached the model seam"
        selected = [record["id"] for record in records
                    if NEEDLE in " ".join(str(record.get(key, "")) for key in
                                         ("name", "shelf", "topics", "issuer", "excerpt")).casefold()]
        self.calls.append({"records": records, "selected_ids": selected})
        return json.dumps({"documents": selected, "recipient": "PERSONAL",
                           "reason": "Scripted synthetic match; no actual model ran."})


def wait_for(app, predicate, *, seconds=25):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert predicate(), "synthetic desktop action did not finish"
    app.processEvents()


def workers_idle(window):
    return (not any(worker.isRunning() for worker in window.findChildren(QThread))
            and (window.text_queue is None or not window.text_queue.busy))


def select_path(app, pane, path):
    wait_for(app, lambda: pane.proxy.mapFromSource(pane.model.index(str(path))).isValid())
    index = pane.proxy.mapFromSource(pane.model.index(str(path)))
    pane.view.selectionModel().select(index, QItemSelectionModel.SelectionFlag.ClearAndSelect
                                     | QItemSelectionModel.SelectionFlag.Rows)
    pane.view.selectionModel().setCurrentIndex(index, QItemSelectionModel.SelectionFlag.NoUpdate)
    pane.view.setFocus()
    return index


def context_action(app, pane, text):
    errors = []

    def activate():
        menu = QApplication.activePopupWidget()
        try:
            action = next((item for item in menu.actions() if item.text() == text), None)
            assert action is not None and action.isEnabled(), f"unavailable action: {text}"
            action.trigger()
        except BaseException as exc:
            errors.append(exc)
        finally:
            if menu is not None:
                menu.close()

    QTimer.singleShot(0, activate)
    pane.view.customContextMenuRequested.emit(pane.view.visualRect(pane.view.currentIndex()).center())
    app.processEvents()
    if errors:
        raise errors[0]


def open_ask(app, window, inspect):
    """Use the production toolbar entrypoint, including its fresh collector."""
    errors, visited = [], []

    def opened():
        dialog = QApplication.activeModalWidget()
        try:
            assert isinstance(dialog, AskDialog), "Ask toolbar did not open its dialog"
            visited.append(True)
            inspect(dialog)
        except BaseException as exc:
            errors.append(exc)
        finally:
            if dialog is not None:
                dialog.reject()

    wait_for(app, lambda: next(action for action in window.actions() if action.text() == "Ask…").isEnabled())
    QTimer.singleShot(0, opened)
    next(action for action in window.actions() if action.text() == "Ask…").trigger()
    if errors:
        raise errors[0]
    assert visited, "Ask action was not inspected"


def propose(app, dialog):
    dialog.phrase.setText(NEEDLE)
    QTest.mouseClick(dialog.propose_btn, Qt.MouseButton.LeftButton)
    wait_for(app, lambda: dialog.propose_btn.isEnabled())
    assert dialog.result is not None, dialog.status.text()


def screenshot(app, widget, output, name):
    app.processEvents()
    assert widget.grab().save(str(output / name)), f"could not save {name}"
    return name


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    output = args.output_directory.absolute()
    output.mkdir(parents=True, exist_ok=False)
    repository = Path(__file__).resolve().parents[1]
    sources = sorted([*repository.joinpath("vault_v2").glob("*.py"),
                      repository / "tools/synthetic_ocr_files.py", Path(__file__).resolve()])

    def fingerprints():
        return {source.relative_to(repository).as_posix(): sha256_file(source) for source in sources}

    before = fingerprints()
    head = subprocess.check_output(["git", "-C", str(repository), "rev-parse", "HEAD"], text=True).strip()
    report = {"synthetic_only": True, "offscreen": True, "passed": False,
              "repository_head": head, "backend": "scripted in-process fixture oracle; no actual model",
              "model_quality_qualified": False, "native_os_network_attested": False,
              "code_sha256_before": before, "screenshots": []}
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    font = Path("C:/Windows/Fonts/segoeui.ttf")
    if font.is_file():
        QFontDatabase.addApplicationFont(str(font))
        app.setFont(QFont("Segoe UI", 10))
    network_attempts = []

    def deny_network(*_args, **_kwargs):
        network_attempts.append("socket")
        raise AssertionError("qualification forbids Python socket creation")

    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="vault-archive-ask-qa-") as directory:
            root = Path(directory) / "synthetic-vault"
            root.mkdir()
            (root / "vault.json").write_text(json.dumps({"agent_backend": "unsupported"}), encoding="utf-8")
            with patch.object(socket, "socket", deny_network):
                window = MainWindow(root, start_services=False)
                try:
                    paths = window.paths
                    marked = paths.documents / "SYNTHETIC-allowed"
                    marked.mkdir()
                    pdf = text_pdf(marked / "packet.pdf", "Synthetic immigration record with visaneedle evidence.")
                    proof = marked / "proof.txt"
                    proof.write_text("Synthetic permitted visaneedle proof.", encoding="utf-8")
                    private = paths.personal / "private.txt"
                    private.write_text(f"{FORBIDDEN} {NEEDLE}", encoding="utf-8")
                    card_source = paths.personal / "card-only.txt"
                    card_source.write_text("Synthetic neutral bytes with a confirmed owner card.", encoding="utf-8")
                    staged = paths.staging / "visaneedle-note.txt"
                    staged.write_text("Synthetic existing Staging note.", encoding="utf-8")
                    collision = paths.staging / "proof.txt"
                    collision.write_text("Synthetic existing file that must not be overwritten.", encoding="utf-8")
                    originals = [pdf, proof, private, card_source, staged, collision]
                    original_hashes = {path.relative_to(root).as_posix(): sha256_file(path) for path in originals}
                    window.cards.confirm(Card.build(sha256_file(card_source), card_source.name, "TEXT",
                                                     shelf="INBOX", topics=[NEEDLE], issuer="Synthetic",
                                                     recipients=["PERSONAL"], origin="HUMAN"), card_source)
                    backend = ScriptedBackend()
                    window.chat.backend = backend
                    window.show()
                    pane = window.panes["documents"]
                    select_path(app, pane, marked)
                    context_action(app, pane, "Allow agent to read text here")
                    wait_for(app, lambda: workers_idle(window))
                    assert "Agent text" in select_path(app, pane, marked).data()
                    extraction = [row for row in window.ops.log.tail(1000) if row["op"] == "text_extracted"]
                    assert len(extraction) == 2, extraction
                    assert {row["extra"]["engine"] for row in extraction} == {"pdfium+pypdf", "utf8-replacement"}
                    wait_for(app, lambda: not window.guard_status.text())
                    report["screenshots"].append(screenshot(app, window, output, "01-marked-folder.png"))
                    preview = render_pdf_bytes_page(pdf.read_bytes(), 0)
                    (output / "synthetic-native-pdf.png").write_bytes(preview.png)
                    report["fixture_preview"] = "synthetic-native-pdf.png"
                    expected = {pdf, proof, card_source, staged}

                    def first_ask(dialog):
                        propose(app, dialog)
                        chosen = {doc.path for doc in dialog.docs if doc.id in dialog.result.proposed_ids}
                        assert chosen == expected, chosen
                        assert not dialog.export_btn.isEnabled() and dialog.copy_btn.isEnabled()
                        report["first_ask"] = {"selected": sorted(path.relative_to(root).as_posix() for path in chosen),
                                               "notes": list(dialog.result.notes),
                                               "unmarked_excerpt_empty": next(doc for doc in dialog.docs if doc.path == private).excerpt == ""}
                        report["screenshots"].append(screenshot(app, dialog, output, "02-ask-results.png"))
                        QTest.mouseClick(dialog.copy_btn, Qt.MouseButton.LeftButton)
                        assert dialog.export_btn.isEnabled() and not dialog.copy_btn.isEnabled(), dialog.status.text()
                        assert (paths.staging / "proof (1).txt").read_bytes() == proof.read_bytes()
                        assert sha256_file(collision) == original_hashes[collision.relative_to(root).as_posix()]
                        selected_ids = set(dialog.result.proposed_ids)
                        copies = [doc.path for doc in dialog.docs if doc.id in selected_ids]
                        assert all(path.is_relative_to(paths.staging) for path in copies)
                        report["copy_to_staging"] = {"selected_staging_paths": sorted(path.relative_to(root).as_posix() for path in copies),
                                                     "status": dialog.status.text(), "export_enabled": True,
                                                     "collision_preserved": True}
                        report["screenshots"].append(screenshot(app, dialog, output, "03-copied-to-staging.png"))

                    open_ask(app, window, first_ask)
                    try:
                        window.gate.prepare([pdf], Path(directory) / "never-exported", "folder")
                    except VaultError as exc:
                        assert "Staging" in str(exc), str(exc)
                        report["nonstaging_gate_refusal"] = str(exc)
                    else:
                        raise AssertionError("export gate accepted an archive source")
                    assert not (Path(directory) / "never-exported").exists()
                    wait_for(app, lambda: pane.write_enabled)
                    select_path(app, pane, marked)
                    context_action(app, pane, "Stop reading text here")
                    assert "Agent text" not in select_path(app, pane, marked).data()

                    def revoked_ask(dialog):
                        propose(app, dialog)
                        source_docs = [doc for doc in dialog.docs if doc.path in {pdf, proof}]
                        assert len(source_docs) == 2
                        assert all(doc.excerpt == "" and doc.total_chars is None for doc in source_docs)
                        assert not ({doc.id for doc in source_docs} & set(dialog.result.proposed_ids))
                        assert next(doc for doc in dialog.docs if doc.path == private).excerpt == ""
                        report["revoked_ask"] = {"original_marked_excerpts_empty": True,
                                                 "original_marked_documents_not_selected": True,
                                                 "staging_copies_retained": True,
                                                 "notes": list(dialog.result.notes)}
                        report["screenshots"].append(screenshot(app, dialog, output, "04-revoked-next-ask.png"))

                    open_ask(app, window, revoked_ask)
                    assert {path.relative_to(root).as_posix(): sha256_file(path) for path in originals} == original_hashes
                    receipts = window.ops.log.tail(1000)
                    counts = Counter(row["op"] for row in receipts)
                    assert counts["agent_scope_granted"] == counts["agent_scope_revoked"] == 1
                    assert counts["copy"] == 3 and counts["export_done"] == 0
                    assert len(backend.calls) == 2
                    assert not network_attempts
                    report.update(original_files_sha256=original_hashes, originals_unchanged=True,
                                  receipts_by_operation=dict(counts), receipt_chain_entries=window.ops.log.verify(),
                                  scripted_backend_calls=backend.calls, python_socket_attempts=0,
                                  python_network_blocked=True, native_pdf_extraction_without_ocr=True,
                                  passed=True)
                finally:
                    wait_for(app, lambda: workers_idle(window))
                    window.close()
                    app.processEvents()
                    assert not window.isVisible(), "synthetic window did not release its workers"
    except Exception as exc:
        report.update(passed=False, error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        after = fingerprints()
        report.update(code_sha256_after=after, code_unchanged=after == before,
                      elapsed_seconds=round(time.monotonic() - started, 3))
        if before != after:
            report.update(passed=False, error="source changed during qualification")
        report["evidence_sha256"] = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                     for path in sorted(output.glob("*.png"))}
        (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"passed": report["passed"], "output_directory": str(output),
                          "elapsed_seconds": report["elapsed_seconds"]}, ensure_ascii=False), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
