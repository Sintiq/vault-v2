"""Calibrate document prompt estimates using synthetic prose and local Ollama.

Run from the repository with its main Python environment and -B. A fresh run
warms up before 27 sequential requests; the explicit continuation retains 17
measurements and makes only the authorized ten remaining calls. No owner root
is accepted.
This reports empirical estimates; it does not set the product admission budget.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request


MODEL = "llama3.1:8b"
BASE = "http://127.0.0.1:11434"
OPTIONS = {"num_ctx": 8192, "num_predict": 2048, "temperature": 0, "seed": 42}
FORMAT = {"type": "array", "items": {"type": "object"}}
DEADLINE_SECONDS = 330
SOCKET_TIMEOUT_SECONDS = 320
MARGIN = 1.25
INPUT_TOKEN_BUDGET = 8192 - 2048 - 256
LENGTHS = (1000, 3000, 6000)
LANGUAGES = ("en", "ru", "mixed")
JOBS = ("sort", "tasks", "health")

# Original, wholly fictional source material. Parallel paragraphs deliberately
# mix administrative narrative with a small number of actionable health facts.
# Numbered sections vary document structure; truncation here constructs exact
# benchmark lengths, never changes a real user's document or submitted request.
PARAGRAPHS = {
    "en": (
        "Synthetic example only. Cedar Example Clinic prepared this fictional visit packet on 2026-09-14. The document records a follow-up visit for headaches. Return the completed intake form by 2026-10-15. The name, organisation, dates and circumstances in this packet are invented for a software measurement.",
        "The reception cover sheet identifies the packet as a copy for the document owner. A blue divider separates correspondence from the administrative index. Each attachment has a short title and a page number. The index describes where information appears; it does not replace any statement in the source pages.",
        "The appointment record states that a telephone conversation took place on 2026-09-12. The clerk verified the spelling printed on the cover sheet and recorded the preferred language as English and Russian. No account number, address, telephone number or other real personal information appears in this synthetic example.",
        "The laboratory page records a blood test on 2026-09-13. The result line reads: sample received; report pending. The packet contains no numeric measurement, reference interval or interpretation. This paragraph is a description of the fictional page and does not establish a condition or recommend treatment.",
        "The archive inventory groups the material into correspondence, forms and supporting records. The front page carries a revision marker, while the envelope has a separate dispatch label. These labels help distinguish copies that otherwise have similar names. The original order of pages is preserved in the inventory.",
        "The insurance insert says that the document is informational. Its table has headings for document title, issuing organisation and issue year, followed by empty cells in this sample. The cover letter explains that an empty cell means information was not supplied; it is not an instruction to infer a missing value.",
        "The owner-facing summary quotes the intake form without adding an assessment. Bring the insurance card to the follow-up appointment. A separate note says that a copy of the visit record is enclosed. The words on the enclosure and the words on the cover sheet may overlap, but each page keeps its own source title.",
        "The records desk uses a simple correspondence register. Entries appear in the order received, with a reference code printed beside each title. A corrected title is shown as a new line rather than an erased entry. This sample illustrates a filing convention and contains no instruction to alter an existing record.",
        "A document preparation note distinguishes printed text from handwritten annotations. The typed page has regular margins and a clear heading. A stamp near the footer indicates that this is a demonstration copy. There are no photographs, signatures, patient identifiers or private attachments in this generated packet.",
        "The final administrative section describes the contents of the envelope: a cover sheet, an index and supporting pages. The number printed on an enclosure refers to its position in the packet rather than a calendar date. All examples are fictional, and the software measurement concerns prompt size rather than clinical accuracy.",
    ),
    "ru": (
        "Только синтетический пример. Учебная клиника «Кедр» подготовила этот вымышленный пакет документов 2026-09-14. В документе указан повторный визит по поводу головной боли. Верните заполненную анкету до 2026-10-15. Название организации, даты и обстоятельства придуманы для измерения работы программы.",
        "На сопроводительном листе указано, что пакет является копией для владельца документов. Синий разделитель отделяет переписку от административного указателя. У каждого приложения есть короткий заголовок и номер страницы. Указатель показывает расположение сведений, но не заменяет утверждения на исходных страницах.",
        "В записи о приёме указано, что телефонный разговор состоялся 2026-09-12. Сотрудник сверил написание на обложке и отметил предпочтительные языки: русский и английский. В этом синтетическом примере нет настоящего номера счёта, адреса, телефона или других персональных сведений.",
        "На лабораторной странице указан анализ крови от 2026-09-13. Строка результата гласит: образец получен; отчёт ожидается. Пакет не содержит числового результата, референсного интервала или интерпретации. Этот абзац описывает вымышленную страницу и не устанавливает заболевание и не рекомендует лечение.",
        "Архивная опись объединяет материалы в переписку, формы и подтверждающие записи. На первой странице есть отметка редакции, а на конверте отдельная почтовая наклейка. Такие обозначения помогают различать копии с похожими названиями. Исходная последовательность страниц сохранена в описи.",
        "В страховом приложении сказано, что документ носит информационный характер. В таблице есть заголовки для названия документа, организации и года выпуска, а ячейки учебного образца оставлены пустыми. Пустая ячейка означает отсутствие предоставленных сведений, а не разрешение придумать недостающее значение.",
        "Краткая записка для владельца цитирует анкету без дополнительной оценки. Принесите страховую карточку на повторный приём. В отдельной строке указано, что копия записи о визите приложена. Текст приложения и сопроводительного листа может повторяться, но каждая страница сохраняет собственный заголовок источника.",
        "В отделе документов используется простой журнал переписки. Записи расположены в порядке получения, рядом с каждым заголовком напечатан справочный код. Исправленное название показывается новой строкой, а старая запись не стирается. Образец иллюстрирует порядок хранения и не предписывает изменять существующую запись.",
        "В примечании о подготовке документа различаются печатный текст и рукописные пометки. У печатной страницы ровные поля и чёткий заголовок. Штамп рядом с нижним колонтитулом обозначает демонстрационную копию. В созданном пакете нет фотографий, подписей, идентификаторов пациента или частных приложений.",
        "Последний административный раздел описывает содержимое конверта: сопроводительный лист, указатель и дополнительные страницы. Напечатанный номер обозначает место приложения в пакете, а не календарную дату. Все примеры вымышлены; измерение программы касается размера запроса, а не клинической точности.",
    ),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def synthetic_text(language: str, length: int) -> str:
    if language not in LANGUAGES or length not in LENGTHS:
        raise ValueError("unsupported synthetic case")
    pieces = []
    for index in range(40):
        selected = language if language != "mixed" else ("en" if index % 2 == 0 else "ru")
        label = "Section" if selected == "en" else "Раздел"
        pieces.append(f"{label} {index + 1:02d}. {PARAGRAPHS[selected][index % len(PARAGRAPHS[selected])]}")
        text = "\n\n".join(pieces)
        if len(text) >= length:
            return text[:length]
    raise AssertionError("source material did not reach target length")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def transport_worker() -> int:
    """One request per isolated worker; the parent owns the wall-clock deadline."""
    request = json.load(sys.stdin)
    endpoint = request["endpoint"]
    if endpoint not in ("/api/chat", "/api/ps", "/api/tags"):
        raise ValueError("only the fixed local measurement endpoints are allowed")
    payload = request.get("payload")
    if endpoint == "/api/chat" and (not isinstance(payload, dict) or payload.get("model") != MODEL):
        raise ValueError("only the selected local model is allowed")
    if endpoint == "/api/chat" and payload.get("options", {}).get("num_predict") not in (8, OPTIONS["num_predict"]):
        raise ValueError("superseded calibration output limit refused before the model call")
    body = canonical_bytes(payload) if payload is not None else None
    req = urllib.request.Request(BASE + endpoint, data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST" if body is not None else "GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    started = time.perf_counter()
    with opener.open(req, timeout=SOCKET_TIMEOUT_SECONDS) as response:
        result = json.loads(response.read().decode("utf-8"))
    print(json.dumps({"response": result, "http_elapsed_seconds": time.perf_counter() - started},
                     ensure_ascii=False))
    return 0


def request_local(endpoint: str, payload: dict | None = None) -> dict:
    started = time.perf_counter()
    try:
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()), "--transport-worker"],
            input=json.dumps({"endpoint": endpoint, "payload": payload}, ensure_ascii=False),
            text=True, encoding="utf-8", capture_output=True, timeout=DEADLINE_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"local transport worker failed: {exc.stderr.strip()}") from exc
    observed = json.loads(result.stdout)
    observed["process_elapsed_seconds"] = time.perf_counter() - started
    return observed


def invoke_job(job: str, backend, text: str, language: str):
    from vault_v2 import health, sorting, tasks

    name = f"synthetic-{language}.txt"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if job == "sort":
        return sorting.agent_cards(backend, [sorting.SortInput(
            "doc-001", Path(name), name, "TEXT", len(text.encode("utf-8")), digest, text)])
    if job == "tasks":
        return tasks.agent_tasks(backend, [tasks.TaskDoc("doc-001", name, name, digest, text)])
    if job == "health":
        return health.agent_entries(backend, [health.HealthDoc("doc-001", name, digest, text)])
    raise ValueError("unsupported job")


class MeasurementBackend:
    def __init__(self, row: dict, *, dry_run: bool = False):
        self.row, self.dry_run = row, dry_run
        self.calls = 0

    def chat(self, system, messages, on_chunk):
        return self.document_chat(system, messages, on_chunk)

    def document_chat(self, system, messages, on_chunk, *, json_shape="array"):
        if json_shape != "array" or self.calls:
            raise RuntimeError("one array-format request is permitted per measured job")
        self.calls += 1
        envelope = {"system": system, "messages": messages}
        encoded = canonical_bytes(envelope)
        self.row.update(
            system_unicode_chars=len(system),
            message_content_unicode_chars=sum(len(m["content"]) for m in messages),
            full_content_unicode_chars=len(system) + sum(len(m["content"]) for m in messages),
            canonical_utf8_bytes=len(encoded),
            input_sha256=hashlib.sha256(encoded).hexdigest(),
            prompt=envelope,
        )
        if self.dry_run:
            return "[]"
        payload = {"model": MODEL, "messages": [{"role": "system", "content": system}, *messages],
                   "options": OPTIONS, "format": FORMAT, "stream": False, "keep_alive": "30m"}
        self.row["request_started_at"] = utc_now()
        started = time.perf_counter()
        try:
            observed = request_local("/api/chat", payload)
        except Exception:
            self.row["failed_wall_seconds"] = time.perf_counter() - started
            self.row["request_failed_at"] = utc_now()
            raise
        response = observed["response"]
        self.row.update(observed)
        for key in ("prompt_eval_count", "prompt_eval_cached_count", "eval_count", "done_reason",
                    "total_duration", "load_duration", "prompt_eval_duration", "eval_duration"):
            self.row[key] = response.get(key)
        self.row["request_completed_at"] = utc_now()
        count = self.row["prompt_eval_count"]
        if not isinstance(count, int) or count <= 0 or response.get("done") is not True:
            raise RuntimeError("measurement lacks a completed positive prompt token count")
        self.row["bytes_per_prompt_token"] = len(encoded) / count
        return response.get("message", {}).get("content", "")


def derive_summary(rows: list[dict]) -> dict:
    measured = [r for r in rows if isinstance(r.get("prompt_eval_count"), int)
                and r["prompt_eval_count"] > 0]
    ratios = {language: min(r["bytes_per_prompt_token"] for r in measured if r["language"] == language)
              for language in LANGUAGES if any(r["language"] == language for r in measured)}
    groups = {f"{language}/{job}": min(r["bytes_per_prompt_token"] for r in measured
                                      if r["language"] == language and r["job"] == job)
              for language in LANGUAGES for job in JOBS
              if any(r["language"] == language and r["job"] == job for r in measured)}
    for row in measured:
        row["estimated_full_request_tokens"] = math.ceil(
            row["canonical_utf8_bytes"] / ratios[row["language"]] * MARGIN)
        row["within_estimated_input_budget"] = row["estimated_full_request_tokens"] <= INPUT_TOKEN_BUDGET
    output_counts = [r["eval_count"] for r in measured if isinstance(r.get("eval_count"), int)]
    elapsed = [r["http_elapsed_seconds"] for r in measured]
    return {
        "measured_count": len(measured), "required_count": 27,
        "minimum_bytes_per_prompt_token_by_language": ratios,
        "minimum_bytes_per_prompt_token_by_language_and_job": groups,
        "global_minimum_bytes_per_prompt_token": min(ratios.values()) if ratios else None,
        "formula": "ceil(canonical_utf8_bytes / language_minimum_bytes_per_prompt_token * 1.25)",
        "input_token_budget": INPUT_TOKEN_BUDGET,
        "observed_maximum_output_tokens": max(output_counts) if output_counts else None,
        "output_length_limited_cases": [r["case"] for r in measured if r.get("done_reason") == "length"],
        "warm_http_seconds": {"min": min(elapsed), "median": statistics.median(elapsed), "max": max(elapsed)} if elapsed else None,
        "over_200_second_cases": [r["case"] for r in measured if r["http_elapsed_seconds"] > 200],
        "over_250_second_cases": [r["case"] for r in measured if r["http_elapsed_seconds"] > 250],
        "single_6000_character_documents": [
            {key: row.get(key) for key in ("case", "language", "job", "excerpt_unicode_chars",
              "canonical_utf8_bytes", "prompt_eval_count", "estimated_full_request_tokens",
              "within_estimated_input_budget", "eval_count", "done_reason", "http_elapsed_seconds")}
            for row in measured if row["excerpt_unicode_chars"] == 6000],
        "all_nine_6000_character_cases_within_estimate":
            len([r for r in measured if r["excerpt_unicode_chars"] == 6000]) == 9
            and all(r["within_estimated_input_budget"] for r in measured if r["excerpt_unicode_chars"] == 6000),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-local-synthetic", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="build all real job prompts without model calls or writes")
    parser.add_argument("--resume-2048-partial", action="store_true",
                        help="retain the first 17 measurements, retry RU6000 Health once, then measure the nine mixed cases")
    parser.add_argument("--output", type=Path, default=Path("docs/document-budget-calibration.json"))
    args = parser.parse_args(argv)
    if not args.dry_run and not args.run_local_synthetic:
        parser.error("model calls require --run-local-synthetic")
    allowed_output = Path(__file__).resolve().parents[1] / "docs" / "document-budget-calibration.json"
    if args.output.resolve() != allowed_output:
        parser.error("output must be docs/document-budget-calibration.json in this checkout")
    if not args.dry_run and args.output.exists():
        parser.error("output already exists; preserve previous evidence")
    report = {
        "schema": "vault-document-budget-calibration@1", "started_at": utc_now(),
        "run_id": "2048-" + utc_now(),
        "synthetic_only": True, "model": MODEL, "options": OPTIONS, "format": FORMAT,
        "sequential_requests": True, "per_request_deadline_seconds": DEADLINE_SECONDS,
        "socket_timeout_seconds": SOCKET_TIMEOUT_SECONDS, "uninterrupted_run": True,
        "margin": MARGIN, "input_token_budget": INPUT_TOKEN_BUDGET,
        "source_generator": "tools/calibrate_document_budget.py:PARAGRAPHS and synthetic_text",
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "prior_output_token_evidence": {"observed_maximum": None, "status": "unknown",
            "inspected": ["docs/ocr-qualification.json", "tools/qualify_local_ocr.py", "tools/pick_local_model.py", "tools/check_dates_open_door.py"],
            "reason": "Inspected old qualification evidence records timings and accepted items, not eval_count. The interrupted 1024 calibration reached its output cap on EN6000/RU6000 Health, so its true required maximum is unknown. 2048 is the explicit updated calibration setting, not a derived maximum.",
            "superseded_evidence": "docs/document-budget-calibration-1024.json"},
        "limitations": [
            "Empirical estimate for these synthetic prose samples and the observed model digest; not an exact tokenizer or a context attestation.",
            "No guarantee for arbitrary Unicode, OCR noise, unusual metadata, custom model templates, or languages not measured.",
            "Prompt token counts include server framing while canonical bytes describe {system,messages}; the ratio is deliberately end-to-end.",
            "Only one document per case was measured; multi-document admission requires applying the estimate to the complete assembled request.",
            "A response completing within the token cap does not prove factual extraction accuracy or complete document comprehension.",
        ],
        "cases": [], "complete": False,
    }
    if args.resume_2048_partial:
        prior_path = allowed_output.with_name("document-budget-calibration-2048-partial.json")
        prior_bytes = prior_path.read_bytes()
        prior = json.loads(prior_bytes)
        prior_rows = [row for row in prior["cases"] if isinstance(row.get("prompt_eval_count"), int)]
        expected_prior = [f"{language}-{length}-{job}" for language in ("en", "ru")
                          for length in LENGTHS for job in JOBS][:-1]
        if ([row["case"] for row in prior_rows] != expected_prior or prior.get("complete")
                or prior.get("options") != OPTIONS or prior["cases"][-1]["case"] != "ru-6000-health"):
            parser.error("resume requires the preserved 17-case 2048 run with its RU6000 Health timeout")
        old_run_id = "2048-initial-" + prior["started_at"]
        report["cases"] = [{**row, "run_id": old_run_id,
                            "source_script_sha256": prior["script_sha256"],
                            "source_artifact": "docs/" + prior_path.name,
                            "per_request_deadline_seconds": prior["per_request_deadline_seconds"],
                            "socket_timeout_seconds": 175} for row in prior_rows]
        report.update(uninterrupted_run=False, resumed=True,
                      authorized_new_case_count=10, original_started_at=prior["started_at"],
                      expected_model_digest=prior["model_digest"],
                      previous_run={"run_id": old_run_id, "artifact": "docs/" + prior_path.name,
                                    "artifact_sha256": hashlib.sha256(prior_bytes).hexdigest(),
                                    "source_script_sha256": prior["script_sha256"],
                                    "completed_measurements_retained": 17,
                                    "censored_timeout_retained_in_source": True},
                      retry_authorization="Lead 2026-09-23 10:15: one RU6000 Health retry after server completion, then nine mixed cases; child 330s/socket 320s.",
                      previous_http_completion_evidence="Ollama server.log: [GIN] 2026/09/23 - 03:51:22 | 500 | 2m55s | 127.0.0.1 | POST /api/chat")

    def save():
        if not args.dry_run:
            report["summary"] = derive_summary(report["cases"])
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if not args.dry_run:
        # Reserve the new evidence artifact before any local network request.
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
    try:
        if not args.dry_run:
            report["ps_before"] = request_local("/api/ps")
            report["tags_before"] = request_local("/api/tags")
            selected = [m for m in report["tags_before"]["response"].get("models", [])
                        if m.get("name") == MODEL]
            if len(selected) != 1 or selected[0].get("remote_host") or selected[0].get("remote_model"):
                raise RuntimeError("exact selected local model not confirmed by tags")
            report["model_digest"] = selected[0].get("digest")
            if args.resume_2048_partial and report["model_digest"] != report["expected_model_digest"]:
                raise RuntimeError("model digest changed since the preserved run; no retry sent")
            save()
            if args.resume_2048_partial:
                loaded = [m for m in report["ps_before"]["response"].get("models", [])
                          if m.get("name") == MODEL and m.get("context_length") == 8192]
                if len(loaded) != 1:
                    raise RuntimeError("resume requires the existing warm 8192 model; no extra warm-up authorized")
                report["warmup"] = {"not_repeated": True, "reason": "existing 8192 residency confirmed; only ten new measured chat calls authorized"}
                print("resume: 17 preserved measurements; one RU6000 Health retry followed by nine mixed cases", flush=True)
            else:
                print("warm-up: loading context 8192; excluded from measured cases", flush=True)
                report["warmup"] = request_local("/api/chat", {
                    "model": MODEL, "messages": [{"role": "user", "content": "Return an empty JSON array."}],
                    "options": {**OPTIONS, "num_predict": 8}, "format": FORMAT,
                    "stream": False, "keep_alive": "30m"})
                report["ps_after_warmup"] = request_local("/api/ps")
            save()
        for language in LANGUAGES:
            for length in LENGTHS:
                text = synthetic_text(language, length)
                for job in JOBS:
                    case = f"{language}-{length}-{job}"
                    if args.resume_2048_partial and any(row["case"] == case for row in report["cases"]):
                        continue
                    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != report["script_sha256"]:
                        raise RuntimeError("calibration source changed; stopped between requests before the next model call")
                    row = {"case": case, "language": language,
                           "run_id": report["run_id"], "source_script_sha256": report["script_sha256"],
                           "per_request_deadline_seconds": DEADLINE_SECONDS,
                           "socket_timeout_seconds": SOCKET_TIMEOUT_SECONDS,
                           "job": job, "excerpt_unicode_chars": len(text),
                           "excerpt_utf8_bytes": len(text.encode("utf-8")),
                           "excerpt_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}
                    report["cases"].append(row)
                    backend = MeasurementBackend(row, dry_run=args.dry_run)
                    try:
                        invoke_job(job, backend, text, language)
                    except Exception as exc:
                        row["error"] = f"{type(exc).__name__}: {exc}"
                        raise
                    if backend.calls != 1:
                        raise RuntimeError("public document job did not call the measurement backend once")
                    save()
                    print(json.dumps({"progress": f"{len(report['cases'])}/27", **{
                        key: row.get(key) for key in ("case", "canonical_utf8_bytes", "prompt_eval_count",
                                                     "eval_count", "done_reason", "http_elapsed_seconds")}}, ensure_ascii=False), flush=True)
        if not args.dry_run:
            report["ps_after"] = request_local("/api/ps")
        report["complete"] = True
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["aborted_without_retry"] = True
        # A timed-out generation may still be running server-side: do not send
        # another chat or probe. This retains strict sequential-call semantics.
        print(report["error"], file=sys.stderr, flush=True)
    finally:
        report["completed_at"] = utc_now()
        save()
    if args.dry_run:
        print(json.dumps({"dry_run": True, "case_count": len(report["cases"]), "complete": report["complete"]}))
    else:
        print(json.dumps(report.get("summary", {}), ensure_ascii=False, indent=2), flush=True)
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    if sys.argv[1:] == ["--transport-worker"]:
        raise SystemExit(transport_worker())
    raise SystemExit(main())
