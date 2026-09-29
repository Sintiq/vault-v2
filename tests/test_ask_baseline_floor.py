"""Selection contract: local keyword evidence cannot be removed by a model."""
import json

from vault_v2.agent import Backend, BackendInfo
from vault_v2.agent_scope import AgentTextScope
from vault_v2.ask import AskDoc, ask, collect_archive_docs
from vault_v2.cards import Card, CardStore
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths


class SelectionBackend(Backend):
    info = BackendInfo("ollama", "synthetic", "local test adapter")

    def __init__(self, selected):
        self.selected = selected

    def chat(self, system, messages, on_chunk):
        return json.dumps({"documents": self.selected, "recipient": "PERSONAL", "reason": "synthetic"})


def test_archive_model_cannot_remove_card_or_permitted_text_hits(tmp_path):
    ops = VaultOps(VaultPaths(tmp_path / "synthetic-vault"))
    cards = CardStore(ops.paths.root / ".cards", ops.log)
    staged = ops.paths.staging / "record.txt"
    staged.write_text("Synthetic umbrella note", encoding="utf-8")
    cards.confirm(Card.build(cards.hash_of(staged), staged.name, "TEXT",
                             shelf="INBOX", topics=["мигрень"]), staged)
    clinic = ops.paths.personal / "Clinic"
    clinic.mkdir()
    permitted = clinic / "record.txt"
    permitted.write_text("Мигрень: synthetic appointment", encoding="utf-8")
    denied = ops.paths.documents / "neutral.txt"
    denied.write_text("Мигрень: unmarked negative control", encoding="utf-8")
    scope = AgentTextScope(ops.paths, ops.log)
    scope.set_folder(clinic, True)
    scope.warm(clinic)
    docs = collect_archive_docs(ops.paths, cards)

    result = ask("мигрень", docs, SelectionBackend([]))

    assert {doc.path for doc in result.docs if doc.id in result.proposed_ids} == {staged, permitted}
    assert result.omitted_by_agent == ()


def test_model_can_rank_and_add_but_cannot_erase_baseline_or_invent_ids(tmp_path):
    docs = [AskDoc("keyword", tmp_path / "migraine.txt", None),
            AskDoc("semantic", tmp_path / "referral.txt", None)]
    result = ask("migraine", docs, SelectionBackend(["semantic", "invented", "semantic"]))
    assert result.proposed_ids == ("semantic", "keyword")
    assert result.added_by_agent == ("semantic",)
    assert result.omitted_by_agent == ()


def test_null_model_reason_is_silence_not_a_displayed_note(tmp_path):
    class SilentBackend(SelectionBackend):
        def chat(self, system, messages, on_chunk):
            return '{"documents": [], "reason": null}'

    result = ask("migraine", [AskDoc("one", tmp_path / "migraine.txt", None)], SilentBackend([]))
    assert result.agent_reason == ""
