"""Shelves are the owner's to extend; the defaults never go away."""

from __future__ import annotations

import pytest

from vault_v2 import cards
from vault_v2.cards import DEFAULT_SHELVES, Card, CardError, canonical_shelf_name, load_shelves


@pytest.fixture(autouse=True)
def _reset_shelves():
    yield
    load_shelves({})


def test_defaults_stay_and_the_owners_shelf_is_added() -> None:
    got = load_shelves({"shelves": ["IMMIGRATION"]})
    assert got[: len(DEFAULT_SHELVES)] == DEFAULT_SHELVES
    assert got[-1] == "IMMIGRATION"
    assert cards.SHELVES == got, "readers must see the loaded list, not a stale copy"


def test_a_card_on_the_new_shelf_validates_and_on_an_unknown_one_does_not() -> None:
    load_shelves({"shelves": ["IMMIGRATION"]})
    card = Card.build("a" * 64, "i-130.pdf", "BINARY", shelf="IMMIGRATION", topics=["uscis"])
    assert card.shelf == "IMMIGRATION"
    with pytest.raises(CardError, match="shelf must be one of"):
        Card.build("a" * 64, "x.pdf", "BINARY", shelf="GARDENING", topics=["x"])


def test_names_are_tidied_and_nonsense_is_refused() -> None:
    assert canonical_shelf_name(" immigration ") == "IMMIGRATION"
    assert canonical_shelf_name("car loan") == "CAR_LOAN"
    assert canonical_shelf_name("us-visa") == "US_VISA"
    for bad in ("", "x", "1ST", "a shelf with far too long a name to be a label", "ni!ce"):
        with pytest.raises(CardError):
            canonical_shelf_name(bad)


def test_duplicates_and_strings_are_tolerated() -> None:
    got = load_shelves({"shelves": "immigration, HEALTH, immigration"})
    assert got.count("IMMIGRATION") == 1 and got.count("HEALTH") == 1


def test_no_settings_means_the_defaults() -> None:
    assert load_shelves(None) == DEFAULT_SHELVES
    assert load_shelves({}) == DEFAULT_SHELVES


# -- the setting the owner edits from the window ------------------------------

from vault_v2.cards import Card, CardStore
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.receipts import sha256_file
from vault_v2.shelves import add_shelf, custom_shelves, remove_shelf


def test_adding_a_shelf_writes_vault_json_and_takes_effect_live(tmp_path) -> None:
    paths = VaultPaths(tmp_path / "vault").ensure()
    assert custom_shelves(paths) == []
    got = add_shelf(paths, "car loan")
    assert got[-1] == "CAR_LOAN" and cards.SHELVES[-1] == "CAR_LOAN"
    assert custom_shelves(paths) == ["CAR_LOAN"]
    assert VaultPaths(tmp_path / "vault").settings()["shelves"] == ["CAR_LOAN"], "on disk"
    with pytest.raises(CardError, match="standard"):
        add_shelf(paths, "health")
    add_shelf(paths, "CAR_LOAN")
    assert custom_shelves(paths) == ["CAR_LOAN"], "no duplicates"


def test_removing_a_shelf_moves_its_cards_to_inbox_with_receipts(tmp_path) -> None:
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    paths = ops.paths
    store = CardStore(paths.root / ".cards", ops.log)
    add_shelf(paths, "IMMIGRATION")
    f = paths.personal / "i-130.txt"
    f.write_text("form", encoding="utf-8")
    store.confirm(Card.build(sha256_file(f), f.name, "TEXT", shelf="IMMIGRATION", topics=["uscis"]), f)

    shelves, moved = remove_shelf(paths, store, "IMMIGRATION")
    assert "IMMIGRATION" not in shelves and moved == 1
    card = store.for_path(f)
    assert card.shelf == "INBOX" and card.confirmed and "removed" in card.reason
    assert [r["op"] for r in ops.log.tail(2)] == ["card_confirm", "shelf_remove"]
    assert custom_shelves(paths) == []
    with pytest.raises(CardError):
        remove_shelf(paths, store, "HEALTH")
    with pytest.raises(CardError):
        remove_shelf(paths, store, "NEVER_WAS")
