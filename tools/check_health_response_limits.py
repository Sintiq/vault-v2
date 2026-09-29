"""Two real local Health requests over the unchanged synthetic 6000-char texts.

No model work occurs on import or --prepare. After an explicit GO, run with
python -X utf8 -B -m tools.check_health_response_limits --run-local-synthetic
from the checkout using the main venv Python. Use module invocation, not a
direct script path, so imports resolve identically in the parent and child.
The public agent_entries -> OllamaBackend route creates its own real schema;
an observer only records transport inputs and streaming response frames.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
from unittest.mock import patch

from tools.calibrate_document_budget import synthetic_text


MODEL = "llama3.1:8b"
BASE = "http://127.0.0.1:11434"
LANGUAGES = ("ru", "en")
CHILD_DEADLINE_SECONDS = 330
ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "health-response-limits.json"
SOURCE_FILES = (
    "tools/check_health_response_limits.py",
    "tools/calibrate_document_budget.py",
    "vault_v2/agent.py",
    "vault_v2/health.py",
    "vault_v2/document_budget.py",
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def source_hashes() -> dict:
    return {name: digest((ROOT / name).read_bytes()) for name in SOURCE_FILES}


def emit(event: str, **fields) -> None:
    # The parent captures these events, including partial stdout on hard timeout.
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def worker(language: str) -> int:
    from vault_v2.agent import OllamaBackend
    from vault_v2.health import HealthDoc, agent_entries

    expected = json.load(sys.stdin)
    observed_hashes = source_hashes()
    if expected != observed_hashes:
        emit("worker_result", success=False, error="source changed before the public job; no model call",
             source_hashes=observed_hashes)
        return 2
    text = synthetic_text(language, 6000)
    name = f"synthetic-{language}.txt"
    document = HealthDoc("doc-001", name, digest(text.encode("utf-8")), text)
    original_open = urllib.request.OpenerDirector.open
    request_count = 0
    started = time.perf_counter()
    emit("worker_started", language=language, started_at=now(), excerpt_unicode_chars=len(text),
         excerpt_sha256=digest(text.encode("utf-8")), source_hashes=observed_hashes)

    class ObservedResponse:
        def __init__(self, response):
            self.response = response

        def __enter__(self):
            self.response.__enter__()
            return self

        def __exit__(self, *args):
            return self.response.__exit__(*args)

        def __iter__(self):
            for line in self.response:
                # Preserve every raw line before giving it to application code.
                emit("response_frame", raw=line.decode("utf-8"), elapsed_seconds=time.perf_counter() - started)
                yield line

        def __getattr__(self, name):
            return getattr(self.response, name)

    def observe(opener, request, *args, **kwargs):
        nonlocal request_count
        url = request.full_url if hasattr(request, "full_url") else str(request)
        if url != BASE + "/api/chat" or not getattr(request, "data", None):
            raise RuntimeError("qualification refuses any transport except the real local chat POST")
        payload = json.loads(request.data)
        if payload.get("model") != MODEL or payload.get("stream") is not True:
            raise RuntimeError("qualification requires the selected local model's real streaming request")
        request_count += 1
        if request_count != 1:
            raise RuntimeError("only one model call per qualification case is authorized")
        messages = payload.get("messages", [])
        if not messages or messages[0].get("role") != "system":
            raise RuntimeError("real request lacks its system message")
        envelope = {"system": messages[0]["content"], "messages": messages[1:]}
        encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        emit("request", started_at=now(), url=url, payload=payload,
             wire_body_utf8=request.data.decode("utf-8"), wire_sha256=digest(request.data),
             canonical_utf8_bytes=len(encoded), input_sha256=digest(encoded),
             timeout_passed_to_real_opener=kwargs.get("timeout", args[1] if len(args) > 1 else None))
        # Original private opener, proxy/redirect policy, options, schema and
        # timeout execute exactly as supplied by the production backend.
        return ObservedResponse(original_open(opener, request, *args, **kwargs))

    try:
        with patch.object(urllib.request.OpenerDirector, "open", observe):
            accepted, notes = agent_entries(OllamaBackend(MODEL), [document])
        emit("worker_result", success=True, request_count=request_count,
             accepted=[asdict(entry) for entry in accepted], notes=list(notes),
             public_job_elapsed_seconds=time.perf_counter() - started, completed_at=now())
        return 0
    except Exception as exc:
        emit("worker_result", success=False, request_count=request_count,
             error_type=type(exc).__name__, error=str(exc),
             error_http_status=getattr(exc, "http_status", getattr(exc, "status_code", None)),
             public_job_elapsed_seconds=time.perf_counter() - started, completed_at=now())
        return 1


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def metadata(endpoint: str) -> dict:
    if endpoint not in ("/api/ps", "/api/tags"):
        raise ValueError("only read-only model metadata endpoints are permitted")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(BASE + endpoint, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_events(raw: str | bytes | None) -> list[dict]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    events = []
    for line in (raw or "").splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            events.append({"event": "unparsed_worker_output", "text": line})
    return events


def validate(events: list[dict]) -> dict:
    requests = [event for event in events if event.get("event") == "request"]
    results = [event for event in events if event.get("event") == "worker_result"]
    result = results[-1] if results else {}
    frames, frame_errors = [], []
    for event in events:
        if event.get("event") == "response_frame" and event["raw"].strip():
            try:
                frames.append(json.loads(event["raw"]))
            except ValueError as exc:
                frame_errors.append(str(exc))
    terminal = next((frame for frame in reversed(frames) if frame.get("done") is True), {})
    content = "".join(frame.get("message", {}).get("content", "") for frame in frames)
    raw_items, parse_error = None, None
    try:
        parsed = json.loads(content)
        if not isinstance(parsed, list):
            raise ValueError("raw model response was not an array")
        raw_items = parsed
    except ValueError as exc:
        parse_error = str(exc)
    payload = requests[0]["payload"] if len(requests) == 1 else {}
    schema = payload.get("format", {})
    schema_items = schema.get("items", {}) if isinstance(schema, dict) else {}
    properties = schema_items.get("properties", {}) if isinstance(schema_items, dict) else {}
    quote_schema = properties.get("quote", {})
    schema_check = (isinstance(schema, dict) and schema.get("type") == "array"
                    and schema.get("maxItems") == 12 and isinstance(quote_schema, dict)
                    and quote_schema.get("type") == "string" and quote_schema.get("maxLength") == 200)
    counts = Counter(str(item.get("doc")) for item in raw_items or [] if isinstance(item, dict))
    raw_valid = (raw_items is not None and all(isinstance(item, dict) for item in raw_items)
                 and all(item.get("doc") == "doc-001" for item in raw_items)
                 and all(isinstance(item.get("quote"), str) and len(item["quote"]) <= 200 for item in raw_items)
                 and all(count <= 12 for count in counts.values()))
    accepted = result.get("accepted", [])
    accepted_counts = Counter(item.get("doc_sha256") for item in accepted)
    accepted_valid = (result.get("success") is True and len(accepted) <= 12
                      and all(count <= 12 for count in accepted_counts.values())
                      and all(isinstance(item.get("quote"), str) and len(item["quote"]) <= 200 for item in accepted))
    options = payload.get("options", {})
    options_check = all(options.get(key) == value for key, value in
                        {"num_ctx": 8192, "num_predict": 2048, "temperature": 0, "seed": 42}.items())
    nonempty_fixture_result = bool(raw_items) and bool(accepted)
    passed = (len(requests) == 1 and schema_check and options_check and not frame_errors
              and terminal.get("done_reason") == "stop" and raw_valid and accepted_valid
              and nonempty_fixture_result)
    return {
        "passed": passed, "request_count": len(requests), "actual_schema_limits_match": schema_check,
        "actual_options_match": options_check, "raw_items_per_document": dict(counts),
        "raw_items_within_limits": raw_valid, "accepted_count": len(accepted),
        "accepted_items_within_limits": accepted_valid,
        "nonempty_fixture_result": nonempty_fixture_result,
        "maximum_raw_quote_chars": max((len(item.get("quote", "")) for item in raw_items or []
                                         if isinstance(item, dict) and isinstance(item.get("quote"), str)), default=None),
        "raw_response_content": content, "raw_json_parse_error": parse_error,
        "malformed_frame_errors": frame_errors, "terminal_frame": terminal,
        "prompt_eval_count": terminal.get("prompt_eval_count"), "eval_count": terminal.get("eval_count"),
        "done_reason": terminal.get("done_reason"), "load_duration": terminal.get("load_duration"),
        "prompt_eval_duration": terminal.get("prompt_eval_duration"),
        "public_result": result,
    }


def run_case(language: str, expected_sources: dict) -> dict:
    started = time.perf_counter()
    row = {"language": language, "excerpt_unicode_chars": 6000, "started_at": now(),
           "hard_deadline_seconds": CHILD_DEADLINE_SECONDS}
    try:
        process = subprocess.run(
            [sys.executable, "-X", "utf8", "-B", "-m", "tools.check_health_response_limits", "--worker", language],
            input=json.dumps(expected_sources), text=True, encoding="utf-8", capture_output=True,
            timeout=CHILD_DEADLINE_SECONDS, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            cwd=ROOT,
        )
        row.update(events=parse_events(process.stdout), child_exit_code=process.returncode,
                   child_stderr=process.stderr, timed_out=False)
    except subprocess.TimeoutExpired as exc:
        # subprocess.run kills and waits for the child. Never send the next case
        # after this; the server's post-disconnect state must be checked separately.
        row.update(events=parse_events(exc.stdout), timed_out=True,
                   error="child exceeded 330 seconds and was terminated; server completion unknown")
    row.update(process_elapsed_seconds=time.perf_counter() - started, completed_at=now())
    row["below_170_seconds"] = row["process_elapsed_seconds"] < 170
    row["validation"] = validate(row["events"])
    row["passed"] = not row["timed_out"] and row["validation"]["passed"]
    return row


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true", help="read code and generate the two exact texts; no writes or network")
    parser.add_argument("--run-local-synthetic", action="store_true")
    parser.add_argument("--worker", choices=LANGUAGES, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        return worker(args.worker)
    if args.prepare:
        print(json.dumps({"prepared": True, "model_calls": 0, "source_hashes": source_hashes(),
                          "cases": [{"language": language, "chars": len(synthetic_text(language, 6000)),
                                     "sha256": digest(synthetic_text(language, 6000).encode("utf-8"))}
                                    for language in LANGUAGES]}, ensure_ascii=False, indent=2))
        return 0
    if not args.run_local_synthetic:
        parser.error("model calls require explicit --run-local-synthetic after root GO")
    if OUTPUT.exists():
        parser.error("health-response-limits.json already exists; preserve prior evidence")
    report = {"schema": "vault-health-response-limits@1", "synthetic_only": True,
              "started_at": now(), "model": MODEL, "public_route": "agent_entries -> OllamaBackend.document_chat",
              "source_hashes": source_hashes(), "sequential_requests": True,
              "authorized_case_count": 2, "languages_in_order": list(LANGUAGES),
              "hard_child_deadline_seconds": CHILD_DEADLINE_SECONDS,
              "transport_timeout_policy": "production backend timeout unchanged; parent enforces 330 seconds",
              "cases": [], "complete": False, "passed": False,
              "limitations": ["Two synthetic single-document examples do not prove provider schema enforcement for all inputs.",
                              "A normal stop and valid limits do not attest clinical accuracy or complete coverage.",
                              "No schema is reconstructed for the request; the report records the production request verbatim."]}

    def save():
        OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    with OUTPUT.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    try:
        report["ps_before"] = metadata("/api/ps")
        report["tags_before"] = metadata("/api/tags")
        selected = [item for item in report["tags_before"].get("models", []) if item.get("name") == MODEL]
        resident = [item for item in report["ps_before"].get("models", [])
                    if item.get("name") == MODEL and item.get("context_length") == 8192]
        if (len(selected) != 1 or selected[0].get("remote_host") or selected[0].get("remote_model")
                or len(resident) != 1):
            raise RuntimeError("expected already-warm local 8192 model not confirmed; no extra warm-up authorized")
        report["model_digest"] = selected[0].get("digest")
        save()
        for language in LANGUAGES:
            print(f"starting {language}-6000 Health through the real public backend", flush=True)
            row = run_case(language, report["source_hashes"])
            report["cases"].append(row)
            save()
            print(json.dumps({"language": language, "seconds": row["process_elapsed_seconds"],
                              "timed_out": row["timed_out"], **{key: row["validation"].get(key) for key in
                              ("passed", "done_reason", "prompt_eval_count", "eval_count", "accepted_count",
                               "maximum_raw_quote_chars", "actual_schema_limits_match")}}, ensure_ascii=False), flush=True)
            if row["timed_out"] or not row["validation"]["terminal_frame"]:
                report["stopped_without_retry"] = True
                break
        else:
            report["ps_after"] = metadata("/api/ps")
            report["complete"] = True
        report["passed"] = report["complete"] and all(row["passed"] for row in report["cases"])
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["stopped_without_retry"] = True
    finally:
        report["completed_at"] = now()
        report["over_200_second_cases"] = [r["language"] for r in report["cases"] if r["process_elapsed_seconds"] > 200]
        report["over_250_second_cases"] = [r["language"] for r in report["cases"] if r["process_elapsed_seconds"] > 250]
        save()
    print(json.dumps({"complete": report["complete"], "passed": report["passed"], "cases": len(report["cases"]),
                      "over_200_second_cases": report["over_200_second_cases"],
                      "over_250_second_cases": report["over_250_second_cases"], "error": report.get("error")}), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
