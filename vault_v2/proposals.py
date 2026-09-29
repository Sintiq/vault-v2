"""Immutable shown rows for in-memory, one-use proposal authorization."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass

from .ops import VaultError


class ProposalConflict(VaultError):
    def __init__(self):
        super().__init__("proposals changed — refresh the list")


@dataclass(frozen=True)
class ShownProposal:
    handle: str
    payload: str
    payload_sha256: str
    target: str = ""

    @classmethod
    def freeze(cls, row: dict, target: str = "") -> "ShownProposal":
        handle = secrets.token_urlsafe(32)
        payload = json.dumps({**row, "id": handle}, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False)
        return cls(handle, payload, hashlib.sha256(payload.encode("utf-8")).hexdigest(), target)

    def shown(self) -> dict:
        if hashlib.sha256(self.payload.encode("utf-8")).hexdigest() != self.payload_sha256:
            raise ProposalConflict()
        row = json.loads(self.payload)
        if row.get("id") != self.handle:
            raise ProposalConflict()
        return row
