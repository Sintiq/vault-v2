"""Seed a throwaway vault so the phone page can be clicked through for real."""

from __future__ import annotations

import sys
from pathlib import Path

from vault_v2.cards import Card, CardStore
from vault_v2.health import Entry, HealthStore
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.receipts import sha256_file
from vault_v2.tasks import Task, TaskStore

DOCS = {
    "documents/Insurance policy 2026.txt":
        "Blue Shield policy PPO-4471.\nCoverage year 2026.\nPremium due on 2026-10-01.\n",
    "documents/Lab results 2026-01-20.txt":
        "Lab result 2026-01-20: metabolic panel within range.\nFollow-up appointment to be scheduled.\n",
    "personal/Passport notes.txt": "Passport expires 2029-04-11.\n",
    "staging/Prescription sumatriptan.txt":
        "Visit on 2026-03-15 at Bay Neurology Clinic.\nAssessment: migraine without aura.\n"
        "Sumatriptan 50 mg prescribed. Refill before 2026-10-16.\n",
    "staging/Utility bill September.txt": "PG&E statement. Amount due 84.20. Pay by 2026-10-05.\n",
}


def main(root: Path) -> None:
    ops = VaultOps(VaultPaths(root))
    cards = CardStore(ops.paths.root / ".cards", ops.log)
    tasks = TaskStore(ops.paths.root / ".tasks", ops.log)
    health = HealthStore(ops.paths.root / ".health", ops.log)

    for rel, text in DOCS.items():
        p = ops.paths.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    policy = ops.paths.documents / "Insurance policy 2026.txt"
    cards.confirm(Card.build(sha256_file(policy), policy.name, "TEXT", shelf="INSURANCE",
                             topics=["insurance", "policy"], issuer="Blue Shield", year=2026,
                             recipients=["INSURANCE"], origin="HUMAN"), policy)
    lab = ops.paths.documents / "Lab results 2026-01-20.txt"
    cards.propose(Card.build(sha256_file(lab), lab.name, "TEXT", shelf="HEALTH",
                             topics=["lab"], year=2026, origin="AGENT"), lab)

    rx = ops.paths.staging / "Prescription sumatriptan.txt"
    tasks.add(Task(f"{sha256_file(rx)[:12]}:refill", sha256_file(rx), rx.name,
                   "Refill sumatriptan", "2026-10-16", "Refill before 2026-10-16.", "AGENT"))
    bill = ops.paths.staging / "Utility bill September.txt"
    tasks.add(Task(f"{sha256_file(bill)[:12]}:pay", sha256_file(bill), bill.name,
                   "Pay the PG&E statement", "2026-10-05", "Pay by 2026-10-05.", "AGENT"))

    health.add(Entry(f"{sha256_file(rx)[:12]}:visit", sha256_file(rx), rx.name, "2026-03-15",
                     "VISIT", "Visit at Bay Neurology Clinic",
                     "Visit on 2026-03-15 at Bay Neurology Clinic.", "AGENT"))
    health.add(Entry(f"{sha256_file(rx)[:12]}:dx", sha256_file(rx), rx.name, "2026-03-15",
                     "DIAGNOSIS", "Migraine without aura", "Assessment: migraine without aura.", "AGENT"))
    lab_sha = sha256_file(lab)
    health.add(Entry(f"{lab_sha[:12]}:lab", lab_sha, lab.name, "2026-01-20", "TEST",
                     "Metabolic panel within range",
                     "Lab result 2026-01-20: metabolic panel within range.", "AGENT"))
    print(root)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
