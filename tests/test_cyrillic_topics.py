"""Unicode topic behavior at the card, persistence and Ask boundaries."""

import pytest

from vault_v2.ask import ask, collect_docs
from vault_v2.cards import Card, CardError, CardStore, canonical_topics
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths


def test_topics_preserve_cyrillic_and_mixed_script_subjects():
    assert canonical_topics(["МиГрЕнЬ", "АНАЛИЗ-КРОВИ", "MRT-МОЗГ", "ЁЛКА", "Елка"]) == (
        "мигрень", "анализ-крови", "mrt-мозг", "елка",
    )


@pytest.mark.parametrize("raw,expected", [
    ("Car Insurance", "car-insurance"),
    ("car_loan", "car-loan"),
    ("Анализ Крови", "анализ-крови"),
    ("tax 2025!", "tax-2025"),
])
def test_owner_and_model_topic_inputs_keep_legacy_normalization(raw, expected):
    assert canonical_topics([raw]) == (expected,)


@pytest.mark.parametrize("raw,expected", [
    ("анализ крови", "анализ-крови"), ("blood_sugar", "blood-sugar"),
    ("мигрень!", "мигрень"), ("#мигрень", "мигрень"),
    ("мигрень/мозг", "мигреньмозг"), ("мигрень\\мозг", "мигреньмозг"),
    ("мигрень\tмозг", "мигреньмозг"), ("мигрень\nмозг", "мигреньмозг"),
    ("мигрень🙂", "мигрень"), ("-мигрень", "мигрень"),
    ("мигрень-", "мигрень"), ("анализ--крови", "анализ-крови"),
    ("мигрень—мозг", "мигреньмозг"), ("мигрень\u200b", "мигрень"),
])
def test_topic_normalization_retains_unicode_while_removing_punctuation(raw, expected):
    assert canonical_topics([raw], allow_empty=True) == (expected,)


def test_empty_normalized_topics_are_skipped_and_normalized_duplicates_count_once():
    assert canonical_topics(["!_ -🙂", "", " "], allow_empty=True) == ()
    with pytest.raises(CardError, match="at least one"):
        canonical_topics(["!_ -🙂"])
    assert canonical_topics(["--Анализ _ Крови!", "анализ-крови", "АНАЛИЗ__КРОВИ"]) == (
        "анализ-крови",
    )


def test_topic_length_limit_applies_after_full_normalization():
    assert canonical_topics(["!" * 60 + "я" * 40 + "_" * 60]) == ("я" * 40,)
    with pytest.raises(CardError, match="bad topic"):
        canonical_topics(["!" * 60 + "я" * 41 + "_" * 60])


def test_topic_ui_list_delimiters_outer_trim_and_deduplication_remain_supported():
    assert canonical_topics("  Мигрень , АНАЛИЗ-КРОВИ;\nMRT-МОЗГ, мигрень, ") == (
        "мигрень", "анализ-крови", "mrt-мозг",
    )


def test_topics_accept_unicode_letters_and_numbers_after_casefold():
    assert canonical_topics(["Straße", "CAFÉ", "ТОМОГРАФИЯ-2026", "分析-１２"]) == (
        "strasse", "café", "томография-2026", "分析-１２",
    )


def test_topic_length_is_counted_after_unicode_casefold():
    assert canonical_topics(["я" * 40]) == ("я" * 40,)
    assert canonical_topics(["ß" * 20]) == ("ss" * 20,)
    with pytest.raises(CardError, match="bad topic"):
        canonical_topics(["я" * 41])
    with pytest.raises(CardError, match="bad topic"):
        canonical_topics(["ß" * 21])


def test_topic_limit_and_binary_empty_topics_are_unchanged():
    topics = ["тема-" + str(number) for number in range(8)]
    assert len(canonical_topics(topics)) == 8
    with pytest.raises(CardError, match="at most 8"):
        canonical_topics(topics + ["девятая"])
    with pytest.raises(CardError, match="at least one"):
        canonical_topics([])
    assert canonical_topics([], allow_empty=True) == ()
    assert canonical_topics(" , ;\n ", allow_empty=True) == ()


def test_confirmed_cyrillic_topic_is_persisted_and_found_without_text_or_model(tmp_path):
    vault = VaultOps(VaultPaths(tmp_path / "vault"))
    source = vault.paths.staging / "report.pdf"
    source.write_bytes(b"Synthetic bytes with no PDF contents or searchable topic")
    store = CardStore(vault.paths.root / ".cards", vault.log)
    card = Card.build(store.hash_of(source), source.name, "BINARY", shelf="HEALTH",
                      topics=["МиГрЕнЬ", "ЁЛКА", "Анализ Крови"], recipients=["DOCTOR"])
    store.confirm(card, source)

    reloaded = CardStore(vault.paths.root / ".cards", vault.log)
    assert reloaded.for_path(source).topics == ("мигрень", "елка", "анализ-крови")
    docs = collect_docs(vault.paths.staging, reloaded)
    assert docs[0].excerpt == "" and docs[0].total_chars is None
    assert ask("МИГРЕНЬ", docs, None).proposed_ids == ("doc-001",)
    assert ask("ёлка", docs, None).proposed_ids == ("doc-001",)
    assert ask("АНАЛИЗ КРОВИ", docs, None).proposed_ids == ("doc-001",)
    assert not (vault.paths.root / ".text").exists()


def test_legacy_ascii_card_load_does_not_migrate_saved_bytes(tmp_path):
    vault = VaultOps(VaultPaths(tmp_path / "vault"))
    source = vault.paths.staging / "record.pdf"
    source.write_bytes(b"Legacy synthetic record")
    store = CardStore(vault.paths.root / ".cards", vault.log)
    card = Card.build(store.hash_of(source), source.name, "BINARY", shelf="HEALTH",
                      topics=["migraine", "blood-sugar"], recipients=["DOCTOR"])
    store.confirm(card, source)
    saved = store.file.read_bytes()
    receipts = vault.log.tail()

    reloaded = CardStore(vault.paths.root / ".cards", vault.log)
    assert reloaded.for_path(source).topics == ("migraine", "blood-sugar")
    assert reloaded.file.read_bytes() == saved
    assert vault.log.tail() == receipts
