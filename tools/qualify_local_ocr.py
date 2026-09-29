"""Explicit synthetic OCR + warm local model gate. No owner root accepted."""
import json
from pathlib import Path
import socket
import tempfile
import time
from unittest.mock import patch

from vault_v2.agent import pick_backend, warm_ollama
from vault_v2.cards import CardStore
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.reader import StagingReader
from vault_v2.tasks import collect_docs, agent_tasks
from vault_v2.text_extract import EXTRACTION_PROFILE
from tools.synthetic_ocr_files import ENGLISH, RUSSIAN, scan_file, text_pdf
from tools.synthetic_viewer_files import form_pdf


def main():
    model = "llama3.1:8b"
    report = {"synthetic_only": True, "profile": EXTRACTION_PROFILE,
              "model": model, "temperature": 0, "seed": 42, "cases": []}
    with tempfile.TemporaryDirectory(prefix="vault-ocr-qualification-") as directory:
        ops = VaultOps(VaultPaths(Path(directory)))
        cards = CardStore(ops.paths.root / ".cards", ops.log)
        text_pdf(ops.paths.staging / "native.pdf")
        scan_file(ops.paths.staging / "scan.pdf")
        form_pdf(ops.paths.staging / "form.pdf", value=ENGLISH, with_appearance=False,
                 encrypted=True, encryption_algorithm="AES-256")
        scan_file(ops.paths.staging / "photo.png")
        scan_file(ops.paths.staging / "russian.png", RUSSIAN)
        reader = StagingReader(ops, cards)
        notes = []
        started = time.monotonic()
        with patch.object(socket, "socket", side_effect=AssertionError("unexpected extraction network")):
            docs = collect_docs(reader, notes)
        report.update(extraction_seconds=round(time.monotonic() - started, 3), extraction_notes=notes,
                      python_network_blocked_during_extraction=True,
                      native_os_network_attested=False)
        if len(docs) != 5:
            report.update(passed=False, reason="five synthetic documents were not extracted")
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 1
        backend = pick_backend({"ollama_model": model})
        report["warmup_succeeded"] = warm_ollama(model)
        if not report["warmup_succeeded"]:
            report["passed"] = False
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 1
        for doc in docs:
            started = time.monotonic()
            tasks, dropped = agent_tasks(backend, [doc])
            accepted = [task for task in tasks if task.origin == "AGENT"
                        and task.due == "2026-10-15" and task.quote in doc.text]
            expected = RUSSIAN if doc.name == "russian.png" else ENGLISH
            row = {"name": doc.name, "source_sha256": doc.sha256,
                   "phrase_exact": expected in doc.text, "accepted": len(accepted),
                   "due": [task.due for task in accepted], "dropped": dropped,
                   "quotes_literal": all(task.quote in doc.text for task in tasks),
                   "seconds": round(time.monotonic() - started, 3)}
            row["passed"] = row["phrase_exact"] and bool(accepted) and row["quotes_literal"]
            report["cases"].append(row)
        report["passed"] = all(row["passed"] for row in report["cases"])
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
