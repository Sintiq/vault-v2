"""HTTP API of the vault, for the phone.

Design:
- binds to the Tailscale address only, never 0.0.0.0, so the vault is not
  exposed on a cafe wifi by accident;
- every request needs a device token (Authorization: Bearer <token>); each
  phone or browser gets its own when the owner pairs it at the PC, so one can
  be shut out without the others (devices.py). The old shared key in
  <root>/.api/key keeps working as the device "legacy" until the owner revokes
  it. Tailscale already gives encryption and device identity; the token is the
  second lock, not the only one;
- reads and writes go through the same VaultOps / stores as the desktop
  window, so receipts are written exactly as if the owner clicked;
- the gate stays on the PC: the desk Export is desk-only. The phone gets
  exactly the Staging grant path of the devices contract and nothing wider.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import time
import threading
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .agent import LocalModelUnavailable
from .agent_door import DoorError
from .agent_api import ProposalConflict
from . import cards as card_catalog
from .cards import CardStore, content_kind
from .errors import MayHaveApplied, VaultBusy, VaultReadOnly, VaultWriteBlocked
from .devices import LEGACY_ID, Device, DeviceRegistry, PairingError
from .file_access import visible_file, visible_directory, visible_children
from .phone_grants import PhoneGrants
from .health import HealthStore
from .ops import VaultError, VaultOps
from .paths import PANE_NAMES
from .pdf_preview import PdfPreviewBusy, read_pdf_info, render_pdf_page
from .dates import days_left
from .document_budget import DocumentBudgetExceeded, DocumentRequestRefused
from .receipts import ReceiptLog
from .tasks import TaskStore

if False:  # typing only
    from .agent_api import AgentAPI

API_PORT = 8777
UPLOAD_MAX_BYTES = 64 * 1024 * 1024
BLOB_MAX_BYTES = 40 * 1024 * 1024
TEXT_VIEW_MAX_BYTES = 2 * 1024 * 1024

# What the phone is allowed to fetch whole, and as what. Most real documents
# are a scan or a photograph, so a vault the phone can only read .txt out of
# is a vault the phone cannot really show. The list is an allowlist on
# purpose: an extension that is not here is not served as anything.
VIEWABLE: dict[str, tuple[str, str]] = {
    ".pdf": ("application/pdf", "PDF"),
    ".jpg": ("image/jpeg", "IMAGE"),
    ".jpeg": ("image/jpeg", "IMAGE"),
    ".png": ("image/png", "IMAGE"),
    ".gif": ("image/gif", "IMAGE"),
    ".webp": ("image/webp", "IMAGE"),
    ".heic": ("image/heic", "IMAGE"),
}


def view_kind(path: Path) -> str:
    """How a client can show this file: TEXT, IMAGE, PDF, or not at all."""
    if content_kind(path) == "TEXT":
        return "TEXT"
    return VIEWABLE.get(Path(path).suffix.lower(), ("", "NONE"))[1]


def api_port(settings: dict | None = None) -> int:
    """Port for the phone server: VAULT_V2_API_PORT, else vault.json, else default."""
    for raw in (os.environ.get("VAULT_V2_API_PORT"), (settings or {}).get("api_port")):
        try:
            port = int(raw)
        except (TypeError, ValueError):
            continue
        if 1024 <= port <= 65535:
            return port
    return API_PORT


def tailscale_ip(attempts: int = 3) -> str | None:
    """This machine's Tailscale address, or None when Tailscale is not up.

    Asked more than once on purpose: at logon this runs while the machine is
    still busy, and one slow answer used to leave the vault bound to
    localhost for the rest of the day, unreachable from the phone.
    """
    for attempt in range(attempts):
        try:
            out = subprocess.run(["tailscale", "ip", "-4"],
                                 capture_output=True, text=True, timeout=10)
            ip = out.stdout.strip().splitlines()[0].strip() if out.stdout.strip() else ""
            if ip:
                return ip
        except (OSError, subprocess.SubprocessError, IndexError):
            pass
        if attempt + 1 < attempts:
            time.sleep(1.5)
    return None


def front_door(settings: dict | None = None) -> str:
    """Which way in the phone uses.

    "direct"  — listen on the Tailscale address ourselves, plain HTTP inside
                the encrypted tunnel. Works everywhere, but a phone browser
                shows a not-secure warning because a tailnet has no CA.
    "proxied" — listen on localhost only and let Tailscale be the front door
                with its own certificate. Nothing is exposed on the tailnet
                directly, and the browser sees real HTTPS. Requires HTTPS
                certificates to be enabled for the tailnet.
    """
    value = str((settings or {}).get("front", "direct")).strip().lower()
    return value if value in ("proxied", "local") else "direct"


def bind_host(settings: dict | None = None) -> str | None:
    """The address to listen on: localhost when Tailscale fronts us, else our
    Tailscale address. Never 0.0.0.0 — the vault is not for the local wifi."""
    if front_door(settings) in ("proxied", "local"):
        return "127.0.0.1"
    return tailscale_ip()


def existing_legacy_key(root: Path) -> str:
    """The shared key of a vault set up before per-device keys, or "" — never a new one."""
    f = root / ".api" / "key"
    if (root / ".api" / "legacy_revoked").exists() or not f.exists():
        return ""
    return f.read_text(encoding="utf-8").strip()


def load_or_create_key(root: Path, log: ReceiptLog | None = None) -> str:
    """Tests and tools only: the window uses existing_legacy_key and never makes one."""
    d = root / ".api"
    f = d / "key"
    if (d / "legacy_revoked").exists():
        return ""
    if f.exists():
        key = f.read_text(encoding="utf-8").strip()
        if key:
            return key
    log = log or ReceiptLog(root / ".receipts")
    with log.write("create phone API key"):
        # Another thread may have created it while this caller waited.
        if f.exists():
            key = f.read_text(encoding="utf-8").strip()
            if key:
                return key
        key = secrets.token_urlsafe(18)
        log.effect()
        d.mkdir(parents=True, exist_ok=True)
        f.write_text(key, encoding="utf-8")
        log.append("api_key_created", "", f, note="phone API key created; secret omitted")
        return key


class VaultAPI:
    """Everything the phone may ask for. No export: the gate stays at the desk."""

    def __init__(self, ops: VaultOps, cards: CardStore, tasks: TaskStore, health: HealthStore,
                 agent: "AgentAPI | None" = None, devices: DeviceRegistry | None = None):
        self.ops = ops
        self.cards = cards
        self.tasks = tasks
        self.health = health
        self.agent = agent
        self.devices = devices if devices is not None else DeviceRegistry(ops.paths.root, ops.log)
        self.phone = PhoneGrants(ops)

    # -- helpers --------------------------------------------------------------

    def _pane_dir(self, pane: str, rel: str = "") -> Path:
        if pane not in PANE_NAMES:
            raise VaultError(f"unknown pane: {pane}")
        if Path(rel).anchor or ".." in Path(rel).parts:
            raise VaultError("path escapes the pane")
        # Preserve aliases for VaultOps' whole-tree link preflight.
        return self.ops.paths.pane(pane).absolute() / rel

    def _entry(self, p: Path, pane: str) -> dict:
        root = self.ops.paths.pane(pane).absolute()
        card = None if p.is_dir() else self.cards.for_path(p)
        return {
            "name": p.name,
            "rel": p.relative_to(root).as_posix(),
            "dir": p.is_dir(),
            "size": 0 if p.is_dir() else p.stat().st_size,
            "modified": int(p.stat().st_mtime),
            "kind": "DIR" if p.is_dir() else content_kind(p),
            "view": "DIR" if p.is_dir() else view_kind(p),
            "shelf": card.shelf if card else None,
            "confirmed": bool(card.confirmed) if card else None,
            "topics": list(card.topics) if card else [],
            "issuer": card.issuer if card else None,
            "year": card.year if card else None,
            "recipients": list(card.recipients) if card else [],
        }

    # -- read -----------------------------------------------------------------

    def _file_path(self, pane: str, rel: str) -> Path:
        # Keep lexical components intact for the common visible-file check.
        if pane not in PANE_NAMES or not rel or Path(rel).anchor:
            raise VaultError("File must name a relative path in a visible pane")
        return self.ops.paths.pane(pane).absolute() / rel

    def pdf_info(self, pane: str, rel: str) -> dict:
        info = read_pdf_info(self.ops.paths, self._file_path(pane, rel), wait=False)
        return {"pages": info.page_count, "warning": info.warning}

    def pdf_page(self, pane: str, rel: str, page: int, width: int = 1080) -> bytes:
        if type(page) is int and page >= 50:
            raise VaultError("too many pages for the phone")
        return render_pdf_page(self.ops.paths, self._file_path(pane, rel), page, width, wait=False).png

    def list_shelves(self) -> dict:
        return {"shelves": list(card_catalog.SHELVES),
                "standard": list(card_catalog.DEFAULT_SHELVES)}

    def list_pane(self, pane: str, rel: str = "") -> dict:
        if pane not in PANE_NAMES or Path(rel).anchor:
            raise VaultError("Directory must name a relative path in a visible pane")
        d = visible_directory(self.ops.paths, self.ops.paths.pane(pane).absolute() / rel)
        items = []
        for p in sorted(visible_children(self.ops.paths, d), key=lambda x: (not x.is_dir(), x.name.lower())):
            items.append(self._entry(p, pane))
        return {"pane": pane, "rel": rel, "items": items}

    def read_file(self, pane: str, rel: str) -> dict:
        p = visible_file(self.ops.paths, self._file_path(pane, rel))
        if content_kind(p) != "TEXT":
            raise VaultError("not a text document")
        with p.open("rb") as stream:
            raw = stream.read(TEXT_VIEW_MAX_BYTES + 1)
        text = raw[:TEXT_VIEW_MAX_BYTES].decode("utf-8", errors="replace")
        return {"name": p.name, "rel": rel, "text": text, "truncated": len(raw) > TEXT_VIEW_MAX_BYTES}

    def blob(self, pane: str, rel: str) -> tuple[bytes, str, str]:
        """The file itself, for a viewer. Bytes, content type, name."""
        p = visible_file(self.ops.paths, self._file_path(pane, rel))
        entry = VIEWABLE.get(p.suffix.lower())
        if entry is None:
            raise VaultError(f"this vault does not serve {p.suffix or 'that'} files to a viewer")
        size = p.stat().st_size
        if size > BLOB_MAX_BYTES:
            raise VaultError(f"too large to view on a phone: {size // (1024 * 1024)} MB")
        return p.read_bytes(), entry[0], p.name

    def list_tasks(self) -> dict:
        rows = []
        for t in self.tasks.all():
            remaining = days_left(t.due)
            rows.append({"id": t.id, "title": t.title, "due": t.due, "quote": t.quote,
                         "doc": t.doc_name, "done": t.done, "origin": t.origin,
                         "due_source": t.due_source, "flags": list(t.flags),
                         "days_left": remaining, "overdue": remaining is not None and remaining < 0})
        return {"tasks": rows}

    def list_health(self) -> dict:
        return {
            "summary": self.health.summary(),
            "years": [
                {"year": year, "entries": [
                    {"id": e.id, "date": e.date, "kind": e.kind, "label": e.label, "quote": e.quote, "doc": e.doc_name,
                     "due_source": e.due_source, "flags": list(e.flags)}
                    for e in entries
                ]}
                for year, entries in self.health.by_year()
            ],
        }

    def list_receipts(self, n: int = 60) -> dict:
        try:
            total = self.ops.log.verify()
            intact = True
        except ValueError:
            total, intact = -1, False
        rows = self.ops.log.tail(n)
        return {"total": total, "intact": intact, "rows": [
            {"ts": r["ts"], "op": r["op"], "src": Path(r["src"]).name, "dst": Path(r["dst"]).name, "sha256": r["sha256"][:12]}
            for r in rows
        ]}

    # -- write (same receipts as a click at the desk) -------------------------

    def upload(self, filename: str, data: bytes) -> dict:
        # Network input was received before entry; only local effects hold the
        # root guard. Cleanup is part of the same guarded operation.
        with self.ops.log.write("phone upload"):
            safe = Path(filename).name or "upload.bin"
            tmp = self.ops.paths.root / ".api" / "incoming"
            self.ops.log.effect()
            tmp.mkdir(parents=True, exist_ok=True)
            staged = tmp / safe
            try:
                staged.write_bytes(data)
                res = self.ops.import_file(staged, "staging", note="uploaded from the phone")
            finally:
                staged.unlink(missing_ok=True)
        return {"name": res.dst.name, "sha256": res.sha256, "size": res.size}

    def transfer(self, pane: str, rel: str, to_pane: str, move: bool) -> dict:
        src = self._pane_dir(pane, rel)
        dst_dir = self._pane_dir(to_pane)
        res = (self.ops.move if move else self.ops.copy)(src, dst_dir, note="from the phone")
        return {"op": res.op, "name": res.dst.name, "pane": to_pane}

    def trash(self, pane: str, rel: str) -> dict:
        res = self.ops.trash(self._pane_dir(pane, rel), note="from the phone")
        return {"name": Path(res.src).name}

    def pair_door(self, address: str, key: str, device: Device | None = None) -> dict:
        """The phone hands over its door key over the channel it is already
        trusted on. Nobody has to read the key off a screen and type it in,
        which is the step where a secret usually ends up somewhere it should
        not be."""
        address = str(address).strip().rstrip("/")
        key = str(key).strip()
        if not address.startswith("http://") and not address.startswith("https://"):
            raise VaultError("the door address must be a URL")
        if not key:
            raise VaultError("the door sent no key")
        if device is None:
            raise VaultError("the door must be paired by a paired device")
        self.devices.pair_door(device, address, key)
        return {"paired": True, "address": address}

    def session(self, device: Device) -> dict:
        """Who this device is to this vault; the phone's green dot rests on this answer."""
        return {"vault_id": self.devices.vault_id(), "api_version": API_VERSION,
                "device_id": device.id, "device_name": device.name, "legacy": device.id == LEGACY_ID}

    def set_card_shelf(self, pane: str, rel: str, shelf: str) -> dict:
        if not all(isinstance(value, str) for value in (pane, rel, shelf)):
            raise VaultError("pane, rel and shelf must be strings")
        with self.ops.log.write("set file shelf"):
            self._pane_dir(pane, rel)  # containment, without losing the lexical path below
            card = self.cards.set_shelf(self.ops.paths.pane(pane) / rel, shelf)
            return {"pane": pane, "rel": rel, "card": card.to_json()}

    def set_task_due(self, tid: str, due: str | None) -> dict:
        if not isinstance(tid, str):
            raise VaultError("id must be a string")
        self.tasks.set_due(tid, due)
        return {"id": tid, "due": due, "due_source": "owner", "flags": []}

    def set_health_date(self, eid: str, value: str | None) -> dict:
        if not isinstance(eid, str):
            raise VaultError("id must be a string")
        self.health.set_date(eid, value)
        return {"id": eid, "date": value, "due_source": "owner", "flags": []}

    def set_task_done(self, tid: str, done: bool) -> dict:
        self.tasks.set_done(tid, done)
        return {"id": tid, "done": done}

    def add_watch_readings(self, body: dict) -> dict:
        """One day from the watch into the Health timeline, on the owner's press on the phone."""
        if not isinstance(body, dict):
            raise VaultError("request body must be a JSON object")
        entries = self.health.add_watch(body.get("day"), body.get("readings"))
        return {"added": len(entries), "labels": [e.label for e in entries], "generation": self.health.generation}


API_VERSION = 2

RELEASE_FILES = {
    "/app/vault.apk": ("vault.apk", "application/vnd.android.package-archive"),
    "/app/vault-release.json": ("vault-release.json", "application/json"),
    "/app/vault-release.json.sig": ("vault-release.json.sig", "text/plain"),
}

WEB_ASSETS = {
    "/manifest.webmanifest": "application/manifest+json",
    "/icon-192.png": "image/png",
    "/icon-512.png": "image/png",
}


class _Handler(BaseHTTPRequestHandler):
    server_version = "VaultV2"
    api: VaultAPI = None       # set by ApiServer.start()
    key: str = ""              # the legacy shared key; devices.py decides who is in
    release_dir: Path | None = None   # where the phone app and its signed description live
    device: Device | None = None
    page: Path = None
    chat_send = None           # callable(text) -> str
    chat_state = None          # callable() -> dict

    def log_message(self, fmt, *args):  # quiet: the vault has its own receipts
        pass

    # -- plumbing -------------------------------------------------------------

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8",
              filename: str | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            # inline, so a phone shows the scan instead of downloading it
            safe = filename.replace('"', "")
            self.send_header("Content-Disposition", f'inline; filename="{safe}"')
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _error(self, code: int, msg: str) -> None:
        self._json({"error": msg}, code)

    def _authorised(self) -> bool:
        """Sets self.device to the paired device making this request, if any."""
        self.device = None
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            self.device = self.api.devices.authenticate(header[7:].strip())
            return self.device is not None
        qs = parse_qs(urlparse(self.path).query)
        if qs.get("key"):
            # The old QR link: the shared key only, never a device token.
            legacy = self.api.devices.authenticate(qs["key"][0])
            self.device = legacy if legacy is not None and legacy.id == LEGACY_ID else None
        return self.device is not None

    # -- routes ---------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        path = url.path
        qs = {k: v[0] for k, v in parse_qs(url.query, keep_blank_values=path.startswith("/api/pdf/")).items()}

        if path in ("/", "/index.html"):
            self._send(200, self.page.read_bytes(), "text/html; charset=utf-8")
            return
        if path in RELEASE_FILES:
            # The phone app and its signed description: fixed release files from the program
            # folder, no key and no data in them. Anything else under /app does not exist.
            name, ctype = RELEASE_FILES[path]
            f = (self.release_dir or Path(__file__).resolve().parent / "web") / name
            if not f.is_file():
                self._error(404, "this installation carries no phone app")
                return
            self._send(200, f.read_bytes(), ctype, filename=name if name.endswith(".apk") else None)
            return
        if path in WEB_ASSETS:
            # The manifest and icons let a phone browser pin the page to its home screen.
            self._send(200, (self.page.parent / path.lstrip("/")).read_bytes(), WEB_ASSETS[path])
            return
        if path == "/api/ping":
            # Reachability only: answers without a key and proves nothing about access.
            self._json({"ok": True, "name": "Vault V2", "api_version": API_VERSION,
                        "vault_id": self.api.devices.existing_vault_id()})
            return
        if not self._authorised():
            self._error(401, "bad key")
            return
        try:
            if path == "/api/session":
                self._json(self.api.session(self.device))
            elif path == "/api/files":
                self._json(self.api.list_pane(qs.get("pane", "staging"), unquote(qs.get("rel", ""))))
            elif path == "/api/shelves":
                self._json(self.api.list_shelves())
            elif path == "/api/file":
                self._json(self.api.read_file(qs["pane"], unquote(qs["rel"])))
            elif path == "/api/pdf/info":
                self._json(self.api.pdf_info(qs["pane"], qs["rel"]))
            elif path == "/api/pdf/page":
                try:
                    page, width = int(qs["page"]), int(qs.get("width", "1080"))
                except ValueError as exc:
                    raise VaultError("page and width must be integers") from exc
                self._send(200, self.api.pdf_page(qs["pane"], qs["rel"], page, width), "image/png")
            elif path == "/api/blob":
                data, ctype, name = self.api.blob(qs["pane"], unquote(qs["rel"]))
                self._send(200, data, ctype, filename=name)
            elif path == "/api/phone/blob":
                data, name = self.api.phone.blob(self.device, qs.get("grant"))
                self._send(200, data, "application/octet-stream", filename=name)
            elif path == "/api/tasks":
                self._json(self.api.list_tasks())
            elif path == "/api/health":
                self._json(self.api.list_health())
            elif path == "/api/receipts":
                self._json(self.api.list_receipts())
            elif path == "/api/chat":
                self._json(self.chat_state())
            elif path == "/api/monitor":
                self._json({"available": False,
                            "pending_intentions": len(self.api.ops.log.pending()),
                            "note": "watch readings arrive through the phone's Watch tab; "
                                    "Add to Health writes them into the timeline"})
            else:
                self._error(404, "no such endpoint")
        except PdfPreviewBusy as exc:
            self._error(503, str(exc))
        except PermissionError as exc:
            self._error(403, str(exc))
        except (VaultError, KeyError, OSError) as exc:
            self._error(400, str(exc))

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        path = url.path
        if path in ("/api/pair/claim", "/api/pair/status"):
            self._pairing(path)
            return
        if not self._authorised():
            self._error(401, "bad key")
            self._finish_rejected_post()
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length > UPLOAD_MAX_BYTES:
            self._error(413, "too large")
            return
        raw = self.rfile.read(length) if length else b""
        try:
            # This only sets the acquire policy; it holds no lock while the
            # model, networking or response body runs. Local write blocks
            # acquire separately and refuse promptly if the root is busy.
            with self.api.ops.log.nonblocking():
                self._dispatch_post(path, raw)
        except ProposalConflict as exc:
            self._error(409, str(exc))
        except DocumentBudgetExceeded as exc:
            self._json({"error": str(exc), "documents": list(exc.documents),
                        "notes": list(exc.reading_notes)}, 413)
        except DocumentRequestRefused as exc:
            self._json({"error": str(exc), "documents": list(exc.documents),
                        "notes": list(exc.reading_notes)}, 422)
        except LocalModelUnavailable as exc:
            self._error(503, exc.public_message)
        except DoorError as exc:
            self._error(503, exc.public_message)
        except (VaultBusy, VaultWriteBlocked, MayHaveApplied) as exc:
            self._error(503, str(exc))
        except VaultReadOnly as exc:
            self._error(403, str(exc))
        except PermissionError as exc:
            self._error(403, str(exc))
        except (VaultError, KeyError, ValueError, OSError) as exc:
            self._error(400, str(exc))

    def _dispatch_post(self, path: str, raw: bytes) -> None:
        if path == "/api/upload":
            name, data = self._multipart(raw)
            self._json(self.api.upload(name, data))
            return
        body = json.loads(raw.decode("utf-8")) if raw else {}
        if path == "/api/transfer":
            self._json(self.api.transfer(body["pane"], body["rel"], body["to"], bool(body.get("move"))))
        elif path == "/api/trash":
            self._json(self.api.trash(body["pane"], body["rel"]))
        elif path == "/api/task":
            self._json(self.api.set_task_done(body["id"], bool(body["done"])))
        elif path in ("/api/card/shelf", "/api/task/due", "/api/health/date"):
            if not isinstance(body, dict):
                raise VaultError("request body must be a JSON object")
            if path == "/api/card/shelf":
                self._json(self.api.set_card_shelf(body["pane"], body["rel"], body["shelf"]))
            elif path == "/api/task/due":
                self._json(self.api.set_task_due(body["id"], body["due"]))
            else:
                self._json(self.api.set_health_date(body["id"], body["date"]))
        elif path == "/api/health/watch":
            self._json(self.api.add_watch_readings(body))
        elif path == "/api/door/pair":
            self._json(self.api.pair_door(body["address"], body["key"], self.device))
        elif path == "/api/phone/grant":
            if not isinstance(body, dict):
                raise VaultError("request body must be a JSON object")
            self._json(self.api.phone.grant(self.device, body.get("rel"), body.get("action"), body.get("sha256")))
        elif path == "/api/phone/outcome":
            if not isinstance(body, dict):
                raise VaultError("request body must be a JSON object")
            self._json(self.api.phone.outcome(self.device, body.get("grant_id"), body.get("outcome"),
                                              body.get("target")))
        elif path == "/api/device/self-wipe":
            # The phone says it is erasing itself after too many wrong PINs. The PC did not
            # see that happen; it records the request and shuts this token out.
            if self.device is not None and self.device.id != LEGACY_ID:
                self.api.devices.revoke(self.device.id, op="device_self_wipe_requested")
            self._json({"revoked": True})
        elif path.startswith("/api/agent/"):
            self._json(self._agent(path, body))
        elif path == "/api/chat":
            text = str(body.get("text", "")).strip()
            if not text:
                self._error(400, "empty message")
                return
            self._json({"reply": self.chat_send(text)})
        else:
            self._error(404, "no such endpoint")

    def _agent(self, path: str, body: dict) -> dict:
        """The agent's jobs from the phone. A vault without an agent half says so."""
        if not isinstance(body, dict):
            if path in ("/api/agent/sort/confirm", "/api/agent/tasks/add", "/api/agent/health/add"):
                raise ProposalConflict()
            raise VaultError("request body must be a JSON object")
        agent = self.api.agent
        if agent is None:
            raise VaultError("this vault has no agent")
        routes = {
            "/api/agent/sort": lambda: agent.sort_propose(),
            "/api/agent/sort/confirm": lambda: agent.sort_confirm(body.get("items")),
            "/api/agent/tasks": lambda: agent.tasks_propose(),
            "/api/agent/tasks/add": lambda: agent.tasks_add(body.get("ids")),
            "/api/agent/health": lambda: agent.health_propose(),
            "/api/agent/health/add": lambda: agent.health_add(body.get("ids")),
            "/api/agent/ask": lambda: agent.ask(body.get("phrase", "")),
        }
        handler = routes.get(path)
        if handler is None:
            raise VaultError("no such agent call")
        return handler()

    def _finish_rejected_post(self) -> None:
        """Deliver the refusal before closing over a late, unread request body.

        On Windows, closing with unread TCP bytes can replace our 401 with a
        reset at the client. Half-close the response, then discard at most
        64 KiB / 250 ms of incoming bytes. Never parse or dispatch them, never
        trust Content-Length, and never retain the connection for another call.
        Delivery beyond these finite limits remains best effort.
        """
        self.close_connection = True
        try:
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_WR)
            deadline = time.monotonic() + 0.25
            remaining_bytes = 64 * 1024
            while remaining_bytes:
                remaining_time = deadline - time.monotonic()
                if remaining_time <= 0:
                    break
                self.connection.settimeout(remaining_time)
                chunk = self.connection.recv(min(8192, remaining_bytes))
                if not chunk:
                    break
                remaining_bytes -= len(chunk)
        except OSError:
            # A peer may already have closed; it still has no authority.
            pass

    def _pairing(self, path: str) -> None:
        """The two pairing calls a device makes before it has a token of its own.

        Nobody is authenticated here, so the body is read only with a valid length of at
        most 4 KiB and within a few seconds; anything else is refused before reading."""
        raw_length = self.headers.get("Content-Length")
        if raw_length is None or not raw_length.strip().isdigit():
            self._error(411, "a length is required")
            return
        length = int(raw_length)
        if length > 4096:
            self._error(413, "too large")
            return
        try:
            deadline = time.monotonic() + 5
            raw = b""
            while len(raw) < length:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("pairing body too slow")
                self.connection.settimeout(remaining)
                chunk = self.rfile.read1(length - len(raw))
                if not chunk:
                    break
                raw += chunk
            if len(raw) != length:
                self._error(400, "incomplete request")
                return
            body = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(body, dict):
                raise PairingError("request body must be a JSON object")
            if path == "/api/pair/claim":
                self._json(self.api.devices.claim(body.get("ticket"), body.get("name"), body.get("kind")))
            else:
                self._json(self.api.devices.status(body.get("claim_id")))
        except PairingError as exc:
            self._error(409, str(exc))
        except (ValueError, UnicodeDecodeError):
            self._error(400, "bad request")
        except (TimeoutError, OSError):
            self.close_connection = True
        except (VaultBusy, VaultWriteBlocked) as exc:
            self._error(503, str(exc))

    def _multipart(self, raw: bytes) -> tuple[str, bytes]:
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            return self.headers.get("X-Filename", "upload.bin"), raw
        msg = BytesParser(policy=email_policy).parsebytes(
            b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + raw)
        for part in msg.iter_parts():
            name = part.get_filename()
            if name:
                return name, part.get_payload(decode=True)
        raise VaultError("no file in the upload")


class _TrackedHTTPServer(ThreadingHTTPServer):
    """Count accepted handlers before starting workers, including queued work."""

    def __init__(self, *args, **kwargs):
        self._activity_lock = threading.Lock()
        self._active_requests = 0
        super().__init__(*args, **kwargs)

    @property
    def active_requests(self) -> int:
        with self._activity_lock:
            return self._active_requests

    def process_request(self, request, client_address):
        with self._activity_lock:
            self._active_requests += 1
        try:
            super().process_request(request, client_address)
        except BaseException:
            with self._activity_lock:
                self._active_requests -= 1
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._activity_lock:
                self._active_requests -= 1


class ApiServer:
    def __init__(self, api: VaultAPI, key: str, page: Path, chat_send, chat_state,
                 host: str | None, port: int = API_PORT, release_dir: Path | None = None):
        self.api, self.key, self.page = api, key, page
        self.release_dir = release_dir
        self.chat_send, self.chat_state = chat_send, chat_state
        self.host = host or "127.0.0.1"
        self.port = port
        self._httpd: _TrackedHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._stopping = False

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def start(self) -> None:
        if self._httpd is not None:
            raise VaultError("phone server is already running or draining")
        self._stopping = False
        self.api.devices.use_legacy_key(self.key)
        handler = type("Handler", (_Handler,), {
            "api": self.api, "key": self.key, "page": self.page, "release_dir": self.release_dir,
            "chat_send": staticmethod(self.chat_send), "chat_state": staticmethod(self.chat_state)})
        self._httpd = _TrackedHTTPServer((self.host, self.port), handler)
        self.port = self._httpd.server_address[1]
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> bool:
        """Stop admissions; False means callers must keep the runtime lease.

        In-flight model/network calls are not interrupted or waited on here.
        Their local effects remain guarded; the owner retries closing later.
        """
        if self._httpd is not None:
            if not self._stopping:
                self._stopping = True
                self._httpd.shutdown()
                self._httpd.server_close()
            if self._httpd.active_requests:
                return False
            self._httpd = None
        return True

    @property
    def active_requests(self) -> int:
        return self._httpd.active_requests if self._httpd is not None else 0

    @property
    def running(self) -> bool:
        return self._httpd is not None and not self._stopping


def free_port(host: str, port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False
