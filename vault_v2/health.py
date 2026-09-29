"""Health — a timeline of what the documents say, nothing more.

Every entry must quote the document it came from, exactly as for tasks:
a line the text does not contain is dropped, not shown. The pane groups
entries by year and by subject, and says plainly that this is a reading of
the owner's own papers, not a medical record and not advice.

No interpretation is allowed: "the document states X on date D", never
"you have X". The agent is told so, and anything that looks like advice is
still just a quoted line — the owner sees the source for each one.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from datetime import date
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from threading import RLock

from .agent import Backend, document_chat
from .dates import checked_saved_date, owner_date, resolve_date, DATE_FLAGS
from .reader import ReadRefused, StagingReader
from .receipts import ReceiptLog
from .document_budget import DocumentOutputIncomplete

KINDS = ("VISIT", "DIAGNOSIS", "MEDICATION", "TEST", "PROCEDURE", "VACCINATION", "OTHER")
LABEL_MAX = 140
QUOTE_MAX = 200
ENTRIES_PER_DOCUMENT = 12
HEALTH_LIMIT_NOTE = "Health lists at most 12 entries per document; a long record may have more"

HEALTH_SYSTEM = (
    "You read personal health documents and extract ONLY what the text itself states, as a "
    "timeline. You are not a clinician: never interpret, never advise, never add a diagnosis "
    "the text does not name. Answer with STRICT JSON and nothing else — an array of objects:\n"
    '{"doc": "<doc id>", "date": "YYYY-MM-DD or YYYY-MM or YYYY or null", "kind": "<KIND>", '
    '"label": "<what the document says, <= 140 chars>", "quote": "<exact words copied from the document>"}\n'
    f"KIND is one of: {', '.join(KINDS)}. The quote must be copied character for character from the "
    "document text. If a document states nothing about health, return nothing for it. An empty array "
    "is a valid answer. Do not summarise across documents; one object per stated fact. "
    "Return at most 12 entries per document; choose the main stated facts. "
    "Each quote must use at most 200 Unicode characters."
)

_BASELINE_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("VISIT", ("visit", "appointment", "consultation", "follow-up", "seen by")),
    ("DIAGNOSIS", ("diagnosis", "diagnosed", "assessment", "impression")),
    ("MEDICATION", ("prescription", "prescribed", "mg", "tablet", "refill", "dose")),
    ("TEST", ("lab", "test", "result", "panel", "mri", "x-ray", "xray", "ultrasound", "blood")),
    ("PROCEDURE", ("procedure", "surgery", "operation", "biopsy")),
    ("VACCINATION", ("vaccin", "immuniz", "booster")),
)


@dataclass(frozen=True)
class Entry:
    id: str
    doc_sha256: str
    doc_name: str
    date: str | None       # ISO date, YYYY-MM or YYYY
    kind: str
    label: str
    quote: str
    origin: str            # AGENT | BASELINE
    due_source: str = "none"  # quote | none | owner
    flags: tuple[str, ...] = ()

    @property
    def year(self) -> str:
        return self.date[:4] if self.date else "undated"

    @property
    def sort_key(self) -> tuple:
        return (self.date or "0000", self.kind, self.label.lower())


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def view_entry(entry: Entry) -> Entry:
    """Validate the date for display without migrating saved timeline bytes."""
    checked = checked_saved_date(entry.date, entry.quote, entry.due_source, entry.flags, allow_partial=True)
    return replace(entry, date=checked.value, due_source=checked.due_source, flags=checked.flags)


def entry_id(sha256: str, label: str) -> str:
    return f"{sha256[:12]}:{_norm(label)[:60]}"


# -- the watch -----------------------------------------------------------------
#
# Readings arrive from the phone's Watch tab (Health Connect), one day at a
# time, only when the owner presses "Add to Health". They are not a document,
# so the source named on every line is the watch itself, and the date is the
# day that was measured — "device", never a quote to re-verify. One entry per
# metric per day: sending the same day again replaces, never duplicates.

WATCH_FALLBACK_SOURCE = "Health Connect (source not verified)"
WATCH_KIND = "WATCH"
WATCH_ORIGIN = "WATCH"
SOURCE_MAX = 80
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_CLOCK = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


def watch_sha256(day: str, metric: str) -> str:
    """A stable stand-in for a document hash: the same day and metric map to the same entry."""
    return hashlib.sha256(f"watch:{day}:{metric}".encode("utf-8")).hexdigest()


def _source(readings: dict, metric: str) -> str:
    """The device or app Health Connect named for this metric; never a claim the phone did not make."""
    value = readings.get(f"{metric}_source")
    if not isinstance(value, str):
        return WATCH_FALLBACK_SOURCE
    clean = " ".join(value.split())
    clean = "".join(ch for ch in clean if ch.isprintable())[:SOURCE_MAX].strip()
    return clean or WATCH_FALLBACK_SOURCE


def _reading(readings: dict, key: str, lo: float, hi: float) -> float | None:
    value = readings.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a number")
    if not lo <= value <= hi:
        raise ValueError(f"{key} out of range ({lo}–{hi})")
    return value


def watch_entries(day: object, readings: object) -> list[Entry]:
    """One day of watch readings as timeline lines. Absent metrics are simply not lines."""
    if not isinstance(day, str) or not _DAY.fullmatch(day):
        raise ValueError("day must be YYYY-MM-DD")
    try:
        date.fromisoformat(day)
    except ValueError as exc:
        raise ValueError("day must be a real calendar date") from exc
    if not isinstance(readings, dict):
        raise ValueError("readings must be an object")

    lines: list[tuple[str, str, str]] = []
    steps = _reading(readings, "steps", 0, 200_000)
    if steps is not None:
        lines.append(("steps", f"Steps {day}", f"{int(steps)} steps"))
    low, avg, high = (_reading(readings, k, 20, 300) for k in ("heart_min", "heart_avg", "heart_max"))
    if low is not None and high is not None:
        if low > high or (avg is not None and not low <= avg <= high):
            raise ValueError("heart rate range must be min <= average <= max")
        quote = f"heart rate {int(low)}–{int(high)} bpm" + (f", average {int(avg)}" if avg is not None else "")
        lines.append(("heart", f"Heart rate {day}", quote))
    minutes = _reading(readings, "sleep_minutes", 1, 24 * 60)
    if minutes is not None:
        hours, rest = divmod(int(minutes), 60)
        quote = f"sleep {hours} h {rest} min" if hours else f"sleep {rest} min"
        woke = readings.get("sleep_end")
        if isinstance(woke, str) and _CLOCK.fullmatch(woke):
            quote += f", woke {woke}"
        lines.append(("sleep", f"Sleep {day}", quote))
    oxygen = _reading(readings, "oxygen", 50, 100)
    if oxygen is not None:
        lines.append(("oxygen", f"Blood oxygen {day}", f"blood oxygen {oxygen:.0f}%"))

    return [Entry(entry_id(watch_sha256(day, metric), label), watch_sha256(day, metric), _source(readings, metric),
                  day, WATCH_KIND, label, quote, WATCH_ORIGIN, "device")
            for metric, label, quote in lines]


@dataclass(frozen=True)
class HealthDoc:
    id: str
    name: str
    sha256: str
    text: str


def collect_docs(reader: StagingReader, notes: list[str] | None = None) -> list[HealthDoc]:
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
        docs.append(HealthDoc(f"doc-{i:03d}", Path(e.rel).name, document.source_sha256, document.text))
    return docs


def baseline_entries(docs: list[HealthDoc]) -> list[Entry]:
    out: list[Entry] = []
    for d in docs:
        for raw in re.split(r"[\r\n]+|(?<=\.)\s+", d.text):
            line = raw.strip()
            if len(line) < 8:
                continue
            low = line.lower()
            kind = next((k for k, hints in _BASELINE_HINTS if any(h in low for h in hints)), None)
            if kind is None:
                continue
            quote = line[:QUOTE_MAX]
            resolved = resolve_date(quote, allow_partial=True)
            out.append(Entry(entry_id(d.sha256, quote), d.sha256, d.name, resolved.value,
                             kind, quote[:LABEL_MAX], quote, "BASELINE", resolved.due_source, resolved.flags))
    return out


def agent_entries(backend: Backend, docs: list[HealthDoc], *,
                  reading_notes: tuple[str, ...] = ()) -> tuple[list[Entry], list[str]]:
    if not docs:
        return [], []
    payload = [{"doc": d.id, "name": d.name, "text": d.text} for d in docs]
    prompt = "Documents:\n" + json.dumps(payload, ensure_ascii=False, indent=1) + "\n\nReply with the JSON array only."
    reply = document_chat(backend, HEALTH_SYSTEM, [{"role": "user", "content": prompt}], lambda _s: None,
                          json_shape="health", document_count=len(docs),
                          documents=tuple(f"{d.id} — {d.name}" for d in docs), reading_notes=reading_notes)
    start, end = reply.find("["), reply.rfind("]")
    if start < 0 or end <= start:
        return [], ["agent reply unusable: no JSON array"]
    try:
        arr = json.loads(reply[start : end + 1])
    except ValueError as exc:
        return [], [f"agent reply unusable: {exc}"]
    by_id = {d.id: d for d in docs}
    counts: dict[str, int] = {}
    for obj in arr if isinstance(arr, list) else []:
        if not isinstance(obj, dict):
            continue
        doc_id = str(obj.get("doc", ""))
        counts[doc_id] = counts.get(doc_id, 0) + 1
        if counts[doc_id] > ENTRIES_PER_DOCUMENT:
            raise DocumentOutputIncomplete(
                f"Health response refused: {doc_id} exceeds 12 entries; no proposals were published.",
                documents=tuple(f"{d.id} — {d.name}" for d in docs), reading_notes=reading_notes,
            )
        if len(str(obj.get("quote", ""))) > QUOTE_MAX:
            raise DocumentOutputIncomplete(
                "Health response refused: a quote exceeds 200 characters; no proposals were published.",
                documents=tuple(f"{d.id} — {d.name}" for d in docs), reading_notes=reading_notes,
            )
    entries, dropped = [], []
    for obj in arr if isinstance(arr, list) else []:
        if not isinstance(obj, dict):
            continue
        d = by_id.get(str(obj.get("doc", "")))
        label = str(obj.get("label", "")).strip()[:LABEL_MAX]
        quote = str(obj.get("quote", "")).strip()
        kind = str(obj.get("kind", "")).strip().upper()
        if d is None or not label:
            dropped.append("dropped: unknown document or empty label")
            continue
        if len(quote) < 4 or _norm(quote) not in _norm(d.text):
            dropped.append(f"dropped (quote not in document): {label}")
            continue
        resolved = resolve_date(quote, obj.get("date"), allow_partial=True)
        entries.append(Entry(entry_id(d.sha256, label), d.sha256, d.name, resolved.value,
                             kind if kind in KINDS else "OTHER", label, quote, "AGENT",
                             resolved.due_source, resolved.flags))
    return entries, dropped


def propose(reader: StagingReader, backend: Backend | None) -> tuple[list[Entry], list[str]]:
    notes: list[str] = [HEALTH_LIMIT_NOTE]
    docs = collect_docs(reader, notes)
    if backend is None:
        entries = []
        notes.append("no agent — keyword baseline only")
    else:
        entries, agent_notes = agent_entries(backend, docs, reading_notes=tuple(notes))
        notes.extend(agent_notes)
    quotes = {_norm(e.quote) for e in entries}
    ids = {e.id for e in entries}
    counts: dict[tuple[str, str], int] = {}
    for entry in entries:
        source = (entry.doc_sha256, entry.doc_name)
        counts[source] = counts.get(source, 0) + 1
    baseline_limited = False
    for b in baseline_entries(docs):
        if b.id not in ids and _norm(b.quote) not in quotes:
            source = (b.doc_sha256, b.doc_name)
            if counts.get(source, 0) >= ENTRIES_PER_DOCUMENT:
                baseline_limited = True
                continue
            entries.append(b)
            counts[source] = counts.get(source, 0) + 1
    if baseline_limited:
        notes.append("Baseline additions limited by the same 12-entry-per-document cap.")
    return sorted(entries, key=lambda e: e.sort_key), notes


class HealthStore:
    """Lock order: root write guard -> proposal lock -> store mutation lock."""

    SCHEMA = "vault-v2-health@1"

    def __init__(self, health_dir: Path, log: ReceiptLog):
        self.dir = Path(health_dir)
        self.file = self.dir / "timeline.json"
        self.log = log
        self.generation = 0
        self.mutation_lock = RLock()
        self._entries: dict[str, Entry] = {}
        self._load()

    def _load(self) -> None:
        self._entries = {}
        if self.file.exists():
            data = json.loads(self.file.read_text(encoding="utf-8"))
            self._entries = {k: Entry(**{**v, "flags": tuple(v.get("flags", ()))}) for k, v in data.get("entries", {}).items()}

    def _save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        body = {"schema": self.SCHEMA, "entries": {k: asdict(e) for k, e in sorted(self._entries.items())}}
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

    def all(self) -> list[Entry]:
        return sorted((view_entry(e) for e in self._entries.values()), key=lambda e: e.sort_key, reverse=True)

    def has(self, eid: str) -> bool:
        return eid in self._entries

    def add(self, entry: Entry) -> None:
        with self._mutation("health_add"):
            previous = self._entries.get(entry.id)
            if previous is not None and previous.due_source == "owner":
                entry = replace(entry, date=previous.date, due_source="owner",
                                flags=tuple(f for f in entry.flags if f not in DATE_FLAGS))
            self.log.effect()
            self.generation += 1
            self._entries[entry.id] = entry
            self._save()
            self.log.append("health_add", entry.doc_name, self.file, sha256=entry.doc_sha256,
                            extra={"kind": entry.kind, "date": entry.date, "label": entry.label, "origin": entry.origin})

    def add_watch(self, day: object, readings: object) -> list[Entry]:
        """The phone's Watch tab, on the owner's press.

        One guard and one save for the whole day, so another writer cannot slip
        in between two of its lines and leave a half-written day behind a plain
        "busy"; still one receipt per line, like any add.
        """
        entries = watch_entries(day, readings)
        if not entries:
            return []
        stored: list[Entry] = []
        with self._mutation("health_add"):
            self.log.effect()
            self.generation += 1
            for entry in entries:
                previous = self._entries.get(entry.id)
                if previous is not None and previous.due_source == "owner":
                    entry = replace(entry, date=previous.date, due_source="owner",
                                    flags=tuple(f for f in entry.flags if f not in DATE_FLAGS))
                self._entries[entry.id] = entry
                stored.append(entry)
            self._save()
            for entry in stored:
                self.log.append("health_add", entry.doc_name, self.file, sha256=entry.doc_sha256,
                                extra={"kind": entry.kind, "date": entry.date, "label": entry.label,
                                       "origin": entry.origin})
        return stored

    def set_date(self, eid: str, value: str | None) -> None:
        value = owner_date(value)
        with self._mutation("health_date_set"):
            entry = self._entries.get(eid)
            if entry is None:
                raise KeyError(eid)
            changed = replace(entry, date=value, due_source="owner",
                              flags=tuple(f for f in entry.flags if f not in DATE_FLAGS))
            self.log.effect()
            self.generation += 1
            self._entries[eid] = changed
            self._save()
            self.log.append("health_date_set", entry.doc_name, self.file, sha256=entry.doc_sha256,
                            extra={"label": entry.label, "date": value, "due_source": "owner"})

    def remove(self, eid: str) -> None:
        with self._mutation("health_remove"):
            e = self._entries.get(eid)
            if e is not None:
                self.log.effect()
                self.generation += 1
                del self._entries[eid]
                self._save()
                self.log.append("health_remove", e.doc_name, self.file, sha256=e.doc_sha256, extra={"label": e.label})

    def by_year(self) -> list[tuple[str, list[Entry]]]:
        groups: dict[str, list[Entry]] = {}
        for e in self.all():
            groups.setdefault(e.year, []).append(e)
        # newest year first, undated last
        return sorted(groups.items(), key=lambda kv: ("" if kv[0] == "undated" else kv[0]), reverse=True)

    def summary(self) -> str:
        entries = self.all()
        if not entries:
            return "nothing recorded yet"
        kinds: dict[str, int] = {}
        for e in entries:
            kinds[e.kind] = kinds.get(e.kind, 0) + 1
        parts = [f"{n} {k.lower()}" for k, n in sorted(kinds.items(), key=lambda kv: -kv[1])]
        dated = [e.date for e in entries if e.date]
        span = f" · {min(dated)} … {max(dated)}" if dated else ""
        return f"{len(entries)} entries: " + ", ".join(parts) + span
