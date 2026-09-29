"""Append-only, hash-chained receipts.

Every operation the vault performs (copy, move, trash, restore, export)
writes one JSON line. Each line carries the sha256 of the previous line, so
a tampered or deleted middle record breaks the chain on verification —
the evidence-ledger rule from capsule-product-block, kept whole here.
"""

from __future__ import annotations

import hashlib
import json
import stat
from contextlib import contextmanager
from uuid import uuid4
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .write_guard import root_guard
from .errors import VaultError

GENESIS = "0" * 64


def _plain_evidence_directory(path: Path) -> bool:
    """Inspect the container itself without following a linked directory."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
        raise VaultError("pending evidence directory needs manual review")
    return True


def _evidence_directory_error(identifier: str) -> dict:
    return {"id": identifier, "op": "unknown", "paths": {}, "expected_hashes": {}, "ts": "unknown",
            "error": "pending evidence directory needs manual review"}


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class Receipt:
    op: str
    src: str
    dst: str
    sha256: str
    size: int
    ts: str
    prev_hash: str
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def body(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    def hash(self) -> str:
        return hashlib.sha256(_canonical(self.body())).hexdigest()


class ReceiptLog:
    """One JSONL file per vault: <receipts dir>/receipts.jsonl."""

    def __init__(self, receipts_dir: Path, *, read_only: bool = False):
        self.dir = Path(receipts_dir)
        self.file = self.dir / "receipts.jsonl"
        self.read_only = read_only
        self.guard = root_guard(self.dir.parent)

    def write(self, label: str = ""):
        """Acquire root guard BEFORE proposal and store locks; precheck log."""
        return self.guard.write(self, label)

    def completion_write(self, label: str = ""):
        """Await outcome audit after background work, despite HTTP admission policy.

        No model/process wait may live inside this scope. Other writes retain
        their ordinary fail-fast UI/HTTP policy; journal errors still refuse.
        """
        return self.guard.write(self, label, wait=True)

    def effect(self):
        self.guard.effect()

    def nonblocking(self):
        return self.guard.nonblocking()

    def prefer_nonblocking(self):
        return self.guard.prefer_nonblocking()

    @contextmanager
    def intent(self, op: str, paths: dict, expected_hashes: dict | None = None):
        """An outer file operation leaves one intention until all receipts land.

        This is evidence to show after a crash, never an instruction to replay.
        Caller already holds write(); nested file operations share the intent.
        """
        state = self.guard._local
        if not getattr(state, "depth", 0):
            raise RuntimeError("intent requires root guard")
        if getattr(state, "intent", None) is not None:
            yield
            return
        directory = self.dir / "pending"
        _plain_evidence_directory(self.dir)
        _plain_evidence_directory(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (uuid4().hex + ".json")
        row = {"op": op, "paths": {k: str(v) for k, v in paths.items()},
               "expected_hashes": dict(expected_hashes or {}),
               "ts": datetime.now(timezone.utc).isoformat()}
        with path.open("x", encoding="utf-8") as stream:
            json.dump(row, stream, ensure_ascii=False)
        state.intent = path
        try:
            yield
            path.unlink()
        finally:
            # On any failure keep the intention, even when effect is uncertain.
            state.intent = None

    def pending(self) -> list[dict]:
        """List unresolved evidence; never modify or infer success from it."""
        with self.guard.read():
            directory = self.dir / "pending"
            try:
                if not _plain_evidence_directory(self.dir) or not _plain_evidence_directory(directory):
                    return []
                children = sorted(directory.iterdir())
            except (OSError, VaultError):
                return [_evidence_directory_error("<pending-directory>")]
            rows = []
            try:
                _plain_evidence_directory(directory / "seen")
            except (OSError, VaultError):
                rows.append(_evidence_directory_error("<seen-directory>"))
            for path in children:
                if path.name == "seen":
                    continue
                row = {"id": path.name, "op": "unknown", "paths": {}, "expected_hashes": {}, "ts": "unknown"}
                try:
                    if path.is_symlink() or path.is_junction() or not path.is_file() or path.stat().st_size > 65536:
                        raise ValueError("not a small regular evidence file")
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if (not isinstance(data, dict) or not isinstance(data.get("op"), str)
                            or not isinstance(data.get("paths"), dict)
                            or not all(isinstance(k, str) and isinstance(v, str) for k, v in data["paths"].items())
                            or not isinstance(data.get("expected_hashes"), dict)
                            or not isinstance(data.get("ts"), str)):
                        raise ValueError("invalid evidence")
                    row.update({k: data[k] for k in ("op", "paths", "expected_hashes", "ts")})
                except (OSError, ValueError, RecursionError):
                    row["error"] = "unreadable intention — manual review needed"
                rows.append(row)
            return rows

    def acknowledge_pending(self, ids: list[str]) -> int:
        """Explicit owner's acknowledgement only; keep bytes in seen/."""
        with self.write("acknowledge pending"):
            if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids) or len(ids) != len(set(ids)):
                raise VaultError("invalid intention selection")
            directory = self.dir / "pending"
            _plain_evidence_directory(self.dir)
            _plain_evidence_directory(directory)
            _plain_evidence_directory(directory / "seen")
            existing = {row["id"] for row in self.pending()}
            selected = []
            for name in ids:
                if name not in existing or Path(name).name != name:
                    raise VaultError("intention list changed — refresh")
                path = directory / name
                if path.is_symlink() or path.is_junction() or not path.is_file():
                    raise VaultError("intention needs manual file review")
                target = directory / "seen" / name
                if target.exists():
                    raise VaultError("acknowledged intention already exists")
                selected.append((path, target))
            for path, target in selected:
                # Record the owner's decision before hiding evidence in seen/.
                # Failure/crash before the rename leaves it visibly unresolved.
                self.append("pending_acknowledged", path, target, note="owner acknowledged possible effect; no replay")
                self.effect()
                target.parent.mkdir(exist_ok=True)
                path.rename(target)
            return len(selected)

    def _last_hash(self) -> str:
        last = GENESIS
        for rec in self._iter_raw():
            last = rec["hash"]
        return last

    def _iter_raw(self) -> Iterator[dict[str, Any]]:
        if not self.file.exists():
            return
        with self.file.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def append(
        self,
        op: str,
        src: Path | str,
        dst: Path | str,
        *,
        sha256: str = "",
        size: int = 0,
        note: str = "",
        extra: dict[str, Any] | None = None,
    ) -> Receipt:
        with self.write("receipt"):
            return self._append(op, src, dst, sha256=sha256, size=size, note=note, extra=extra)

    def _append(self, op, src, dst, *, sha256, size, note, extra):
        rec = Receipt(
            op=op,
            src=str(src),
            dst=str(dst),
            sha256=sha256,
            size=size,
            ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            prev_hash=self._last_hash(),
            note=note,
            extra=dict(extra or {}),
        )
        line = dict(rec.body())
        line["hash"] = rec.hash()
        self.effect()
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        return rec

    def verify(self) -> int:
        """Return the number of valid records; raise ValueError on a broken chain."""
        with self.guard.read():
            return self._verify()

    def _verify(self) -> int:
        prev = GENESIS
        count = 0
        for raw in self._iter_raw():
            if not isinstance(raw, dict):
                raise ValueError(f"receipt chain broken at record {count}: invalid record")
            stored = raw.pop("hash", None)
            try:
                rec = Receipt(**raw)
            except TypeError as exc:
                raise ValueError(f"receipt chain broken at record {count}: invalid record") from exc
            if rec.prev_hash != prev:
                raise ValueError(f"receipt chain broken at record {count}: prev_hash mismatch")
            if rec.hash() != stored:
                raise ValueError(f"receipt chain broken at record {count}: content hash mismatch")
            prev = stored
            count += 1
        return count

    def tail(self, n: int = 50) -> list[dict[str, Any]]:
        with self.guard.read():
            items = list(self._iter_raw())
            return items[-n:]
