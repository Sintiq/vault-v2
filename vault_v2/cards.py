"""Cards — what the vault knows about a document, keyed by its bytes.

A card is bound to the SHA-256 of the file, not to its path: copy a file
into another pane or rename it and the card follows. Vocabulary and limits
use the following local card contract:

    shelf       one of SHELVES
    topics      1-8 Unicode alphanumeric slugs, casefolded, ё -> е,
                hyphenated, <= 40 chars; BINARY docs may have none (UNTAGGED)
    issuer      free text <= 120 chars or "UNCONFIRMED" — never invented
    year        int derived from document text by the runtime, or None
    recipients  subset of RECIPIENTS
    origin      AGENT | BASELINE | HUMAN
    confirmed   only the owner sets this; the agent only proposes

Storage: <root>/.cards/cards.json, one JSON object; every confirm/edit
leaves a receipt.
"""

from __future__ import annotations

import json
import re
import stat
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from threading import RLock

from .receipts import ReceiptLog, sha256_file
from .errors import VaultError
from .file_access import visible_directory, visible_file
from .paths import VaultPaths

DEFAULT_SHELVES = ("INBOX", "HEALTH", "TAXES", "FINANCE", "INSURANCE", "IDENTITY", "PHOTOS")
_SHELF_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,23}$")

# The owner's shelves: the defaults plus whatever vault.json adds under
# "shelves". Kept as a mutable tuple behind a function so every reader —
# card validation, the sort dialog, the agent's prompt — sees the same list
# the moment it is loaded, without threading a settings object through all
# of them. Names are upper case, no spaces, so a shelf reads like a label
# and never like a sentence.
SHELVES: tuple[str, ...] = DEFAULT_SHELVES


def canonical_shelf_name(name: object) -> str:
    s = str(name).strip().upper().replace(" ", "_").replace("-", "_")
    if not _SHELF_NAME.match(s):
        raise CardError(f"bad shelf name: {name!r} — letters, digits and _ only, 2 to 24 characters")
    return s


def load_shelves(settings: dict | None) -> tuple[str, ...]:
    """Apply the owner's shelves from vault.json. Defaults always stay."""
    global SHELVES
    extra = (settings or {}).get("shelves", [])
    if isinstance(extra, str):
        extra = [x for x in re.split("[,;]", extra.replace(chr(10), ",")) if x.strip()]
    names = list(DEFAULT_SHELVES)
    for raw in extra if isinstance(extra, (list, tuple)) else []:
        s = canonical_shelf_name(raw)
        if s not in names:
            names.append(s)
    SHELVES = tuple(names)
    return SHELVES
RECIPIENTS = ("DOCTOR", "ACCOUNTANT", "INSURANCE", "PERSONAL")
TOPIC_MAX_CHARS = 40
TOPICS_MAX_ITEMS = 8
ISSUER_MAX_CHARS = 120
REASON_MAX_CHARS = 200
UNCONFIRMED = "UNCONFIRMED"

TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".log"}
BINARY_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".docx", ".xlsx", ".heic"}

_YEAR = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b")


class CardError(ValueError):
    pass


def content_kind(path: Path) -> str:
    s = Path(path).suffix.lower()
    if s in TEXT_SUFFIXES:
        return "TEXT"
    return "BINARY"


def canonical_topics(topics: object, *, allow_empty: bool = False) -> tuple[str, ...]:
    if topics is None:
        topics = []
    if isinstance(topics, str):
        topics = [t for t in re.split(r"[,\n;]", topics)]
    if not isinstance(topics, (list, tuple)):
        raise CardError("topics must be a list")
    out: list[str] = []
    for raw in topics:
        t = str(raw).strip().casefold().replace("ё", "е")
        t = t.replace("_", "-").replace(" ", "-")
        t = "".join(character for character in t if character.isalnum() or character == "-")
        t = re.sub(r"-{2,}", "-", t).strip("-")
        if not t:
            continue
        if len(t) > TOPIC_MAX_CHARS:
            raise CardError(f"bad topic: {raw!r}")
        if t not in out:
            out.append(t)
    if not out and not allow_empty:
        raise CardError("at least one topic is required")
    if len(out) > TOPICS_MAX_ITEMS:
        raise CardError(f"at most {TOPICS_MAX_ITEMS} topics")
    return tuple(out)


def canonical_issuer(issuer: object) -> str:
    if issuer is None:
        return UNCONFIRMED
    s = str(issuer).strip()
    if not s or s.upper() == UNCONFIRMED:
        return UNCONFIRMED
    if len(s) > ISSUER_MAX_CHARS:
        raise CardError("issuer too long")
    return s


def canonical_year(year: object) -> int | None:
    if year in (None, "", "UNKNOWN", "unknown"):
        return None
    try:
        y = int(str(year).strip())
    except ValueError as exc:
        raise CardError(f"bad year: {year!r}") from exc
    if not 1950 <= y <= 2049:
        raise CardError(f"year out of range: {y}")
    return y


def grounded_issuer(issuer: object, text: str) -> str:
    """Keep a proposed issuer only when literally present in this document."""
    value = canonical_issuer(issuer)
    if value == UNCONFIRMED or value.casefold() in (text or "").casefold():
        return value
    return UNCONFIRMED


def derive_year(text: str, name: str = "") -> int | None:
    """Latest plausible year in document text; a filename is never evidence.

    The optional name argument remains accepted for callers from older builds.
    """
    found = [int(m) for m in _YEAR.findall(text or "")]
    return max(found) if found else None


@dataclass(frozen=True)
class Card:
    sha256: str
    name: str
    kind: str
    shelf: str
    topics: tuple[str, ...]
    issuer: str
    year: int | None
    recipients: tuple[str, ...]
    origin: str
    reason: str = ""
    confirmed: bool = False

    @staticmethod
    def build(
        sha256: str, name: str, kind: str, *, shelf: object, topics: object, issuer: object = None,
        year: object = None, recipients: object = (), origin: str = "HUMAN", reason: object = "", confirmed: bool = False,
    ) -> "Card":
        if shelf not in SHELVES:
            raise CardError(f"shelf must be one of {', '.join(SHELVES)}")
        recs = tuple(dict.fromkeys(str(r).upper() for r in (recipients or ())))
        for r in recs:
            if r not in RECIPIENTS:
                raise CardError(f"recipient must be one of {', '.join(RECIPIENTS)}")
        if origin not in ("AGENT", "BASELINE", "HUMAN"):
            raise CardError("bad origin")
        return Card(
            sha256=sha256, name=name, kind=kind, shelf=str(shelf),
            topics=canonical_topics(topics, allow_empty=(kind == "BINARY")),
            issuer=canonical_issuer(issuer), year=canonical_year(year), recipients=recs,
            origin=origin, reason=str(reason or "")[:REASON_MAX_CHARS], confirmed=confirmed,
        )

    def to_json(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_json(d: dict) -> "Card":
        return Card(
            sha256=d["sha256"], name=d["name"], kind=d["kind"], shelf=d["shelf"],
            topics=tuple(d.get("topics", ())), issuer=d.get("issuer", UNCONFIRMED), year=d.get("year"),
            recipients=tuple(d.get("recipients", ())), origin=d.get("origin", "HUMAN"),
            reason=d.get("reason", ""), confirmed=bool(d.get("confirmed", False)),
        )


class CardStore:
    """Lock order: root write guard -> proposal lock -> store mutation lock."""

    SCHEMA = "vault-v2-cards@1"

    def __init__(self, cards_dir: Path, log: ReceiptLog):
        self.dir = Path(cards_dir)
        self.file = self.dir / "cards.json"
        self.log = log
        self.generation = 0
        self.mutation_lock = RLock()
        self._cards: dict[str, Card] = {}
        self._path_cache: dict[tuple[str, int, int], str] = {}
        self._load()

    # -- persistence ----------------------------------------------------------

    def _load(self) -> None:
        self._cards = {}
        if not self.file.exists():
            return
        data = json.loads(self.file.read_text(encoding="utf-8"))
        self._cards = {k: Card.from_json(v) for k, v in data.get("cards", {}).items()}

    def _save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        body = {"schema": self.SCHEMA, "cards": {k: c.to_json() for k, c in sorted(self._cards.items())}}
        tmp = self.file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(body, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(self.file)

    @contextmanager
    def _mutation(self, label: str):
        with self.log.write(label), self.mutation_lock:
            try:
                yield
            except Exception:
                self._load()
                raise

    # -- lookup ---------------------------------------------------------------

    def hash_of(self, path: Path) -> str:
        p = visible_file(VaultPaths(self.log.dir.parent), Path(path).absolute())
        st = p.stat()
        key = (str(p), st.st_size, int(st.st_mtime_ns))
        h = self._path_cache.get(key)
        if h is None:
            h = sha256_file(p)
            self._path_cache[key] = h
        return h

    def get(self, sha256: str) -> Card | None:
        return self._cards.get(sha256)

    def for_path(self, path: Path) -> Card | None:
        try:
            return self._cards.get(self.hash_of(path))
        except VaultError as exc:
            if isinstance(exc.__cause__, OSError):
                return None
            raise

    def all(self) -> list[Card]:
        return list(self._cards.values())

    # -- writes (owner decisions) ---------------------------------------------

    def _shelf_file(self, path: Path) -> Path:
        """A visible regular file in a pane; inspect links before resolution."""
        try:
            return visible_file(VaultPaths(self.log.dir.parent), Path(path).absolute())
        except VaultError as exc:
            if isinstance(exc.__cause__, OSError):
                raise exc.__cause__
            raise CardError(str(exc).lower()) from exc

    def set_shelf(self, path: Path, shelf: str) -> Card:
        """Confirm the owner's shelf choice, creating a minimal card if absent."""
        with self._mutation("card_confirm"):
            if shelf not in SHELVES:
                raise CardError(f"shelf must be one of {', '.join(SHELVES)}")
            source = self._shelf_file(path)
            digest = self.hash_of(source)
            existing = self.get(digest)
            if existing is None:
                # A shelf-only choice does not claim topics, issuer or year.
                card = Card(digest, source.name, content_kind(source), shelf, (),
                            UNCONFIRMED, None, (), "HUMAN")
            else:
                card = replace(existing, name=source.name, shelf=shelf, origin="HUMAN")
            return self.confirm(card, source)

    def forget_missing(self, roots: list[Path], note: str = "") -> int:
        """Forget absent cards only after a complete, trustworthy pane scan.

        A hidden, linked or unreadable entry makes absence unprovable. Refuse
        the entire cleanup, rather than treating filtered entries as missing.
        """
        with self._mutation("card_forget_missing"):
            present = set()
            paths = VaultPaths(self.log.dir.parent)
            try:
                pending = [visible_directory(paths, Path(root).absolute()) for root in roots]
                while pending:
                    directory = visible_directory(paths, pending.pop())
                    for p in directory.iterdir():
                        if stat.S_ISDIR(p.lstat().st_mode):
                            pending.append(visible_directory(paths, p))
                        else:
                            present.add(self.hash_of(p))
            except (OSError, VaultError) as exc:
                raise VaultError("Card cleanup refused: hidden, linked or unreadable files may still have cards.") from exc
            gone = [s for s in list(self._cards) if s not in present]
            for s in gone:
                self.forget(s, note)
            return len(gone)

    def propose(self, card: Card, source: Path | str = "") -> Card:
        """Store a proposal as NOT confirmed. Never overwrites a confirmed card."""
        with self._mutation("card_propose"):
            # A publication also invalidates old batches when the card is confirmed.
            self.log.effect()
            self.generation += 1
            existing = self._cards.get(card.sha256)
            if existing is not None and existing.confirmed:
                return existing
            draft = replace(card, confirmed=False)
            self._cards[draft.sha256] = draft
            self._save()
            self.log.append(
                "card_propose", source or draft.name, self.file, sha256=draft.sha256,
                extra={"shelf": draft.shelf, "topics": list(draft.topics), "origin": draft.origin},
            )
            return draft

    def confirm(self, card: Card, source: Path | str = "") -> Card:
        """Store a card as confirmed by the owner. Always a receipt."""
        with self._mutation("card_confirm"):
            final = replace(card, confirmed=True)
            self.log.effect()
            self.generation += 1
            self._cards[final.sha256] = final
            self._save()
            self.log.append(
                "card_confirm", source or final.name, self.file, sha256=final.sha256,
                extra={"shelf": final.shelf, "topics": list(final.topics), "issuer": final.issuer,
                       "year": final.year, "recipients": list(final.recipients), "origin": final.origin},
            )
            return final

    def forget(self, sha256: str, source: Path | str = "") -> None:
        with self._mutation("card_forget"):
            if sha256 in self._cards:
                self.log.effect()
                self.generation += 1
                del self._cards[sha256]
                self._save()
                self.log.append("card_forget", source or sha256, self.file, sha256=sha256)
