"""Deterministic date evidence and calendar arithmetic. No model calculations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

DATE_FLAGS = frozenset({"date_not_in_quote", "date_from_quote", "ambiguous_date",
                        "several_dates", "relative_deadline", "not_verified"})
_ISO = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_PARTIAL = re.compile(r"[0-9]{4}(?:-[0-9]{2})?\Z")
_MONTHS = {name: i for i, names in enumerate((
    ("january", "jan", "января"), ("february", "feb", "февраля"),
    ("march", "mar", "марта"), ("april", "apr", "апреля"),
    ("may", "мая"), ("june", "jun", "июня"),
    ("july", "jul", "июля"), ("august", "aug", "августа"),
    ("september", "sep", "sept", "сентября"), ("october", "oct", "октября"),
    ("november", "nov", "ноября"), ("december", "dec", "декабря")), 1)
    for name in names}
_MONTH_WORDS = "|".join(sorted(_MONTHS, key=len, reverse=True))
_FULL = (
    (re.compile(r"(?<![0-9])([0-9]{4})-([0-9]{2})-([0-9]{2})(?![0-9])"), "iso"),
    (re.compile(r"(?<![0-9])([0-9]{1,2})/([0-9]{1,2})/([0-9]{4})(?![0-9])"), "us"),
    (re.compile(r"(?<![0-9])([0-9]{1,2})\.([0-9]{1,2})\.([0-9]{4})(?![0-9])"), "eu"),
    (re.compile(r"\b(" + _MONTH_WORDS + r")\.?\s+([0-9]{1,2}),?\s+([0-9]{4})\b", re.I), "mdy"),
    (re.compile(r"\b([0-9]{1,2})\s+(" + _MONTH_WORDS + r")\.?\s+([0-9]{4})\b", re.I), "dmy"),
)
_PARTIAL_TEXT = re.compile(r"(?<![0-9-])([0-9]{4})(?:-([0-9]{2}))?(?![0-9-])")
_RELATIVE = re.compile(
    r"\b(?:within|in)\s+(?:[0-9]+|a|an|one|two|three|four|five|six|seven|eight|nine|ten)"
    r"\s+(?:business\s+|calendar\s+)?(?:days?|weeks?|months?|years?)\b"
    r"|\b(?:в\s+течение|через)\s+[0-9]+\s+(?:рабочих\s+|календарных\s+)?"
    r"(?:день|дня|дней|недел[юьи]|месяц(?:а|ев)?|год(?:а|ов)?|лет)\b", re.I)


@dataclass(frozen=True)
class DateEvidence:
    value: str
    start: int
    end: int
    ambiguous: bool = False


@dataclass(frozen=True)
class GroundedDate:
    value: str | None
    due_source: str
    flags: tuple[str, ...] = ()


def clean_date(value: object, *, allow_partial: bool = False) -> str | None:
    """Canonical ISO input only; invalid/calendar-impossible inputs are absent."""
    if not isinstance(value, str):
        return None
    if _ISO.fullmatch(value):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            return None
    if allow_partial and _PARTIAL.fullmatch(value):
        year = int(value[:4])
        if 1 <= year <= 9999 and (len(value) == 4 or 1 <= int(value[5:]) <= 12):
            return value
    return None


def owner_date(value: object) -> str | None:
    """Validate an explicit owner edit; null deliberately clears a date."""
    if value is None:
        return None
    result = clean_date(value)
    if result is None:
        raise ValueError("date must be YYYY-MM-DD or null")
    return result


def find_dates(text: str, *, allow_partial: bool = False) -> tuple[DateEvidence, ...]:
    """Parse literal dates; invalid full dates never degrade into valid years."""
    found: list[DateEvidence] = []
    occupied: list[tuple[int, int]] = []
    for pattern, order in _FULL:
        for match in pattern.finditer(text):
            occupied.append(match.span())
            a, b, c = match.groups()
            ambiguous = False
            if order == "iso":
                year, month, day = int(a), int(b), int(c)
            elif order == "us":
                year, month, day = int(c), int(a), int(b)
                ambiguous = month <= 12 and day <= 12
            elif order == "eu":
                year, month, day = int(c), int(b), int(a)
            elif order == "mdy":
                year, month, day = int(c), _MONTHS[a.lower()], int(b)
            else:
                year, month, day = int(c), _MONTHS[b.lower()], int(a)
            try:
                value = date(year, month, day).isoformat()
            except ValueError:
                continue
            found.append(DateEvidence(value, *match.span(), ambiguous))
    if allow_partial:
        for match in _PARTIAL_TEXT.finditer(text):
            if any(match.start() < end and match.end() > start for start, end in occupied):
                continue
            value = clean_date(match.group(), allow_partial=True)
            if value is not None:
                found.append(DateEvidence(value, *match.span()))
    return tuple(sorted(found, key=lambda row: row.start))


def resolve_date(quote: str, model_value: object = None, *, allow_partial: bool = False) -> GroundedDate:
    """Resolve a model hint against evidence in this quote, never another line."""
    evidence = find_dates(quote, allow_partial=allow_partial)
    candidates = {item.value for item in evidence}
    flags: list[str] = []
    if any(item.ambiguous for item in evidence):
        flags.append("ambiguous_date")
    if _RELATIVE.search(quote):
        flags.append("relative_deadline")
    supplied = model_value not in (None, "", "null")
    if supplied:
        value = clean_date(model_value, allow_partial=allow_partial)
        if value is not None and value in candidates:
            if "relative_deadline" in flags:
                return GroundedDate(None, "none", tuple(flags))
            return GroundedDate(value, "quote", tuple(flags))
        return GroundedDate(None, "none", tuple(flags + ["date_not_in_quote"]))
    if len(candidates) == 1 and "relative_deadline" not in flags:
        return GroundedDate(next(iter(candidates)), "quote", tuple(flags + ["date_from_quote"]))
    if len(candidates) > 1:
        flags.append("several_dates")
    return GroundedDate(None, "none", tuple(flags))


def checked_saved_date(value: str | None, quote: str, source: str,
                       flags: tuple[str, ...] | list[str], *, allow_partial: bool = False) -> GroundedDate:
    """Read-only projection: retain stored bytes and flag unsupported old dates."""
    if source in ("owner", "device"):
        # Set by the owner, or measured by a device on that day: not a quote to re-verify.
        return GroundedDate(value, source, tuple(flag for flag in flags if flag not in DATE_FLAGS))
    if value is None:
        # A rejected model date stays absent even if another date exists in its quote.
        # Reading old undated records does not infer an edit on the owner's behalf.
        return GroundedDate(None, "none", tuple(flags))
    checked = resolve_date(quote, value, allow_partial=allow_partial)
    merged = list(dict.fromkeys((*flags, *checked.flags)))
    if checked.value is None:
        merged.append("not_verified")
    return GroundedDate(checked.value, checked.due_source, tuple(dict.fromkeys(merged)))


def days_left(due: str | None, today: date | None = None) -> int | None:
    value = clean_date(due)
    return (date.fromisoformat(value) - (today or date.today())).days if value else None


def overdue(due: str | None, today: date | None = None) -> bool:
    remaining = days_left(due, today)
    return remaining is not None and remaining < 0


def relative_status(due: str | None, today: date | None = None) -> str:
    remaining = days_left(due, today)
    if remaining is None:
        return "no date" if due is None else "partial date"
    if remaining == 0:
        return "today"
    number = abs(remaining)
    unit = "day" if number == 1 else "days"
    return f"overdue {number} {unit}" if remaining < 0 else f"in {number} {unit}"
