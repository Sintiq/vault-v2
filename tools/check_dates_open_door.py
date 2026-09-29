"""Explicit local-only qualification of document extraction with cloud chat open.

Uses a fresh synthetic Vault, real ChatPane selection and local HTTP transport.
The Claude CLI is never invoked. One warm-up request precedes one scored answer.
Raw synthetic evidence is written exclusively to the user-named new output file.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import threading
import time
import urllib.request
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from tools.pick_local_model import DOC, PROMPT, score
from vault_v2.agent import document_chat
from vault_v2.agent_door import AgentDoor
from vault_v2.chat import ChatPane
from vault_v2.receipts import ReceiptLog
from vault_v2.tasks import TASKS_SYSTEM, TaskDoc, agent_tasks


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-local-synthetic", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("output already exists; preserve previous evidence")
    report = {"timestamp": datetime.now(timezone.utc).isoformat(), "synthetic_only": True,
              "route": "local document job while Claude chat door is open", "pass": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Prove evidence can be saved BEFORE any warm-up or scored model request.
    with args.output.open("x+", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.flush()
        try:
            qualify(report)
        finally:
            stream.seek(0)
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.truncate()
            stream.flush()
            print(json.dumps({key: value for key, value in report.items() if key != "raw_reply"}, ensure_ascii=False))
    return 0 if report["pass"] else 1


def qualify(report):
    warmed = threading.Event()
    captured = []
    original_open = urllib.request.OpenerDirector.open

    class ObservedWarmup:
        def __init__(self, response):
            self.response = response

        def __enter__(self):
            self.response.__enter__()
            return self

        def __exit__(self, *exc):
            return self.response.__exit__(*exc)

        def read(self, *params):
            reply = self.response.read(*params)
            warmed.set()
            return reply

    def observe(opener, request, *params, **kwargs):
        url = request.full_url if hasattr(request, "full_url") else request
        if url not in ("http://127.0.0.1:11434/api/tags", "http://127.0.0.1:11434/api/chat"):
            raise RuntimeError("qualification refuses non-local transport")
        payload = json.loads(request.data) if getattr(request, "data", None) else None
        if payload is not None and payload.get("model") != "llama3.1:8b":
            raise RuntimeError("qualification refuses another model")
        if payload is not None:
            captured.append({key: payload.get(key) for key in ("model", "options", "format", "stream")})
        response = original_open(opener, request, *params, **kwargs)
        return ObservedWarmup(response) if payload is not None and payload.get("stream") is False else response

    app = QApplication.instance() or QApplication([])
    candidate_pass = False
    report["requests"] = captured
    with tempfile.TemporaryDirectory(prefix="vault-dates-door-") as directory:
        root = Path(directory)
        staging = root / "staging"
        staging.mkdir()
        (staging / "Prescription.txt").write_text(DOC, encoding="utf-8")
        log = ReceiptLog(root / ".receipts")
        with patch.object(urllib.request.OpenerDirector, "open", observe):
            pane = ChatPane(staging, {"ollama_model": "llama3.1:8b"}, door=AgentDoor(log, {}))
            try:
                if not warmed.wait(60):
                    raise RuntimeError("local warm-up not confirmed; no scored request sent")
                pane.mode.setCurrentIndex(pane.mode.findData("claude"))
                backend = pane.get_local_backend()
                started = time.monotonic()
                reply = document_chat(backend, TASKS_SYSTEM, [{"role": "user", "content": PROMPT}], lambda _: None)
                report.update(raw_reply=reply, answer_seconds=round(time.monotonic() - started, 3))
                good, bad, note = score(reply)

                class CapturedReply:
                    def chat(self, *_args):
                        return reply

                accepted, dropped = agent_tasks(CapturedReply(), [
                    TaskDoc("doc-001", "Prescription.txt", "Prescription.txt", "a" * 64, DOC)])
                report.update(kept=good, dropped=bad, note=note, runtime_kept=len(accepted),
                              runtime_dropped=dropped,
                              date_results=[{"due": task.due, "flags": list(task.flags)} for task in accepted])
                candidate_pass = good == 3 and bad == 0 and len(accepted) == 3 and not dropped
            except RuntimeError as error:
                report["error"] = str(error)
            finally:
                pane.close()
                pane.deleteLater()
                app.processEvents()
        report["requests"] = captured
        report["receipt_operations"] = [row["op"] for row in log.tail()]
        report["receipt_count"] = log.verify()
        report["cloud_requests"] = report["receipt_operations"].count("agent_door_request")
        scored = [row for row in captured if row["stream"] is True]
        report["pass"] = (candidate_pass and len(scored) == 1 and report["cloud_requests"] == 0
                          and scored[0]["options"] == {"temperature": 0, "seed": 42}
                          and scored[0]["format"] == {"type": "array", "items": {"type": "object"}})


if __name__ == "__main__":
    raise SystemExit(main())
