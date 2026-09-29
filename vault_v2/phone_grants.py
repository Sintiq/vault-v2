"""Files from Staging to the phone, one grant at a time.

The only way bytes leave the PC for the phone as a copy the phone keeps
(«Available offline») or passes on («Save a copy to Files…», «Share…»). The
owner presses the button on the phone; the PC checks, at that moment, that the
device is still paired, that the file is in Staging, and that it is the same
file the phone saw. A grant lives two minutes and belongs to one device.

Receipts say what the PC did and what the phone reported, never more: a
chooser closed without a target is «cancelled», a mail app chosen is
«handed_to <app>», and nothing here ever says «sent».

Contract: docs/CONTRACT-devices-pin-export-setup.md §4.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from .devices import LEGACY_ID, Device
from .file_access import visible_file
from .ops import VaultError, VaultOps

ACTIONS = ("offline", "save", "share")
# "unknown": the phone could not tell (a share sheet that gave no answer). Never "sent".
OUTCOMES = ("saved", "handed_to", "cancelled", "failed", "stored", "unknown")
GRANT_SECONDS = 120
MAX_BYTES = 100 * 1024 * 1024


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class Grant:
    id: str
    device_id: str
    path: Path
    rel: str
    name: str
    sha256: str
    size: int
    action: str
    created: float
    served: bool = False
    reported: bool = False
    in_flight: bool = False


class PhoneGrants:
    def __init__(self, ops: VaultOps, clock=time.monotonic):
        self.ops = ops
        self._clock = clock
        self._lock = threading.Lock()
        self._grants: dict[str, Grant] = {}

    def _staging_file(self, rel: object) -> Path:
        """A plain relative name inside Staging, checked in both path grammars and after joining."""
        if (not isinstance(rel, str) or not rel or "\\" in rel or ":" in rel or rel.startswith("/")
                or PurePosixPath(rel).is_absolute() or PureWindowsPath(rel).anchor or PureWindowsPath(rel).drive
                or any(part in ("", ".", "..") for part in rel.split("/"))):
            raise VaultError("the file must be named by its place in Staging")
        staging = self.ops.paths.staging.absolute()
        path = visible_file(self.ops.paths, staging / rel)
        if not path.resolve().is_relative_to(staging.resolve()):
            raise VaultError("the file must be in Staging")
        return path

    def _expire(self) -> None:
        now = self._clock()
        for gid, g in list(self._grants.items()):
            if now - g.created > GRANT_SECONDS:
                del self._grants[gid]

    def grant(self, device: Device, rel: object, action: object, sha256: object = None) -> dict:
        """The owner pressed a button on the phone; check everything again, here and now."""
        if device is None or device.id == LEGACY_ID:
            raise PermissionError("taking files to a phone needs a device paired with its own key")
        if action not in ACTIONS:
            raise VaultError("unknown action")
        path = self._staging_file(rel)
        size = path.stat().st_size
        if size > MAX_BYTES:
            raise VaultError(f"too large for the phone: {size // (1024 * 1024)} MB")
        digest = _sha256(path.read_bytes())
        if sha256 is not None and sha256 != digest:
            raise VaultError("this file changed on the PC since the phone saw it — refresh and choose again")
        g = Grant(secrets.token_urlsafe(16), device.id, path, str(rel), path.name, digest, size, str(action),
                  self._clock())
        with self._lock:
            self._expire()
            self._grants[g.id] = g
        return {"grant_id": g.id, "name": g.name, "size": g.size, "sha256": g.sha256, "action": g.action}

    def _get(self, device: Device, grant_id: object) -> Grant:
        with self._lock:
            self._expire()
            g = self._grants.get(grant_id) if isinstance(grant_id, str) else None
        if g is None or device is None or g.device_id != device.id:
            raise VaultError("this grant has expired — press the button again")
        return g

    def blob(self, device: Device, grant_id: object) -> tuple[bytes, str]:
        """The exact bytes that were granted, delivered once; a second request gets nothing."""
        g = self._get(device, grant_id)
        with self._lock:
            if g.served or g.in_flight:
                raise VaultError("this grant was already delivered — press the button again for another copy")
            g.in_flight = True
        try:
            path = self._staging_file(g.rel)          # the same checks again, not only the hash
            data = path.read_bytes()
            if _sha256(data) != g.sha256 or len(data) != g.size:
                raise VaultError("this file changed on the PC since the grant — press the button again")
            op = "phone_offline_copy" if g.action == "offline" else "phone_export_issued"
            self.ops.log.append(op, path, device.name, sha256=g.sha256, size=g.size,
                                extra={"device_id": device.id, "rel": g.rel, "action": g.action, "grant": g.id})
            with self._lock:
                g.served = True
        finally:
            with self._lock:
                g.in_flight = False
        return data, g.name

    def outcome(self, device: Device, grant_id: object, outcome: object, target: object = None) -> dict:
        """What the phone saw happen to a copy it received. Recorded as the phone's report."""
        g = self._get(device, grant_id)
        if outcome not in OUTCOMES:
            raise VaultError("unknown outcome")
        with self._lock:
            if not g.served:
                raise VaultError("nothing was sent for this grant")
            if g.reported:
                return {"recorded": False}
            g.reported = True
        target_name = ""
        if outcome == "handed_to":
            target_name = "".join(ch for ch in str(target or "") if ch.isalnum() or ch in "._-")[:120]
        self.ops.log.append("phone_export_outcome", g.path, device.name, sha256=g.sha256,
                            note="reported by the phone; not observed by the PC",
                            extra={"device_id": device.id, "rel": g.rel, "action": g.action, "grant": g.id,
                                   "outcome": outcome, "target": target_name})
        return {"recorded": True}
