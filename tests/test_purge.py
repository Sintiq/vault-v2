"""Clearing junk out: the one real delete the vault has, and it leaves a trace."""

from __future__ import annotations

from pathlib import Path

from vault_v2.cards import Card, CardStore
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.receipts import sha256_file


def test_purging_the_trash_really_deletes_and_still_leaves_receipts(tmp_path: Path) -> None:
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    f = ops.paths.staging / "old.txt"
    f.write_text("junk", encoding="utf-8")
    d = ops.paths.staging / "olddir"
    d.mkdir()
    (d / "inner.txt").write_text("more junk", encoding="utf-8")
    digest = sha256_file(f)
    ops.trash(f)
    ops.trash(d)
    assert len(ops.list_trash()) == 2

    result = ops.purge_trash(note="owner: synthetic purge")
    assert (result.removed, result.skipped) == (2, 0)
    assert ops.list_trash() == []
    assert not any(ops.paths.trash.iterdir()), "nothing left behind, not even manifests"

    purged = [r for r in ops.log.tail(4) if r["op"] == "purge"]
    assert len(purged) == 2
    assert digest in {r["sha256"] for r in purged}, "the log keeps the digest of what is gone"
    assert ops.log.verify() > 0


def test_cards_of_vanished_files_are_forgotten_with_a_receipt(tmp_path: Path) -> None:
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    store = CardStore(ops.paths.root / ".cards", ops.log)
    keep = ops.paths.personal / "keep.txt"
    keep.write_text("stays", encoding="utf-8")
    gone = ops.paths.documents / "gone.txt"
    gone.write_text("leaves", encoding="utf-8")
    for p in (keep, gone):
        store.confirm(Card.build(sha256_file(p), p.name, "TEXT", shelf="INBOX", topics=["x"]), p)
    gone_sha = sha256_file(gone)
    gone.unlink()

    dropped = store.forget_missing([ops.paths.staging, ops.paths.documents, ops.paths.personal])
    assert dropped == 1
    assert store.for_path(keep) is not None
    assert gone_sha not in {c.sha256 for c in store.all()}
    assert ops.log.tail(1)[0]["op"] == "card_forget"
    # A fresh store reads the same truth back from disk.
    assert len(CardStore(ops.paths.root / ".cards", ops.log).all()) == 1
