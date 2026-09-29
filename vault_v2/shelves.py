"""The owner's shelves, as a setting he can change from the window.

vault.json carries the custom shelves; the defaults are code. Adding is a
name check and a write. Removing is the only part with a decision in it:
cards on a shelf that disappears do not vanish, they go to INBOX, each with
its own receipt, so nothing is ever left pointing at a shelf that no longer
exists and nothing is lost by tidying a list.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from .cards import DEFAULT_SHELVES, Card, CardError, CardStore, canonical_shelf_name, load_shelves
from .paths import VaultPaths
from .receipts import ReceiptLog

FALLBACK_SHELF = "INBOX"


def custom_shelves(paths: VaultPaths) -> list[str]:
    raw = paths.settings().get("shelves", [])
    if isinstance(raw, str):
        raw = [x for x in raw.replace("\n", ",").split(",")]
    out: list[str] = []
    for r in raw if isinstance(raw, (list, tuple)) else []:
        try:
            s = canonical_shelf_name(r)
        except CardError:
            continue
        if s not in DEFAULT_SHELVES and s not in out:
            out.append(s)
    return out


def _write(paths: VaultPaths, names: list[str], log: ReceiptLog, op: str, name: str) -> tuple[str, ...]:
    settings = paths.settings()
    settings["shelves"] = names
    tmp = paths.settings_file.with_suffix(".json.tmp")
    log.effect()
    try:
        tmp.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(paths.settings_file)
        result = load_shelves(settings)
        log.append(op, name, paths.settings_file)
        return result
    except Exception:
        load_shelves(paths.settings())
        raise


def add_shelf(paths: VaultPaths, name: object, log: ReceiptLog | None = None) -> tuple[str, ...]:
    """Returns the full shelf list after adding. Raises CardError on a bad name."""
    log = log if log is not None else ReceiptLog(paths.receipts)
    with log.write("shelf_add"):
        s = canonical_shelf_name(name)
        if s in DEFAULT_SHELVES:
            raise CardError(f"{s} is a standard shelf and is already there")
        names = custom_shelves(paths)
        if s not in names:
            names.append(s)
        return _write(paths, names, log, "shelf_add", s)


def remove_shelf(paths: VaultPaths, cards: CardStore, name: object) -> tuple[tuple[str, ...], int]:
    """Remove a custom shelf. Cards on it move to INBOX, receipted. Returns (shelves, moved)."""
    with cards.log.write("shelf_remove"):
        s = canonical_shelf_name(name)
        if s in DEFAULT_SHELVES:
            raise CardError(f"{s} is a standard shelf and stays")
        names = custom_shelves(paths)
        if s not in names:
            raise CardError(f"no such shelf: {s}")
        moved = 0
        for card in cards.all():
            if card.shelf == s:
                relocated = replace(
                    card, shelf=FALLBACK_SHELF,
                    reason=f"shelf {s} removed; moved to {FALLBACK_SHELF}",
                )
                if card.confirmed:
                    cards.confirm(relocated, f"shelf {s} removed")
                else:
                    cards.propose(relocated, f"shelf {s} removed")
                moved += 1
        names.remove(s)
        return _write(paths, names, cards.log, "shelf_remove", s), moved
