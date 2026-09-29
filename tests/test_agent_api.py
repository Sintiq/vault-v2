"""The phone proposes drafts; accepting them goes through the desktop stores."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vault_v2.agent import Backend, BackendInfo
from vault_v2.agent_api import AgentAPI
from vault_v2.cards import CardStore, load_shelves
from vault_v2.health import HealthStore
from vault_v2.ops import VaultError, VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.reader import StagingReader
from vault_v2.tasks import TaskStore


class Scripted(Backend):
    """Answers the sort, tasks, health and ask prompts with fixed JSON."""

    def __init__(self):
        self.info = BackendInfo("fake", "fake", "fake")
        self.calls = 0

    def chat(self, system, messages, on_chunk):
        import re
        self.calls += 1
        text = messages[-1]["content"]
        ids = re.findall(r"doc-\d{3}", text) or ["doc-001"]
        first = ids[0]
        if "KIND is one of" in system:                        # health
            return json.dumps([{"doc": first, "date": "2026-09-14", "kind": "MEDICATION",
                                "label": "Sumatriptan 50 mg", "quote": "Sumatriptan 50 mg, 9 tablets."}])
        if "assemble a set of documents" in system:           # ask
            return json.dumps({"documents": [first], "recipient": "DOCTOR", "reason": "it is the prescription"})
        if "You sort documents" in system:                    # sort
            return json.dumps([{"id": first, "shelf": "HEALTH", "topics": ["prescription"],
                                "issuer": "Bay Neurology", "recipients": ["DOCTOR"], "reason": "a prescription"}])
        return json.dumps([{"doc": first, "title": "Refill sumatriptan", "due": "2026-10-16",   # tasks
                            "quote": "Refill before 2026-10-16."}])


@pytest.fixture()
def api(tmp_path: Path):
    load_shelves({})
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    (ops.paths.staging / "Prescription.txt").write_text(
        "Bay Neurology Clinic, 2026-09-14.\nSumatriptan 50 mg, 9 tablets. Refill before 2026-10-16.\n",
        encoding="utf-8")
    cards = CardStore(ops.paths.root / ".cards", ops.log)
    tasks = TaskStore(ops.paths.root / ".tasks", ops.log)
    health = HealthStore(ops.paths.root / ".health", ops.log)
    reader = StagingReader(ops, cards, purpose="agent")
    backend = Scripted()
    agent = AgentAPI(ops.paths.staging, reader, cards, tasks, health, lambda: backend)
    return ops, cards, tasks, health, agent, backend


def test_sort_proposes_with_a_question_mark_and_confirms_on_accept(api) -> None:
    ops, cards, _t, _h, agent, _b = api
    out = agent.sort_propose()
    assert len(out["proposals"]) == 1
    p = out["proposals"][0]
    assert p["shelf"] == "HEALTH" and p["origin"] == "AGENT"
    card = cards.for_path(ops.paths.staging / "Prescription.txt")
    assert card is not None and card.confirmed is False, "a proposal shows as '?', it is not confirmed"

    # The owner changed the shelf on the phone before accepting.
    res = agent.sort_confirm([{"id": p["id"], "shelf": "INSURANCE"}])
    assert res == {"confirmed": 1, "errors": []}
    card = cards.for_path(ops.paths.staging / "Prescription.txt")
    assert card.confirmed and card.shelf == "INSURANCE" and card.origin == "HUMAN"
    assert ops.log.tail(1)[0]["op"] == "card_confirm"

    # A replay or unknown handle rejects the entire selection.
    with pytest.raises(VaultError, match="proposals changed — refresh the list"):
        agent.sort_confirm([{"id": p["id"]}, {"id": "nope"}])


def test_tasks_and_health_propose_then_add_only_what_was_ticked(api) -> None:
    ops, _c, tasks, health, agent, _b = api
    t = agent.tasks_propose()
    titles = [x["title"] for x in t["proposals"]]
    assert "Refill sumatriptan" in titles, titles          # the agent's; the baseline may add its own
    assert tasks.all() == [], "proposing writes nothing"
    with pytest.raises(VaultError, match="proposals changed — refresh the list"):
        agent.tasks_add(["not-a-real-id"])
    mine = next(x["id"] for x in t["proposals"] if x["title"] == "Refill sumatriptan")
    assert agent.tasks_add([mine]) == {"added": 1}
    assert [x.title for x in tasks.all()] == ["Refill sumatriptan"]
    assert "Refill sumatriptan" not in [x["title"] for x in agent.tasks_propose()["proposals"]],         "already added — not offered again"

    h = agent.health_propose()
    labels = [x["label"] for x in h["proposals"]]
    assert "Sumatriptan 50 mg" in labels
    assert health.all() == []
    ids = [x["id"] for x in h["proposals"] if x["origin"] == "AGENT"]
    assert agent.health_add(ids) == {"added": len(ids)}
    assert any(e.label == "Sumatriptan 50 mg" for e in health.all())
    assert ops.log.tail(1)[0]["op"] == "health_add"


def test_the_agent_only_ever_reads_staging(api) -> None:
    ops, _c, _t, _h, agent, backend = api
    (ops.paths.personal / "secret.txt").write_text("SECRET-DIAGNOSIS", encoding="utf-8")
    agent.tasks_propose()
    agent.health_propose()
    agent.sort_propose()
    reads = [r for r in ops.log.tail(50) if r["op"] == "agent_read"]
    assert reads and all("secret" not in r["src"].lower() for r in reads)


def test_ask_searches_but_never_exports(api) -> None:
    _o, _c, _t, _h, agent, _b = api
    with pytest.raises(VaultError):
        agent.ask("   ")
    out = agent.ask("what should the doctor see")
    assert out["export"] == "at the desk only"
    assert [m["name"] for m in out["matches"]] == ["Prescription.txt"]


def test_without_a_model_the_baseline_still_answers(api) -> None:
    ops, _c, _t, _h, agent, _b = api
    agent.get_backend = lambda: None
    out = agent.tasks_propose()
    assert "no agent" in " ".join(out["notes"])
    assert any(t["origin"] == "BASELINE" for t in out["proposals"])
