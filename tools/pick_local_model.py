"""Which local model can actually do the vault's work?

The vault asks a model for strict JSON whose every item must quote the
document literally; anything that does not is dropped. That is a harder test
than a chat reply, and the only one worth choosing a model on. This runs the
real prompt through each installed model and reports what survived and how
long it took.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from vault_v2.agent import LOCAL_UNAVAILABLE, LocalModelUnavailable, OllamaBackend, document_chat, ollama_models, warm_ollama
from vault_v2.tasks import TASKS_SYSTEM, TaskDoc, agent_tasks, _norm

DOC = (
    "Prescription — Bay Neurology Clinic, 2026-09-14.\n"
    "Sumatriptan 50 mg, 9 tablets. Refill before 2026-10-16.\n"
    "Follow-up visit in 3 months; schedule with the front desk.\n"
    "Bring the insurance card to the next appointment.\n"
)
PROMPT = (
    "Documents:\n"
    + json.dumps([{"doc": "doc-001", "name": "Prescription.txt", "text": DOC}], indent=1)
    + "\n\nReply with the JSON array only."
)


def score(reply: str) -> tuple[int, int, str]:
    """Returns: tasks that quote the document, tasks that invented a quote, note."""
    start, end = reply.find("["), reply.rfind("]")
    if start < 0 or end <= start:
        return 0, 0, "no JSON array in the reply"
    try:
        items = json.loads(reply[start : end + 1])
    except ValueError as exc:
        return 0, 0, f"unparseable JSON: {exc}"
    if not isinstance(items, list):
        return 0, 0, "JSON was not an array"
    good = bad = 0
    for item in items:
        if not isinstance(item, dict):
            bad += 1
            continue
        quote = str(item.get("quote", ""))
        if len(quote) >= 4 and _norm(quote) in _norm(DOC):
            good += 1
        else:
            bad += 1
    return good, bad, f"{len(items)} proposed"


def warm(model: str) -> None:
    if not warm_ollama(model):
        raise LocalModelUnavailable(LOCAL_UNAVAILABLE)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="Run only this exact installed model")
    parser.add_argument("--warm-runs", type=int, default=0,
                        help="Additional consecutive runs after the initial generation")
    parser.add_argument("--output", type=Path, help="Local synthetic evidence JSON, including raw replies")
    args = parser.parse_args()
    if not 0 <= args.warm_runs <= 10:
        parser.error("--warm-runs must be between 0 and 10")
    models = [m for m in ollama_models() if "coder" not in m]
    if args.model:
        models = [m for m in models if m == args.model]
    if not models:
        print("no matching local models")
        return 1
    report = {"timestamp": datetime.now(timezone.utc).isoformat(), "synthetic_only": True,
              "options": {"temperature": 0, "seed": 42}, "runs": []}

    def save():
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    failed = False
    print(f"{'model':<22} {'phase':<9} {'answer s':>9} {'kept':>5} {'dropped':>8} {'runtime':>8}", flush=True)
    for model in models:
        started = time.monotonic()
        warm(model)
        load_seconds = time.monotonic() - started
        backend = OllamaBackend(model)
        for index in range(args.warm_runs + 1):
            phase = "initial" if index == 0 else f"warm-{index}"
            started = time.monotonic()
            record = {"model": model, "phase": phase, "load_seconds": load_seconds if index == 0 else 0}
            try:
                reply = document_chat(backend, TASKS_SYSTEM,
                                      [{"role": "user", "content": PROMPT}], lambda _: None)
            except RuntimeError as exc:
                record.update({"error": str(exc), "raw_reply": None, "pass": False})
                report["runs"].append(record)
                save()
                failed = True
                print(f"{model:<22} {phase:<9} ERROR {exc}", flush=True)
                continue
            good, bad, note = score(reply)

            class Captured:
                def chat(self, *_args):
                    return reply

            accepted, dropped = agent_tasks(Captured(), [
                TaskDoc("doc-001", "Prescription.txt", "Prescription.txt", "a" * 64, DOC)])
            passed = good == 3 and bad == 0 and len(accepted) == 3 and not dropped
            record.update({"answer_seconds": time.monotonic() - started, "kept": good,
                           "dropped": bad, "note": note, "runtime_kept": len(accepted),
                           "runtime_dropped": dropped, "raw_reply": reply, "pass": passed,
                           "date_results": [{"due": task.due, "flags": list(task.flags)} for task in accepted]})
            report["runs"].append(record)
            save()  # preserve a failing reply before any subsequent generation
            print(f"{model:<22} {phase:<9} {record['answer_seconds']:>9.1f} {good:>5} {bad:>8} {len(accepted):>8}", flush=True)
            if not passed and (index > 0 or args.warm_runs == 0):
                failed = True
    if args.output:
        print(f"Synthetic evidence: {args.output}", flush=True)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
