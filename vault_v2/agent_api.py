"""The agent's four jobs, reachable from the phone.

On the desk each of these is a dialog: the agent proposes, the owner ticks,
the store confirms. Here the same three steps are split into two calls —
propose, then accept — and the middle step, the ticking, happens on the
phone. Sorting persists unconfirmed drafts, as the desktop dialog does;
tasks and health proposals do not write. Acceptance uses the desktop stores.

Proposals live in memory between the two calls, keyed by the id the phone
sends back. A restart forgets them, which is the right failure: a proposal
the owner never saw again should not be acceptable a day later.

Ask is search only. What the desk does after asking — export — stays at the
desk, where the Gatekeeper's snapshot and one-use approval live.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from threading import RLock
from typing import Callable

from . import ask as asking
from . import health as healthing
from . import sorting
from . import tasks as tasking
from .agent import Backend
from .cards import Card, CardError, CardStore
from .health import HealthStore
from .ops import VaultError
from .reader import StagingReader
from .proposals import ProposalConflict, ShownProposal
from .tasks import TaskStore

GetBackend = Callable[[], Backend | None]


def _no_backend_note(backend: Backend | None) -> list[str]:
    return [] if backend is not None else ["no agent — keyword baseline only"]


def _store_selected(handles: list[str], selected: list, write: Callable, count_key: str) -> dict:
    """Selected handles are already burned; a raised store call has unknown effect."""
    result = {count_key: 0}
    if count_key == "confirmed":
        result["errors"] = []
    for index, value in enumerate(selected):
        try:
            write(value)
        except Exception:
            result["errors"] = ["store write failed — effect unknown; refresh the list"]
            result["outcome"] = {
                "completed": handles[:index],
                "failed_unknown": handles[index:index + 1],
                "not_attempted": handles[index + 1:],
            }
            return result
        result[count_key] += 1
    return result


class AgentAPI:
    """Lock order: root guard -> _proposal_lock -> store.mutation_lock.

    Collection/model work completes before entering a publication block.
    """
    def __init__(self, staging: Path, reader: StagingReader, cards: CardStore,
                 tasks: TaskStore, health: HealthStore, get_backend: GetBackend):
        self.staging, self.reader, self.cards = staging, reader, cards
        self.tasks, self.health, self.get_backend = tasks, health, get_backend
        # Each map contains only the current batch's unused handles. Replacing
        # it invalidates a batch; removing every selection consumes it once.
        self._sort: dict[str, ShownProposal] = {}
        self._tasks: dict[str, ShownProposal] = {}
        self._health: dict[str, ShownProposal] = {}
        self._proposal_lock = RLock()
        self._generations: dict[str, int] = {}

    # -- sort ------------------------------------------------------------------

    def sort_propose(self) -> dict:
        backend = self.get_backend()
        docs = sorting.collect_inputs(self.staging, self.cards, reader=self.reader)
        if not docs:
            with self.cards.log.write("publish sort"), self._proposal_lock, self.cards.mutation_lock:
                # Publication is a logical revision even when no draft is written.
                # This also expires an older API instance sharing these stores.
                self.cards.log.effect()
                self.cards.generation += 1
                self._sort = {}
                self._generations["sort"] = self.cards.generation
            return {"proposals": [], "notes": ["Staging is empty — nothing to sort"]}
        # Cards are keyed by bytes. Keep the first document in the stable input
        # order so one acceptance never counts overwrites as separate additions.
        by_sha = {}
        for proposal in sorting.propose(docs, backend):
            by_sha.setdefault(proposal.doc.sha256, proposal)
        proposals = list(by_sha.values())
        out = []
        for p in proposals:
            out.append({
                "sha256": p.doc.sha256, "name": p.doc.name, "size": p.doc.size, "kind": p.card.kind,
                "shelf": p.card.shelf, "topics": list(p.card.topics), "issuer": p.card.issuer,
                "year": p.card.year, "recipients": list(p.card.recipients),
                "origin": p.card.origin, "reason": p.card.reason,
                "flags": list(p.flags), "differs": p.differs,
                "baseline_shelf": p.baseline.shelf,
            })
        frozen = [ShownProposal.freeze(row, str(p.doc.path)) for row, p in zip(out, proposals)]
        with self.cards.log.write("publish sort"), self._proposal_lock, self.cards.mutation_lock:
            for p in proposals:
                # Publish only after all drafts have returned successfully.
                self.cards.propose(p.card, p.doc.path)
            self.cards.log.effect()
            self.cards.generation += 1
            self._sort = {row.handle: row for row in frozen}
            self._generations["sort"] = self.cards.generation
        reading_notes = [f"{d.name}: {warning}" for d in docs for warning in d.warnings]
        return {"proposals": [row.shown() for row in frozen],
                "notes": reading_notes + _no_backend_note(backend)}

    def sort_confirm(self, items: list[dict]) -> dict:
        with self.cards.log.write("confirm sort"), self._proposal_lock, self.cards.mutation_lock:
            if self._generations.get("sort") != self.cards.generation:
                raise ProposalConflict()
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise ProposalConflict()
            handles = [item.get("id") for item in items]
            if any(not isinstance(h, str) for h in handles):
                raise ProposalConflict()
            if len(set(handles)) != len(handles) or any(h not in self._sort for h in handles):
                raise ProposalConflict()
            selected = []
            for item in items:
                p = self._sort[item["id"]]
                row = p.shown()
                # The owner may have edited the shelf or topics on the phone.
                try:
                    card = Card.build(
                        row["sha256"], row["name"], row["kind"],
                        shelf=item.get("shelf", row["shelf"]),
                        topics=item.get("topics", row["topics"]),
                        issuer=item.get("issuer", row["issuer"]),
                        year=item.get("year", row["year"]),
                        recipients=item.get("recipients", row["recipients"]),
                        origin="HUMAN" if any(k in item for k in ("shelf", "topics", "issuer", "year", "recipients"))
                        else row["origin"], reason=row["reason"],
                    )
                except (CardError, TypeError, ValueError) as exc:
                    raise ProposalConflict() from exc
                selected.append((card, p.target))
            if handles:
                self.cards.log.effect()
            for handle in handles:
                del self._sort[handle]
            return _store_selected(handles, selected, lambda value: self.cards.confirm(*value), "confirmed")

    # -- tasks -----------------------------------------------------------------

    def tasks_propose(self) -> dict:
        backend = self.get_backend()
        found, notes = tasking.propose(self.reader, backend)
        with self.tasks.log.write("publish tasks"), self._proposal_lock, self.tasks.mutation_lock:
            by_id = {}
            for task in found:
                if not self.tasks.has(task.id):
                    by_id.setdefault(task.id, task)
            fresh = list(by_id.values())
            rows = []
            for task in fresh:
                row = asdict(task)
                row["task_id"] = row.pop("id")
                rows.append(ShownProposal.freeze(row))
            self.tasks.log.effect()
            self.tasks.generation += 1
            self._tasks = {row.handle: row for row in rows}
            self._generations["tasks"] = self.tasks.generation
        return {"proposals": [row.shown() for row in rows], "notes": list(notes) + _no_backend_note(backend)}

    def tasks_add(self, ids: list[str]) -> dict:
        with self.tasks.log.write("accept tasks"), self._proposal_lock, self.tasks.mutation_lock:
            if self._generations.get("tasks") != self.tasks.generation:
                raise ProposalConflict()
            if not isinstance(ids, list) or any(not isinstance(h, str) for h in ids):
                raise ProposalConflict()
            if len(set(ids)) != len(ids) or any(h not in self._tasks for h in ids):
                raise ProposalConflict()
            selected = []
            for handle in ids:
                row = self._tasks[handle].shown()
                row["id"] = row.pop("task_id")
                selected.append(tasking.Task(**row))
            if ids:
                self.tasks.log.effect()
            for handle in ids:
                del self._tasks[handle]
            return _store_selected(ids, selected, self.tasks.add, "added")

    # -- health ----------------------------------------------------------------

    def health_propose(self) -> dict:
        backend = self.get_backend()
        found, notes = healthing.propose(self.reader, backend)
        with self.health.log.write("publish health"), self._proposal_lock, self.health.mutation_lock:
            by_id = {}
            for entry in found:
                if not self.health.has(entry.id):
                    by_id.setdefault(entry.id, entry)
            fresh = list(by_id.values())
            rows = []
            for entry in fresh:
                row = asdict(entry)
                row["entry_id"] = row.pop("id")
                rows.append(ShownProposal.freeze(row))
            self.health.log.effect()
            self.health.generation += 1
            self._health = {row.handle: row for row in rows}
            self._generations["health"] = self.health.generation
        return {"proposals": [row.shown() for row in rows], "notes": list(notes) + _no_backend_note(backend)}

    def health_add(self, ids: list[str]) -> dict:
        with self.health.log.write("accept health"), self._proposal_lock, self.health.mutation_lock:
            if self._generations.get("health") != self.health.generation:
                raise ProposalConflict()
            if not isinstance(ids, list) or any(not isinstance(h, str) for h in ids):
                raise ProposalConflict()
            if len(set(ids)) != len(ids) or any(h not in self._health for h in ids):
                raise ProposalConflict()
            selected = []
            for handle in ids:
                row = self._health[handle].shown()
                row["id"] = row.pop("entry_id")
                selected.append(healthing.Entry(**row))
            if ids:
                self.health.log.effect()
            for handle in ids:
                del self._health[handle]
            return _store_selected(ids, selected, self.health.add, "added")

    # -- ask (search only) -----------------------------------------------------

    def ask(self, phrase: str) -> dict:
        phrase = str(phrase).strip()
        if not phrase:
            raise VaultError("ask what?")
        backend = self.get_backend()
        docs = asking.collect_archive_docs(self.reader.ops.paths, self.cards)
        if not docs:
            return {"phrase": phrase, "matches": [], "notes": ["Archive is empty"]}
        result = asking.ask(phrase, docs, backend)
        by_id = {d.id: d for d in docs}
        matches = []
        for did in result.proposed_ids:
            d = by_id.get(did)
            if d is None:
                continue
            matches.append({
                "name": d.path.name, "shelf": d.card.shelf if d.card else None,
                "pane": d.pane, "rel": d.rel,
                "added_by_agent": did in result.added_by_agent,
                "omitted_by_agent": did in result.omitted_by_agent,
            })
        notes = list(result.notes) + _no_backend_note(backend)
        if result.error:
            notes.append(result.error)
        return {"phrase": phrase, "matches": matches, "recipient": result.agent_recipient,
                "notes": notes, "agent_reason": result.agent_reason, "export": "at the desk only"}
