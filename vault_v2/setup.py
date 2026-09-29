"""Setup — from a fresh install to a paired phone, without a terminal.

The wizard checks facts and shows them; it changes one thing, and only when
the owner presses its button: it asks the standard Tailscale to serve this
vault over HTTPS inside the owner's own tailnet. It never runs `serve reset`,
`down`, `logout` or Funnel, never touches a route it did not add, and undoes
only a route it added and that nobody has changed since.

Before the change it writes down what it is about to do (so a crash leaves a
visible «pending», not a route nobody owns); the Tailscale command runs
outside the vault's write guard; afterwards it records what Tailscale shows.
A listener it cannot fully read, or one with Funnel on, is never «done».

A step is green because the fact was observed, not because a button was
pressed: the phone step turns green when a phone paired in this wizard has
opened the vault with its own key.

Contract: docs/CONTRACT-devices-pin-export-setup.md §5.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import ssl
import subprocess
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .devices import DeviceRegistry
from .receipts import ReceiptLog

DONE, NEEDS_YOU, UNKNOWN, NOT_STARTED = "done", "needs_you", "unknown", "not_started"
STEP_IDS = ("tailscale", "signed_in", "https", "served", "phone")

TAILSCALE_DOWNLOAD = "https://tailscale.com/download/windows"
TAILSCALE_ADMIN_DNS = "https://login.tailscale.com/admin/dns"
TAILSCALE_ANDROID = "https://play.google.com/store/apps/details?id=com.tailscale.ipn"


@dataclass(frozen=True)
class Step:
    id: str
    title: str
    state: str
    detail: str
    action: str | None = None       # a button the wizard itself carries out
    link: str | None = None         # a page for the owner to open
    qr: str | None = None           # something for the phone's camera


class SetupConflict(RuntimeError):
    """The wizard will not change this; the message says why and what to do."""


@dataclass
class CliResult:
    code: int
    out: str


class TailscaleCli:
    """The standard Tailscale command line, and only the commands the wizard may use."""

    CANDIDATES = (r"C:\Program Files\Tailscale\tailscale.exe", r"C:\Program Files (x86)\Tailscale\tailscale.exe")

    def __init__(self, exe: str | None = None, timeout: float = 15.0):
        self.exe = exe or next((c for c in self.CANDIDATES if Path(c).is_file()), None) or shutil.which("tailscale")
        self.timeout = timeout

    def _run(self, *args: str) -> CliResult:
        if not self.exe:
            return CliResult(127, "")
        try:
            done = subprocess.run([self.exe, *args], capture_output=True, text=True, timeout=self.timeout,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired):
            return CliResult(-1, "")
        return CliResult(done.returncode, done.stdout)

    def version(self) -> str | None:
        r = self._run("version")
        return r.out.splitlines()[0].strip() if r.code == 0 and r.out.strip() else None

    def status(self) -> dict | None:
        r = self._run("status", "--json")
        try:
            return json.loads(r.out) if r.code == 0 else None
        except ValueError:
            return None

    def serve_status(self) -> dict | None:
        r = self._run("serve", "status", "--json")
        if r.code != 0:
            return None
        try:
            value = json.loads(r.out) if r.out.strip() else {}
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def serve_port(self, port: int) -> bool:
        return self._run("serve", "--bg", str(port)).code == 0

    def serve_off(self) -> bool:
        return self._run("serve", "--https=443", "off").code == 0


def https_probe(name: str, vault_id: str, timeout: float = 6.0) -> bool:
    """Does https://<name>/ answer as *this* vault (its id, not only its name), certificate checked?"""
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(f"https://{name}/api/ping", timeout=timeout, context=ctx) as r:
            body = json.loads(r.read(4096))
        return body.get("name") == "Vault V2" and body.get("vault_id") == vault_id
    except Exception:  # noqa: BLE001 — any failure is simply «not answering»
        return False


@dataclass(frozen=True)
class Listener:
    """Everything Tailscale serves on this PC's HTTPS port 443, read as a whole."""
    readable: bool
    handlers: dict[str, str]        # path -> proxy target
    other_handlers: bool            # a handler kind the wizard does not know (files, text…)
    tcp: dict | None                # the TCP["443"] entry, as Tailscale shows it
    funnel: bool                    # public internet access switched on for this listener

    @property
    def empty(self) -> bool:
        return self.readable and not self.handlers and not self.other_handlers and not self.tcp and not self.funnel

    def fingerprint(self) -> str:
        body = json.dumps({"handlers": self.handlers, "other": self.other_handlers, "tcp": self.tcp,
                           "funnel": self.funnel}, sort_keys=True)
        return hashlib.sha256(body.encode()).hexdigest()


UNREADABLE = Listener(False, {}, False, None, False)
KNOWN_TOP = {"TCP", "Web", "AllowFunnel"}


def read_listener(serve: dict | None, name: str) -> Listener:
    """Read the whole listener, or report it unreadable. Anything of an unknown shape is
    unreadable — never mistaken for an empty listener the wizard could claim."""
    if not isinstance(serve, dict):
        return UNREADABLE
    if any(serve.get(k) for k in set(serve) - KNOWN_TOP):
        return UNREADABLE                      # a kind of serving the wizard does not know
    key = f"{name}:443"
    tcp_all, web_all, funnel_all = serve.get("TCP", {}), serve.get("Web", {}), serve.get("AllowFunnel", {})
    if not all(isinstance(x, dict) for x in (tcp_all, web_all, funnel_all)):
        return UNREADABLE
    tcp = tcp_all.get("443")
    if tcp is not None and not isinstance(tcp, dict):
        return UNREADABLE
    web = web_all.get(key)
    if web is not None and (not isinstance(web, dict) or set(web) - {"Handlers"}):
        return UNREADABLE
    handlers_raw = (web or {}).get("Handlers", {})
    if not isinstance(handlers_raw, dict):
        return UNREADABLE
    funnel = funnel_all.get(key, False)
    if not isinstance(funnel, bool):
        return UNREADABLE
    handlers: dict[str, str] = {}
    other = False
    for path, h in handlers_raw.items():
        if isinstance(h, dict) and set(h) == {"Proxy"} and isinstance(h["Proxy"], str):
            handlers[path] = h["Proxy"]
        else:
            other = True
    return Listener(True, handlers, other, tcp, funnel)


@dataclass
class Setup:
    root: Path
    log: ReceiptLog
    devices: Callable[[], DeviceRegistry]
    port: int
    front: Callable[[], str] = lambda: "direct"
    cli: TailscaleCli = field(default_factory=TailscaleCli)
    probe: Callable[[str, str], bool] = https_probe
    apk_present: Callable[[], bool] = lambda: False
    paired_here: set[str] = field(default_factory=set)
    _job_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    running: str | None = None                # "apply" | "undo" while a Tailscale change runs
    last_name: str = ""                        # the name the last change was made for

    def _begin(self, job: str) -> None:
        with self._job_lock:
            if self.running is not None:
                raise SetupConflict("a change is already running — wait for it to finish")
            self.running = job

    def _end(self) -> None:
        with self._job_lock:
            self.running = None

    @property
    def record(self) -> Path:
        return self.root / ".api" / "setup_serve.json"

    @property
    def target(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _name(self, status: dict | None) -> str:
        return str(((status or {}).get("Self") or {}).get("DNSName", "")).rstrip(".")

    def _ours(self) -> dict | None:
        try:
            value = json.loads(self.record.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def _is_ours_exactly(self, listener: Listener) -> bool:
        return (listener.readable and listener.handlers == {"/": self.target} and not listener.other_handlers
                and not listener.funnel and listener.tcp in (None, {"HTTPS": True}))

    # -- the facts -------------------------------------------------------------------

    def check(self) -> list[Step]:
        steps: list[Step] = []
        version = self.cli.version()
        if version is None:
            steps.append(Step("tailscale", "Tailscale installed", NEEDS_YOU,
                              "Vault reaches your phone through Tailscale, a free private network for your own "
                              "devices. Install it, then come back here.", link=TAILSCALE_DOWNLOAD))
            return self._fill(steps)
        steps.append(Step("tailscale", "Tailscale installed", DONE, f"Tailscale {version}"))

        status = self.cli.status()
        if status is None:
            steps.append(Step("signed_in", "Signed in to Tailscale", UNKNOWN,
                              "Tailscale did not answer. Open it from the tray and check it is running."))
            return self._fill(steps)
        name = self._name(status)
        if not (status.get("BackendState") == "Running" and (status.get("Self") or {}).get("Online") and name):
            steps.append(Step("signed_in", "Signed in to Tailscale", NEEDS_YOU,
                              "Open Tailscale from the tray and sign in. Use the same account on your phone."))
            return self._fill(steps)
        steps.append(Step("signed_in", "Signed in to Tailscale", DONE, f"This PC is «{name}» in your network"))

        certs = [str(c).rstrip(".") for c in (status.get("CertDomains") or [])]
        if name not in certs:
            steps.append(Step("https", "HTTPS for your devices", NEEDS_YOU,
                              "Turn on «HTTPS Certificates» in the Tailscale admin page (DNS tab). The name of "
                              f"this PC ({name}) then appears in public certificate logs; nothing of the vault "
                              "does.", link=TAILSCALE_ADMIN_DNS))
            return self._fill(steps)
        steps.append(Step("https", "HTTPS for your devices", DONE, "Certificates are on"))

        served = self._served_step(name)
        steps.append(served)
        steps.append(self._phone_step(name, served.state == DONE))
        return steps

    def _fill(self, steps: list[Step]) -> list[Step]:
        titles = {"signed_in": "Signed in to Tailscale", "https": "HTTPS for your devices",
                  "served": "Vault served to your devices", "phone": "Your phone paired"}
        have = {s.id for s in steps}
        return steps + [Step(i, titles[i], NOT_STARTED, "") for i in STEP_IDS if i not in have]

    def _served_step(self, name: str) -> Step:
        title = "Vault served to your devices"
        ours = self._ours()
        listener = read_listener(self.cli.serve_status(), name)
        if self.running is not None:
            return Step("served", title, UNKNOWN, "Tailscale is being asked right now; wait a moment.")
        if ours and ours.get("state") == "pending":
            same = ours.get("name") == name and ours.get("target") == self.target
            return Step("served", title, UNKNOWN,
                        "A change to Tailscale was started and its result was not confirmed. Check it; if it "
                        "stays like this, look at Tailscale's own Serve settings." if same else
                        "An unfinished change was recorded for another name or port. Vault will not claim "
                        "anything now; look at Tailscale's own Serve settings.",
                        action="Check the pending change" if same else None)
        if not listener.readable:
            return Step("served", title, UNKNOWN, "Tailscale did not say what it serves. Try again in a moment.")
        if listener.funnel:
            return Step("served", title, NEEDS_YOU,
                        f"Funnel is on for https://{name}/ — that opens it to the whole internet. Vault will not "
                        "run like that. Turn Funnel off in Tailscale, then check again.")
        if self._is_ours_exactly(listener):
            if self.front() != "proxied":
                return Step("served", title, NEEDS_YOU,
                            f"https://{name}/ already points at this vault; the vault itself still listens the "
                            "old way. Finish the switch.", action="Finish setting up HTTPS")
            vault_id = self.devices().vault_id()
            ok = self.probe(name, vault_id)
            return Step("served", title, DONE if ok else UNKNOWN,
                        f"https://{name}/ — answering" if ok else
                        f"https://{name}/ is set up but did not answer as this vault yet; keep this window open")
        if not listener.empty:
            shown = ", ".join(f"{p} → {t}" for p, t in listener.handlers.items()) or "another kind of service"
            return Step("served", title, NEEDS_YOU,
                        f"Something else is already served at https://{name}/ ({shown}). Vault will not replace "
                        "it; remove it in Tailscale first if it is yours.")
        return Step("served", title, NEEDS_YOU,
                    f"Serve this vault at https://{name}/ inside your Tailscale network only (no Funnel). The "
                    "setting stays after a restart; you can undo it here.", action="Serve Vault over HTTPS")

    def _phone_step(self, name: str, served: bool) -> Step:
        title = "Your phone paired"
        registry = self.devices()
        fresh = [registry.get(i) for i in sorted(self.paired_here)]
        seen = [d for d in fresh if d is not None and d.active and d.id in registry.last_seen]
        if seen:
            return Step("phone", title, DONE, ", ".join(d.name for d in seen) + " opened the vault with its own key")
        if not served:
            return Step("phone", title, NOT_STARTED, "")
        if any(d is not None and d.active for d in fresh):
            return Step("phone", title, NEEDS_YOU, "Approved — waiting for the phone to open the vault with its key.",
                        action="Add phone…")
        if not self.apk_present():
            return Step("phone", title, NEEDS_YOU,
                        "This installation carries no phone app to hand out. Install Vault on the phone from "
                        "where you got this program, then press Add phone and scan the code.",
                        action="Add phone…", link=TAILSCALE_ANDROID)
        return Step("phone", title, NEEDS_YOU,
                    "On the phone: 1) install Tailscale (Open page) and sign in with the same account; 2) scan "
                    "this code to download Vault and install it; 3) press Add phone and scan the next code.",
                    action="Add phone…", link=TAILSCALE_ANDROID, qr=f"https://{name}/app/vault.apk")

    def note_paired(self, device_id: str) -> None:
        """A phone paired through this wizard; its first authorised request completes the step."""
        self.paired_here.add(device_id)

    # -- the one change ------------------------------------------------------------

    def apply(self, step_id: str) -> str:
        """Serve this vault over HTTPS on this PC's own name. Returns "applied", "already" or "pending"."""
        if step_id != "served":
            raise ValueError("the wizard changes nothing but the Vault route")
        self._begin("apply")
        try:
            return self._apply()
        finally:
            self._end()

    def _apply(self) -> str:
        name = self._name(self.cli.status())
        self.last_name = name
        if not name:
            raise SetupConflict("Tailscale is not signed in")
        ours = self._ours()
        listener = read_listener(self.cli.serve_status(), name)
        if ours and ours.get("state") == "pending":
            return self._settle(name, listener)
        if not listener.readable:
            raise SetupConflict("Tailscale did not say what it serves — nothing was changed")
        if listener.funnel:
            raise SetupConflict("Funnel is on for this name — turn it off in Tailscale first; nothing was changed")
        if self._is_ours_exactly(listener):
            return "already"
        if not listener.empty:
            raise SetupConflict("another service is on https://" + name + "/ — nothing was changed")
        # 1) write down the intention (the guard refuses a read-only or blocked vault here)
        with self.log.write("serve vault over https: intent"):
            self.log.effect()
            self._write_record({"state": "pending", "action": "serve", "name": name, "target": self.target,
                                "at": _now()})
            self.log.append("setup_serve_intent", self.target, f"https://{name}/")
        # 2) Tailscale's own command, outside the vault's guard
        self.cli.serve_port(self.port)
        # 3) record what Tailscale shows now
        return self._settle(name, read_listener(self.cli.serve_status(), name))

    def _settle(self, name: str, listener: Listener) -> str:
        ours = self._ours() or {}
        if ours.get("name") != name or ours.get("target") != self.target:
            return "pending"                   # not this intention: claim nothing
        action = ours.get("action")
        with self.log.write("serve vault over https: outcome"):
            if action == "serve" and self._is_ours_exactly(listener):
                self.log.effect()
                self._write_record({"state": "applied", "action": "serve", "name": name, "target": self.target,
                                    "fingerprint": listener.fingerprint(), "at": _now()})
                self.log.append("setup_serve_applied", self.target, f"https://{name}/")
                return "applied"
            refused = action == "serve" and listener.empty
            if refused:
                self.log.effect()
                self.record.unlink(missing_ok=True)
                self.log.append("setup_serve_not_applied", self.target, f"https://{name}/",
                                note="Tailscale did not take the route")
            if action == "undo" and listener.empty:
                self.log.effect()
                self.record.unlink(missing_ok=True)
                self.log.append("setup_serve_removed", ours.get("target", ""), f"https://{name}/")
                return "removed"
        if refused:
            # Said after the record is settled, so the refusal does not block the vault's writes.
            raise SetupConflict("Tailscale did not accept the route — nothing is served")
        return "pending"

    def undo(self, step_id: str) -> bool:
        """Remove the route only if the wizard added it and nothing about it changed since."""
        if step_id != "served":
            raise ValueError("the wizard undoes nothing but its own route")
        self._begin("undo")
        try:
            return self._undo()
        finally:
            self._end()

    def _undo(self) -> bool:
        ours = self._ours()
        if not ours or ours.get("state") != "applied":
            return False
        name = ours.get("name", "")
        if self._name(self.cli.status()) != name:
            # `serve off` acts on the current name; a renamed PC is not the route that was added.
            raise SetupConflict("this PC's Tailscale name changed since — undo it in Tailscale yourself")
        listener = read_listener(self.cli.serve_status(), name)
        if not listener.readable or listener.fingerprint() != ours.get("fingerprint"):
            raise SetupConflict("the Serve settings changed since the wizard set them — undo it in Tailscale yourself")
        with self.log.write("stop serving vault over https: intent"):
            self.log.effect()
            self._write_record({**ours, "state": "pending", "action": "undo", "at": _now()})
            self.log.append("setup_unserve_intent", ours.get("target", ""), f"https://{name}/")
        self.cli.serve_off()
        return self._settle(name, read_listener(self.cli.serve_status(), name)) == "removed"

    @property
    def can_undo(self) -> bool:
        ours = self._ours()
        return bool(ours and ours.get("state") == "applied")

    def _write_record(self, value: dict) -> None:
        self.record.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.record.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(value), encoding="utf-8")
        tmp.replace(self.record)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
