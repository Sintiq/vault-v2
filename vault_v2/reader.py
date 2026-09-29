"""StagingReader — the agent's only way to read anything (capsule layer L2).

An allowlist, not a filesystem: the agent side of the app can call exactly
these operations and nothing else.

    list_staging()            names, sizes, candidate kinds (no extraction)
    read_text(rel, limit)     bounded local text of ONE Staging document
    read_document(rel)       same text with its exact source digest
    card_for(rel)             the confirmed card of a Staging file

Every read_text leaves a receipt (op "agent_read") with the file hash and
how many characters were handed over, so the owner can always see what the
agent opened and when. Anything outside Staging, any path trick ("..",
hidden ancestors, links), and unsupported BINARY files are refused.
Absolute paths inside Staging are accepted for the desktop collector.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from hashlib import sha256

from .cards import Card, CardStore, content_kind
from .ops import VaultError, VaultOps
from .file_access import visible_file, iter_visible_files
from .receipts import sha256_file
from .reading_coverage import coverage_warning
from .text_extract import EXTRACTABLE_SUFFIXES

READ_LIMIT_CHARS = 6000


class ReadRefused(VaultError):
    pass


@dataclass(frozen=True)
class StagingEntry:
    rel: str
    size: int
    kind: str


@dataclass(frozen=True)
class ReadDocument:
    text: str
    source_sha256: str
    source_size: int
    warnings: tuple[str, ...] = ()
    total_chars: int | None = None


def agent_content_kind(path: Path) -> str:
    """Candidates only: no extraction on enumeration or the GUI thread."""
    return "EXTRACTABLE" if path.suffix.lower() in EXTRACTABLE_SUFFIXES else content_kind(path)


class StagingReader:
    def __init__(self, ops: VaultOps, cards: CardStore, purpose: str = "agent"):
        self.ops = ops
        self.cards = cards
        self.purpose = purpose
        self.root = ops.paths.staging.absolute()

    def _resolve(self, rel: str | Path) -> Path:
        p = Path(rel)
        target = p if p.is_absolute() else self.root / p
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise ReadRefused(f"outside Staging: {rel}") from exc
        try:
            return visible_file(self.ops.paths, target)
        except VaultError as exc:
            raise ReadRefused(str(exc)) from exc

    # -- allowlisted operations ----------------------------------------------

    def list_staging(self) -> list[StagingEntry]:
        out = []
        try:
            for p in iter_visible_files(self.ops.paths, self.root):
                out.append(StagingEntry(p.relative_to(self.root).as_posix(), p.stat().st_size, agent_content_kind(p)))
        except VaultError as exc:
            raise ReadRefused(str(exc)) from exc
        return sorted(out, key=lambda entry: entry.rel)

    def read_text(self, rel: str | Path, limit: int = READ_LIMIT_CHARS) -> str:
        return self.read_document(rel, limit).text

    def read_document(self, rel: str | Path, limit: int = READ_LIMIT_CHARS) -> ReadDocument:
        """Return bounded text and the exact source digest used for that text.

        Extraction holds no root guard. The final source check and receipt
        share one short guard so cooperating edits cannot split the handoff.
        """
        target = self._resolve(rel)
        kind = agent_content_kind(target)
        if kind not in {"TEXT", "EXTRACTABLE"}:
            raise ReadRefused(f"not a text document: {target.name}")
        limit = max(1, min(int(limit), READ_LIMIT_CHARS))
        warnings = ()
        total_chars = None
        if kind == "EXTRACTABLE":
            from .text_cache import DocumentTextCache

            try:
                snapshot = DocumentTextCache(self.ops.paths, self.ops.log).read_snapshot(target)
            except VaultError as exc:
                if type(exc) is not VaultError:
                    raise
                raise ReadRefused(str(exc)) from exc
            text = snapshot.text[:limit]
            total_chars = len(snapshot.text)
            digest, size = snapshot.source_sha256, snapshot.source_size
            warnings = tuple(snapshot.extraction_metadata.get("warnings", ()))
        else:
            raw = target.read_bytes()
            full_text = raw.decode("utf-8", errors="replace")
            total_chars = len(full_text)
            text = full_text[:limit]
            digest, size = sha256(raw).hexdigest(), len(raw)
        coverage = coverage_warning(len(text), total_chars, extracted=kind == "EXTRACTABLE")
        if coverage:
            warnings += (coverage,)
        with self.ops.log.write("agent_read"):
            target = self._resolve(rel)
            if sha256_file(target) != digest:
                raise ReadRefused("document changed while reading — try again")
            self.ops.log.append(
                "agent_read", target, self.purpose, sha256=digest, size=size,
                extra={"chars": len(text), "purpose": self.purpose,
                       "read_chars": len(text), "total_chars": total_chars,
                       "truncated": total_chars is not None and len(text) < total_chars,
                       "character_basis": "extracted" if kind == "EXTRACTABLE" else "source"},
            )
            return ReadDocument(text, digest, size, warnings, total_chars)

    def card_for(self, rel: str | Path) -> Card | None:
        card = self.cards.for_path(self._resolve(rel))
        return card if card and card.confirmed else None
