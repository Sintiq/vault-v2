"""Devices — who may open the vault from outside the PC, one key each.

Every phone or browser gets its own token when the owner pairs it at the PC,
so one lost phone can be shut out without touching the others. The PC keeps
only a SHA-256 of each token; the token itself lives on the device.

Pairing is one QR plus one look: the PC shows a short-lived ticket, the
device claims it and both show the same six digits, the owner confirms at the
PC. Only then does the device receive its token, once.

The old shared key (``.api/key``) stays as the device ``legacy`` until the
owner revokes it; after that only per-device tokens open anything. Nothing
here is revoked or rotated automatically.

Contract: docs/CONTRACT-devices-pin-export-setup.md §1.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from .receipts import ReceiptLog

SCHEMA = "vault-v2-devices@1"
LEGACY_ID = "legacy"
KINDS = ("app", "browser")
TICKET_SECONDS = 5 * 60
NAME_MAX = 60


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _clean_name(value: object, fallback: str) -> str:
    text = "".join(ch if ch.isprintable() else " " for ch in str(value or ""))
    text = " ".join(text.split())[:NAME_MAX].strip()
    return text or fallback


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    kind: str                  # app | browser | legacy
    token_sha256: str
    paired_at: str
    revoked_at: str | None = None
    door: bool = False

    @property
    def active(self) -> bool:
        return self.revoked_at is None


@dataclass
class _Claim:
    ticket: str
    claim_id: str
    code: str
    name: str
    kind: str
    created: float
    state: str = "waiting"     # waiting | confirmed | rejected
    device_id: str | None = None
    token: str | None = None


class PairingError(ValueError):
    """A pairing request that cannot go on; the message is safe to show."""


class DeviceRegistry:
    """Devices of one vault root. Thread-safe; writes go through the root guard."""

    def __init__(self, root: Path, log: ReceiptLog, clock=time.monotonic):
        self.root = Path(root)
        self.dir = self.root / ".api"
        self.file = self.dir / "devices.json"
        self.legacy_file = self.dir / "key"
        self.legacy_revoked_mark = self.dir / "legacy_revoked"
        self._legacy_override: str | None = None
        self.log = log
        self._clock = clock
        self._lock = threading.RLock()
        self._devices: dict[str, Device] = {}
        self._tickets: dict[str, float] = {}        # ticket -> created (monotonic)
        self._claims: dict[str, _Claim] = {}        # claim_id -> claim
        self.last_seen: dict[str, float] = {}       # device_id -> wall time, memory only
        self.collected: set[str] = set()            # devices that picked up their token
        self._load()

    # -- storage ----------------------------------------------------------------

    def _load(self) -> None:
        self._devices = {}
        if self.file.exists():
            data = json.loads(self.file.read_text(encoding="utf-8"))
            for did, raw in data.get("devices", {}).items():
                self._devices[did] = Device(**raw)

    def _save(self, devices: dict[str, Device]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        body = {"schema": SCHEMA, "devices": {k: asdict(v) for k, v in sorted(devices.items())}}
        tmp = self.file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.file)

    def _commit(self, devices: dict[str, Device]) -> None:
        """Write the new set to disk first; only then does this process believe it.
        A failed write leaves memory as the disk is, so a retry cannot report success
        for a change that was never saved."""
        self._save(devices)
        self._devices = devices

    def existing_vault_id(self) -> str:
        """The vault's id if it was made already; never writes (for unauthenticated reads)."""
        try:
            return (self.dir / "vault_id").read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def vault_id(self) -> str:
        """A random name for this vault root, made once, so a device can tell two vaults apart."""
        f = self.dir / "vault_id"
        if f.exists():
            value = f.read_text(encoding="utf-8").strip()
            if value:
                return value
        with self.log.write("create vault id"):
            if f.exists() and f.read_text(encoding="utf-8").strip():
                return f.read_text(encoding="utf-8").strip()
            value = secrets.token_urlsafe(9)
            self.log.effect()
            self.dir.mkdir(parents=True, exist_ok=True)
            f.write_text(value, encoding="utf-8")
            self.log.append("vault_id_created", "", f)
            return value

    # -- the legacy shared key --------------------------------------------------

    def use_legacy_key(self, key: str | None) -> None:
        """The shared key the server was started with. Ignored once the owner revoked it."""
        self._legacy_override = key or None

    @property
    def legacy_revoked(self) -> bool:
        return self.legacy_revoked_mark.exists()

    def _legacy_key(self) -> str | None:
        if self.legacy_revoked:
            return None
        if self._legacy_override:
            return self._legacy_override
        if not self.legacy_file.exists():
            return None
        key = self.legacy_file.read_text(encoding="utf-8").strip()
        return key or None

    def legacy(self) -> Device | None:
        """The shared key as a device, while it still exists."""
        key = self._legacy_key()
        if key is None:
            return None
        return Device(LEGACY_ID, "Key shared by devices paired before per-device keys", "legacy",
                      _sha(key), "", None, False)

    # -- who is asking -------------------------------------------------------------

    def authenticate(self, token: str | None, *, allow_legacy: bool = True) -> Device | None:
        """The active device this token belongs to, or None."""
        if not token:
            return None
        digest = _sha(token.strip())
        with self._lock:
            found = next((d for d in self._devices.values()
                          if secrets.compare_digest(d.token_sha256, digest)), None)
        if found is None and allow_legacy:
            legacy = self.legacy()
            if legacy is not None and secrets.compare_digest(legacy.token_sha256, digest):
                found = legacy
        if found is None or not found.active:
            return None
        self.last_seen[found.id] = time.time()
        return found

    def all(self) -> list[Device]:
        with self._lock:
            devices = sorted(self._devices.values(), key=lambda d: d.paired_at)
        legacy = self.legacy()
        return ([legacy] if legacy else []) + devices

    def get(self, device_id: str) -> Device | None:
        if device_id == LEGACY_ID:
            return self.legacy()
        with self._lock:
            return self._devices.get(device_id)

    # -- pairing --------------------------------------------------------------------

    def _expire(self) -> None:
        now = self._clock()
        for ticket, created in list(self._tickets.items()):
            if now - created > TICKET_SECONDS:
                del self._tickets[ticket]
        for cid, claim in list(self._claims.items()):
            if now - claim.created > TICKET_SECONDS:
                del self._claims[cid]

    def new_ticket(self) -> str:
        """A single-use pairing ticket, valid five minutes, kept in memory only."""
        with self._lock:
            self._expire()
            ticket = secrets.token_urlsafe(16)
            self._tickets[ticket] = self._clock()
            return ticket

    def ticket_alive(self, ticket: str) -> bool:
        """Whether a ticket can still be claimed (not expired, not yet used)."""
        with self._lock:
            self._expire()
            return ticket in self._tickets

    def cancel_ticket(self, ticket: str) -> None:
        with self._lock:
            self._tickets.pop(ticket, None)
            for cid, claim in list(self._claims.items()):
                if claim.ticket == ticket and claim.state == "waiting":
                    del self._claims[cid]

    def claim(self, ticket: object, name: object, kind: object) -> dict:
        """A device presents the ticket it scanned; both sides then show the same code."""
        if not isinstance(ticket, str) or not ticket:
            raise PairingError("no pairing ticket")
        kind = kind if kind in KINDS else "app"
        with self._lock:
            self._expire()
            if ticket not in self._tickets:
                raise PairingError("this pairing code has expired or was already used — make a new one at the PC")
            del self._tickets[ticket]
            claim = _Claim(ticket, secrets.token_urlsafe(12), f"{secrets.randbelow(1_000_000):06d}",
                           _clean_name(name, "Phone" if kind == "app" else "Browser"), kind, self._clock())
            self._claims[claim.claim_id] = claim
            return {"claim_id": claim.claim_id, "code": claim.code}

    def waiting_claim(self, ticket: str) -> dict | None:
        """What the PC dialog shows while it waits: the device's name and code, once claimed."""
        with self._lock:
            self._expire()
            for claim in self._claims.values():
                if claim.ticket == ticket:
                    return {"claim_id": claim.claim_id, "name": claim.name, "kind": claim.kind,
                            "code": claim.code, "state": claim.state}
            return None

    def confirm(self, claim_id: str) -> Device:
        """The owner saw the same code on the device and pressed Confirm at the PC."""
        with self._lock:
            self._expire()
            claim = self._claims.get(claim_id)
            if claim is None or claim.state != "waiting":
                raise PairingError("this pairing request is no longer waiting")
            token = secrets.token_urlsafe(32)
            device = Device(secrets.token_urlsafe(9), claim.name, claim.kind, _sha(token), _now())
            with self.log.write("pair device"):
                self.log.effect()
                self._commit({**self._devices, device.id: device})
                self.log.append("device_paired", device.name, self.file,
                                extra={"device_id": device.id, "kind": device.kind})
            claim.state, claim.device_id, claim.token = "confirmed", device.id, token
            return device

    def reject(self, claim_id: str) -> None:
        with self._lock:
            claim = self._claims.get(claim_id)
            if claim is not None and claim.state == "waiting":
                claim.state = "rejected"

    def status(self, claim_id: object) -> dict:
        """The device polls this; the token is handed out once, then the claim is gone."""
        if not isinstance(claim_id, str):
            raise PairingError("no pairing request")
        with self._lock:
            self._expire()
            claim = self._claims.get(claim_id)
            if claim is None:
                return {"state": "expired"}
            if claim.state == "confirmed":
                del self._claims[claim_id]
                self.collected.add(claim.device_id)
                return {"state": "confirmed", "device_id": claim.device_id, "token": claim.token}
            if claim.state == "rejected":
                del self._claims[claim_id]
                return {"state": "rejected"}
            return {"state": "waiting"}

    # -- changes the owner makes at the PC -------------------------------------------

    def rename(self, device_id: str, name: str) -> Device:
        with self._lock:
            device = self._devices.get(device_id)
            if device is None:
                raise KeyError(device_id)
            renamed = replace(device, name=_clean_name(name, device.name))
            with self.log.write("rename device"):
                self.log.effect()
                self._commit({**self._devices, device_id: renamed})
                self.log.append("device_renamed", renamed.name, self.file, extra={"device_id": device_id})
            return renamed

    def revoke(self, device_id: str, *, op: str = "device_revoked") -> None:
        """Shut a device out from its next request on. Bytes it already has stay where they are."""
        if device_id == LEGACY_ID:
            self._revoke_legacy()
            return
        with self._lock:
            device = self._devices.get(device_id)
            if device is None:
                raise KeyError(device_id)
            with self.log.write("revoke device"):
                # Read the disk, not memory: a revoke that failed to save must not look done.
                self._load()
                device = self._devices.get(device_id)
                if device is None:
                    raise KeyError(device_id)
                if not device.active:
                    # Revoked before, perhaps without the door being removed: finish that now.
                    self._drop_door_of(device_id)
                    return
                self.log.effect()
                self._commit({**self._devices, device_id: replace(device, revoked_at=_now())})
                self.log.append(op, device.name, self.file, extra={"device_id": device_id})
                self._drop_door_of(device_id)

    def _revoke_legacy(self) -> None:
        with self._lock:
            if self.legacy_revoked:
                if self.door_config.exists():
                    with self.log.write("close shared-key door"):
                        self._drop_door_of(LEGACY_ID)
                return
            with self.log.write("revoke shared key"):
                self.log.effect()
                self.dir.mkdir(parents=True, exist_ok=True)
                # The mark comes first: from here on no start of the window brings the
                # shared-key mode back, even with a freshly made key.
                self.legacy_revoked_mark.write_text(_now(), encoding="utf-8")
                self._legacy_override = None
                retired = self.legacy_revoked_mark
                if self.legacy_file.exists():
                    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                    retired = self.dir / f"key.revoked-{stamp}"
                    self.legacy_file.replace(retired)
                self.log.append("device_revoked", "shared key", retired,
                                note="shared phone key revoked; secret omitted", extra={"device_id": LEGACY_ID})
                self._drop_door_of(LEGACY_ID)

    def revoke_all(self) -> int:
        ids = [d.id for d in self.all() if d.active]
        for did in ids:
            self.revoke(did)
        return len(ids)

    # -- the door --------------------------------------------------------------------

    @property
    def door_config(self) -> Path:
        return self.root / ".door" / "config.json"

    def door_owner(self) -> str | None:
        """The device whose phone door the PC uses now, read from the door config itself."""
        try:
            data = json.loads(self.door_config.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        owner = data.get("device_id") if isinstance(data, dict) else None
        return owner if isinstance(owner, str) and owner else None

    def pair_door(self, device: Device, address: str, key: str) -> None:
        """The phone hands over its door; config and owner are written together, and only
        for a device still active at that moment. A later pairing replaces the owner."""
        with self._lock:
            with self.log.write("pair phone door"):
                current = self.get(device.id)
                if current is None or not current.active:
                    raise PairingError("this device is no longer paired")
                self.log.effect()
                self.door_config.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.door_config.with_suffix(".json.tmp")
                tmp.write_text(json.dumps({"address": address, "key": key, "device_id": device.id}, indent=1),
                               encoding="utf-8")
                tmp.replace(self.door_config)
                # The receipt records that pairing happened, never the key itself.
                self.log.append("door_paired", address, self.door_config,
                                note="the phone handed over its door key", extra={"device_id": device.id})

    def door_owner_active(self) -> bool:
        """Whether the door config belongs to a device that may still be reached."""
        owner = self.door_owner()
        if owner in (None, LEGACY_ID):
            return not self.legacy_revoked
        device = self.get(owner)
        return device is not None and device.active

    def _drop_door_of(self, device_id: str) -> None:
        """Close the door only if it is this device's door; another device's stays."""
        if not self.door_config.exists() or self.door_owner() not in (device_id, None if device_id == LEGACY_ID else ""):
            return
        self.door_config.unlink()
        self.log.append("door_unpaired", "agent door", self.door_config,
                        note="door key removed with its device", extra={"device_id": device_id})
