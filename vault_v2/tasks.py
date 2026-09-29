"""Tasks — things to do that a Staging document actually says.

The agent proposes; two deterministic checks stand between it and the list:
1. every task must carry a `quote`, and that quote must occur literally in
   the text the agent was given — a task the document does not say is
   dropped, not shown;
2. `due` must match a date parsed deterministically from that quote.

The owner adds the proposals he wants; only that writes. Tasks are keyed by
(document hash, title) and stored in <root>/.tasks/tasks.json; adding,
completing and reopening leave receipts.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from threading import RLock

from .agent import Backend, document_chat
from .dates import checked_saved_date, owner_date, resolve_date, DATE_FLAGS
from .reader import ReadRefused, StagingReader
from .receipts import ReceiptLog

TITLE_MAX = 140
QUOTE_MAX = 240

TASKS_SYSTEM = (
    "You read documents from a private vault and list things the owner has to DO that the "
    "document itself states: deadlines, renewals, refills, follow-up visits, payments due, "
    "forms to return. Answer with STRICT JSON and nothing else — an array of objects:\n"
    '{"doc": "<doc id>", "title": "<imperative, <= 140 chars>", "due": "YYYY-MM-DD or null", '
    '"quote": "<exact words copied from the document that support this task>"}\n'
    "Rules: the quote must be copied character for character from the document text; if the "
    "document states no action, return nothing for it; never infer a date that is not written; "
    "no medical, legal or financial advice — only what the text says. An empty array is a valid answer."
)

_BASELINE = re.compile(
    r"\b(due|expires?|expiry|renew|refill|follow[- ]up|valid until|deadline|pay by|return by|schedule)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Task:
    id: str
    doc_sha256: str
    doc_name: str
    title: str
    due: str | None
    quote: str
    origin: str  # AGENT | BASELINE
    done: bool = False
    due_source: str = "none"  # quote | none | owner
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskDoc:
    id: str
    rel: str
    name: str
    sha256: str
    text: str


def view_task(task: Task) -> Task:
    """Validate for display/reminders without rewriting the stored record."""
    checked = checked_saved_date(task.due, task.quote, task.due_source, task.flags)
    return replace(task, due=checked.value, due_source=checked.due_source, flags=checked.flags)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def task_id(sha256: str, title: str) -> str:
    return f"{sha256[:12]}:{_norm(title)[:60]}"


def collect_docs(reader: StagingReader, notes: list[str] | None = None) -> list[TaskDoc]:
    notes = [] if notes is None else notes
    docs = []
    for i, e in enumerate((x for x in reader.list_staging() if x.kind in {"TEXT", "EXTRACTABLE"}), 1):
        try:
            document = reader.read_document(e.rel)
        except ReadRefused as exc:
            notes.append(f"{e.rel}: not read — {exc}")
            continue
        notes.extend(f"{e.rel}: {warning}" for warning in document.warnings)
        if not document.text.strip():
            notes.append(f"{e.rel}: not read — no usable text was found")
            continue
        docs.append(TaskDoc(f"doc-{i:03d}", e.rel, Path(e.rel).name, document.source_sha256, document.text))
    return docs


def baseline_tasks(docs: list[TaskDoc]) -> list[Task]:
    out = []
    for d in docs:
        for raw in re.split(r"[\r\n]+|(?<=[.!?])\s+(?=[A-ZА-Я])", d.text):
            quote = raw.strip()[:QUOTE_MAX]
            if len(quote) < 8 or not _BASELINE.search(quote):
                continue
            resolved = resolve_date(quote)
            out.append(Task(task_id(d.sha256, quote), d.sha256, d.name, quote[:TITLE_MAX],
                            resolved.value, quote, "BASELINE", due_source=resolved.due_source,
                            flags=resolved.flags))
    return out


def agent_tasks(backend: Backend, docs: list[TaskDoc], *,
                reading_notes: tuple[str, ...] = ()) -> tuple[list[Task], list[str]]:
    """Returns (accepted tasks, dropped-with-reason notes)."""
    if not docs:
        return [], []
    payload = [{"doc": d.id, "name": d.name, "text": d.text} for d in docs]
    prompt = "Documents:\n" + json.dumps(payload, ensure_ascii=False, indent=1) + "\n\nReply with the JSON array only."
    reply = document_chat(backend, TASKS_SYSTEM, [{"role": "user", "content": prompt}], lambda _s: None,
                          documents=tuple(f"{d.id} — {d.rel}" for d in docs), reading_notes=reading_notes)
    start, end = reply.find("["), reply.rfind("]")
    if start < 0 or end <= start:
        return [], ["agent reply unusable: no JSON array"]
    try:
        arr = json.loads(reply[start : end + 1])
    except ValueError as exc:
        return [], [f"agent reply unusable: {exc}"]
    by_id = {d.id: d for d in docs}
    tasks, dropped = [], []
    for obj in arr if isinstance(arr, list) else []:
        if not isinstance(obj, dict):
            continue
        d = by_id.get(str(obj.get("doc", "")))
        title = str(obj.get("title", "")).strip()[:TITLE_MAX]
        quote = str(obj.get("quote", "")).strip()[:QUOTE_MAX]
        if d is None or not title:
            dropped.append("dropped: unknown document or empty title")
            continue
        if len(quote) < 4 or _norm(quote) not in _norm(d.text):
            dropped.append(f"dropped (quote not in document): {title}")
            continue
        resolved = resolve_date(quote, obj.get("due"))
        tasks.append(Task(task_id(d.sha256, title), d.sha256, d.name, title, resolved.value, quote, "AGENT",
                          due_source=resolved.due_source, flags=resolved.flags))
    return tasks, dropped


def propose(reader: StagingReader, backend: Backend | None) -> tuple[list[Task], list[str]]:
    notes: list[str] = []
    docs = collect_docs(reader, notes)
    if backend is None:
        return baseline_tasks(docs), notes + ["no agent — keyword baseline only"]
    tasks, agent_notes = agent_tasks(backend, docs, reading_notes=tuple(notes))
    notes.extend(agent_notes)
    seen = {t.id for t in tasks}
    quotes = {_norm(t.quote) for t in tasks}
    for b in baseline_tasks(docs):  # baseline fills what the agent missed, never duplicates
        if b.id not in seen and _norm(b.quote) not in quotes:
            tasks.append(b)
    return tasks, notes


class TaskStore:
    """Lock order: root write guard -> proposal lock -> store mutation lock."""

    SCHEMA = "vault-v2-tasks@1"

    def __init__(self, tasks_dir: Path, log: ReceiptLog):
        self.dir = Path(tasks_dir)
        self.file = self.dir / "tasks.json"
        self.log = log
        self.generation = 0
        self.mutation_lock = RLock()
        self._tasks: dict[str, Task] = {}
        self._load()

    def _load(self) -> None:
        self._tasks = {}
        if self.file.exists():
            data = json.loads(self.file.read_text(encoding="utf-8"))
            self._tasks = {k: Task(**{**v, "flags": tuple(v.get("flags", ()))}) for k, v in data.get("tasks", {}).items()}

    def _save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        body = {"schema": self.SCHEMA, "tasks": {k: asdict(t) for k, t in sorted(self._tasks.items())}}
        tmp = self.file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.file)

    @contextmanager
    def _mutation(self, label: str):
        with self.log.write(label), self.mutation_lock:
            try:
                yield
            except Exception:
                self._load()
                raise

    def all(self) -> list[Task]:
        # open first, then by due date (undated last), then by title
        return sorted((view_task(t) for t in self._tasks.values()),
                      key=lambda t: (t.done, t.due or "9999", t.title.lower()))

    def has(self, tid: str) -> bool:
        return tid in self._tasks

    def add(self, task: Task) -> None:
        with self._mutation("task_add"):
            previous = self._tasks.get(task.id)
            if previous is not None:
                task = replace(task, done=previous.done)
                if previous.due_source == "owner":
                    task = replace(task, due=previous.due, due_source="owner",
                                   flags=tuple(f for f in task.flags if f not in DATE_FLAGS))
            self.log.effect()
            self.generation += 1
            self._tasks[task.id] = task
            self._save()
            self.log.append("task_add", task.doc_name, self.file, sha256=task.doc_sha256,
                            extra={"title": task.title, "due": task.due, "origin": task.origin})

    def set_due(self, tid: str, due: str | None) -> None:
        value = owner_date(due)
        with self._mutation("task_due_set"):
            task = self._tasks.get(tid)
            if task is None:
                raise KeyError(tid)
            changed = replace(task, due=value, due_source="owner",
                              flags=tuple(f for f in task.flags if f not in DATE_FLAGS))
            self.log.effect()
            self.generation += 1
            self._tasks[tid] = changed
            self._save()
            self.log.append("task_due_set", task.doc_name, self.file, sha256=task.doc_sha256,
                            extra={"title": task.title, "due": value, "due_source": "owner"})

    def set_done(self, tid: str, done: bool) -> None:
        with self._mutation("task_done" if done else "task_reopen"):
            t = self._tasks.get(tid)
            if t is None or t.done == done:
                return
            self.log.effect()
            self.generation += 1
            self._tasks[tid] = replace(t, done=done)
            self._save()
            self.log.append("task_done" if done else "task_reopen", t.doc_name, self.file, sha256=t.doc_sha256,
                            extra={"title": t.title})

    def remove(self, tid: str) -> None:
        with self._mutation("task_remove"):
            t = self._tasks.get(tid)
            if t is not None:
                self.log.effect()
                self.generation += 1
                del self._tasks[tid]
                self._save()
                self.log.append("task_remove", t.doc_name, self.file, sha256=t.doc_sha256, extra={"title": t.title})
