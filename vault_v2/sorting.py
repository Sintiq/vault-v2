"""Sorting — propose a card for every document in Staging.

Two proposers, always both:
- BASELINE: deterministic rules on the file name and text (no model). It is
  the floor and the comparison point.
- AGENT: the configured backend gets the Staging documents (name, size,
  and for TEXT documents a bounded excerpt) and answers with strict JSON.
  Anything it returns is validated by the same canonical rules as a human
  edit; an invalid field falls back to the baseline and is flagged.

The agent never sees Documents or Personal here; the only input is the list
this module builds from Staging. Nothing is written — the owner confirms
cards in the dialog, and only that writes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .agent import Backend, document_chat
from . import cards
from .cards import RECIPIENTS, UNCONFIRMED, Card, CardError, CardStore, content_kind, derive_year, grounded_issuer, canonical_issuer
from .reader import ReadRefused, agent_content_kind
from .file_access import iter_visible_files, visible_file, visible_directory
from .paths import VaultPaths
from .reading_coverage import coverage_warning

EXCERPT_CHARS = 6000

SORT_SYSTEM = (
    "You sort documents in a private personal vault. You see ONLY the documents listed "
    "in the user message (name, size, and for text documents an excerpt). Answer with "
    "STRICT JSON and nothing else: a JSON array with one object per document id:\n"
    '{"id": "<id>", "shelf": "<SHELF>", "topics": ["slug", ...], "issuer": "<name or UNCONFIRMED>", '
    '"recipients": ["<RECIPIENT>", ...], "reason": "<= 200 chars"}\n'
    f"Shelves: {', '.join(cards.SHELVES)}. Recipients: {', '.join(RECIPIENTS)} — DOCTOR for anything a "
    "physician needs (lab results, prescriptions, visit summaries, referrals, immunizations); "
    "ACCOUNTANT for money or taxes (invoices, receipts, wage and tax statements, bank statements); "
    "INSURANCE for policies and member letters; PERSONAL only when none of the others fit. "
    "Topics describe the subject, condition or program named in the text, not the document type "
    "(do not use invoice, prescription, referral, visit-summary as topics); lowercase Unicode "
    "letters or digits with single hyphens between words, including Cyrillic; no spaces or "
    "other punctuation. Use е instead of ё. Topics are "
    "at most 40 characters, 1 to 8 items. Issuer: the organisation that produced the document if it "
    "is explicitly named in the text; otherwise UNCONFIRMED — never invent one. Do not send a year. "
    "BINARY documents have no excerpt: propose from the name only and keep topics empty."
)


@dataclass(frozen=True)
class SortInput:
    id: str
    path: Path
    name: str
    kind: str
    size: int
    sha256: str
    excerpt: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class Proposal:
    doc: SortInput
    card: Card                # what will be confirmed if the owner accepts
    baseline: Card            # rules-only proposal, always present
    agent: Card | None        # model proposal, None if the agent gave nothing usable
    flags: tuple[str, ...]    # human-readable notes: disagreements, fallbacks

    @property
    def differs(self) -> bool:
        a = self.agent
        return a is not None and (a.shelf, a.topics, a.issuer, a.recipients) != (
            self.baseline.shelf, self.baseline.topics, self.baseline.issuer, self.baseline.recipients)


# -- inputs -------------------------------------------------------------------


def read_excerpt(path: Path, limit: int = EXCERPT_CHARS, *, paths: VaultPaths) -> str:
    return _read_excerpt_and_total(path, limit, paths=paths)[0]


def _read_excerpt_and_total(path: Path, limit: int, *, paths: VaultPaths) -> tuple[str, int | None]:
    path = visible_file(paths, path)
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return "", None
    text = raw.decode("utf-8", errors="replace")
    limit = max(1, min(int(limit), EXCERPT_CHARS))
    return text[:limit], len(text)


def collect_inputs(staging: Path, store: CardStore, only: list[Path] | None = None, reader=None) -> list[SortInput]:
    paths = VaultPaths(store.log.dir.parent)
    scope = visible_directory(paths, Path(staging).absolute())
    if not scope.is_relative_to(paths.staging.absolute()):
        raise ReadRefused("Sort is limited to Staging")
    if reader is not None:
        allowed = [reader.root / e.rel for e in reader.list_staging()]
        if only and any(p not in allowed for p in only):
            raise ReadRefused("Sort selection must contain visible Staging files only")
        files = only if only else allowed
    else:
        files = only if only else sorted(iter_visible_files(paths, scope))
    # Validate the whole selection before the first excerpt/hash is read.
    files = [visible_file(paths, Path(p).absolute()) for p in files]
    if any(not p.is_relative_to(scope) for p in files):
        raise ReadRefused("Sort selection must contain visible Staging files only")
    out: list[SortInput] = []
    for i, p in enumerate(files, 1):
        kind = content_kind(p)
        excerpt, warnings = "", ()
        size, digest = p.stat().st_size, None
        if reader is not None and agent_content_kind(p) in {"TEXT", "EXTRACTABLE"}:
            try:
                document = reader.read_document(p)
                excerpt = document.text
                size, digest = document.source_size, document.source_sha256
                warnings = document.warnings
                kind = "TEXT" if excerpt.strip() else "BINARY"
                if not excerpt.strip():
                    warnings += ("not read — no usable text was found; name only",)
            except ReadRefused as exc:
                kind = "BINARY"
                warnings = (f"not read — {exc}; name only",)
        elif kind == "TEXT":
            excerpt, total_chars = _read_excerpt_and_total(p, EXCERPT_CHARS, paths=paths)
            warning = coverage_warning(len(excerpt), total_chars)
            if warning:
                warnings = (warning,)
        out.append(SortInput(
            id=f"doc-{i:03d}", path=p, name=p.name, kind=kind, size=size,
            sha256=digest if digest is not None else store.hash_of(p),
            # with a reader the read is allowlisted and receipted (agent_read)
            excerpt=excerpt, warnings=warnings,
        ))
    return out


# -- baseline rules -----------------------------------------------------------

_RULES: tuple[tuple[tuple[str, ...], str, tuple[str, ...]], ...] = (
    (("prescription", "rx", "lab", "visit", "referral", "immuniz", "vaccin", "doctor", "clinic", "diagnos", "mri", "xray", "x-ray", "blood"), "HEALTH", ("DOCTOR",)),
    (("w-2", "w2", "1099", "tax", "irs", "return"), "TAXES", ("ACCOUNTANT",)),
    (("invoice", "receipt", "bank", "statement", "payroll", "wage", "salary", "loan", "mortgage"), "FINANCE", ("ACCOUNTANT",)),
    (("policy", "insurance", "coverage", "member", "claim", "premium"), "INSURANCE", ("INSURANCE",)),
    (("passport", "license", "licence", "id-card", "ssn", "birth", "certificate", "visa"), "IDENTITY", ("PERSONAL",)),
)


def baseline_card(doc: SortInput) -> Card:
    hay = f"{doc.name} {doc.excerpt[:2000]}".lower()
    shelf, recipients = "INBOX", ("PERSONAL",)
    if doc.kind == "BINARY" and Path(doc.name).suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic"}:
        shelf, recipients = "PHOTOS", ("PERSONAL",)
    else:
        for keys, sh, recs in _RULES:
            if any(k in hay for k in keys):
                shelf, recipients = sh, recs
                break
    words = [w for w in re.split(r"[^a-z0-9]+", Path(doc.name).stem.lower()) if len(w) > 2 and not w.isdigit()]
    topics = words[:4] if doc.kind == "TEXT" else []
    if doc.kind == "TEXT" and not topics:
        topics = ["untagged"]
    return Card.build(
        doc.sha256, doc.name, doc.kind, shelf=shelf, topics=topics, issuer=UNCONFIRMED,
        year=derive_year(doc.excerpt), recipients=recipients, origin="BASELINE",
        reason="rules on file name and text",
    )


# -- agent --------------------------------------------------------------------


def _extract_json_array(text: str) -> list:
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        raise ValueError("no JSON array in reply")
    return json.loads(text[start : end + 1])


def build_agent_prompt(docs: list[SortInput]) -> str:
    items = []
    for d in docs:
        item = {"id": d.id, "name": d.name, "kind": d.kind, "size": d.size}
        if d.kind == "TEXT":
            item["excerpt"] = d.excerpt
        items.append(item)
    return "Documents in Staging:\n" + json.dumps(items, ensure_ascii=False, indent=1) + "\n\nReply with the JSON array only."


def agent_cards(backend: Backend, docs: list[SortInput]) -> tuple[dict[str, Card], dict[str, str]]:
    """Ask the backend once for all documents. Returns (cards by id, errors by id)."""
    if not docs:
        return {}, {}
    reply = document_chat(backend, SORT_SYSTEM, [{"role": "user", "content": build_agent_prompt(docs)}],
                          lambda _s: None, documents=tuple(f"{d.id} — {d.name}" for d in docs),
                          reading_notes=tuple(f"{d.name}: {w}" for d in docs for w in d.warnings))
    by_id = {d.id: d for d in docs}
    cards: dict[str, Card] = {}
    errors: dict[str, str] = {}
    try:
        arr = _extract_json_array(reply)
    except ValueError as exc:
        return {}, {d.id: f"agent reply unusable: {exc}" for d in docs}
    for obj in arr if isinstance(arr, list) else []:
        if not isinstance(obj, dict):
            continue
        d = by_id.get(str(obj.get("id", "")))
        if d is None:
            continue
        try:
            cards[d.id] = Card.build(
                d.sha256, d.name, d.kind, shelf=obj.get("shelf"), topics=obj.get("topics", []),
                issuer=grounded_issuer(obj.get("issuer"), d.excerpt), year=derive_year(d.excerpt),
                recipients=obj.get("recipients", []), origin="AGENT", reason=obj.get("reason", ""),
            )
            if (cards[d.id].issuer == UNCONFIRMED
                    and canonical_issuer(obj.get("issuer")) != UNCONFIRMED):
                errors[d.id] = "issuer_not_in_text"
        except CardError as exc:
            errors[d.id] = f"agent proposal rejected: {exc}"
    for d in docs:
        if d.id not in cards and d.id not in errors:
            errors[d.id] = "agent gave no proposal"
    return cards, errors


# -- combine ------------------------------------------------------------------


def propose(docs: list[SortInput], backend: Backend | None) -> list[Proposal]:
    agent, errors = agent_cards(backend, docs) if backend is not None else ({}, {})
    out: list[Proposal] = []
    for d in docs:
        base = baseline_card(d)
        a = agent.get(d.id)
        flags: list[str] = list(d.warnings)
        if backend is None:
            flags.append("no agent — baseline only")
        elif a is None:
            flags.append(errors.get(d.id, "baseline used"))
        if a is not None and d.id in errors:
            flags.append(errors[d.id])
        chosen = a if a is not None else base
        p = Proposal(d, chosen, base, a, tuple(flags))
        if p.differs:
            flags.append("agent and baseline disagree")
            p = Proposal(d, chosen, base, a, tuple(flags))
        out.append(p)
    return out
