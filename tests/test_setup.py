"""The setup wizard's facts and its one change, against a fake Tailscale."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vault_v2.devices import DeviceRegistry
from vault_v2.errors import VaultReadOnly
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.setup import DONE, NEEDS_YOU, NOT_STARTED, UNKNOWN, Setup, SetupConflict

NAME = "vault-pc.example.ts.net"
TARGET = "http://127.0.0.1:8777"


class FakeCli:
    def __init__(self, *, installed=True, running=True, certs=True, routes=None, accept=True,
                 funnel=False, readable=True, tcp=None, extra=None):
        self.installed, self.running, self.certs, self.accept = installed, running, certs, accept
        self.routes = dict(routes or {})
        self.funnel, self.readable, self.tcp, self.extra = funnel, readable, tcp, extra
        self.calls: list[str] = []

    def version(self):
        return "1.102.2" if self.installed else None

    def status(self):
        if not self.installed:
            return None
        return {"BackendState": "Running" if self.running else "NeedsLogin",
                "Self": {"DNSName": NAME + ".", "Online": self.running},
                "CertDomains": [NAME] if self.certs else []}

    def serve_status(self):
        if not self.readable:
            return None
        if not self.routes and not self.funnel and not self.tcp and not self.extra:
            return {}
        handlers = {p: {"Proxy": t} for p, t in self.routes.items()}
        if self.extra:
            handlers.update(self.extra)
        out = {"TCP": {"443": self.tcp or {"HTTPS": True}}, "Web": {f"{NAME}:443": {"Handlers": handlers}}}
        if self.funnel:
            out["AllowFunnel"] = {f"{NAME}:443": True}
        return out

    def serve_port(self, port):
        self.calls.append(f"serve --bg {port}")
        if self.accept:
            self.routes["/"] = f"http://127.0.0.1:{port}"
        return self.accept

    def serve_off(self):
        self.calls.append("serve --https=443 off")
        self.routes.clear()
        return True


@pytest.fixture()
def ops(tmp_path: Path) -> VaultOps:
    return VaultOps(VaultPaths(tmp_path / "vault"))


def wizard(ops, cli, *, probe=lambda name, vid: True, front="proxied", apk=True):
    reg = DeviceRegistry(ops.paths.root, ops.log)
    box = {"front": front}
    w = Setup(ops.paths.root, ops.log, lambda: reg, 8777, front=lambda: box["front"], cli=cli, probe=probe,
              apk_present=lambda: apk)
    w.box = box
    return w


def states(steps):
    return {s.id: s.state for s in steps}


def step(w, sid):
    return next(s for s in w.check() if s.id == sid)


@pytest.mark.parametrize("cli", [FakeCli(installed=False), FakeCli(running=False), FakeCli(certs=False), FakeCli()])
def test_every_state_lists_the_same_five_steps(ops, cli) -> None:
    assert [s.id for s in wizard(ops, cli).check()] == ["tailscale", "signed_in", "https", "served", "phone"]


def test_nothing_installed_points_to_the_download_and_waits(ops) -> None:
    steps = wizard(ops, FakeCli(installed=False)).check()
    assert states(steps) == {"tailscale": NEEDS_YOU, "signed_in": NOT_STARTED, "https": NOT_STARTED,
                             "served": NOT_STARTED, "phone": NOT_STARTED}
    assert steps[0].link.startswith("https://tailscale.com/")


def test_no_certificates_points_to_the_admin_page_and_says_what_becomes_public(ops) -> None:
    https = step(wizard(ops, FakeCli(certs=False)), "https")
    assert https.state == NEEDS_YOU and "admin" in https.link and "public certificate logs" in https.detail


def test_ready_to_serve_offers_the_one_button(ops) -> None:
    served = step(wizard(ops, FakeCli()), "served")
    assert served.state == NEEDS_YOU and served.action == "Serve Vault over HTTPS"


def test_serving_writes_the_intent_first_then_records_what_tailscale_shows(ops) -> None:
    cli = FakeCli()
    w = wizard(ops, cli, probe=lambda name, vid: False)
    assert w.apply("served") == "applied"
    assert cli.calls == ["serve --bg 8777"] and w.can_undo
    ops_seen = [r["op"] for r in ops.log.tail(5)]
    assert ops_seen.index("setup_serve_intent") < ops_seen.index("setup_serve_applied")
    assert step(w, "served").state == UNKNOWN, "set up but not answering as this vault is not green"
    w.probe = lambda name, vid: vid == w.devices().vault_id()
    assert step(w, "served").state == DONE


def test_a_read_only_vault_refuses_before_tailscale_is_touched(ops) -> None:
    cli = FakeCli()
    ro = VaultOps(VaultPaths(ops.paths.root), read_only=True)
    w = wizard(ro, cli)
    with pytest.raises(VaultReadOnly):
        w.apply("served")
    assert cli.calls == [], "no Tailscale change without a place to record it"


def test_an_unreadable_listener_or_funnel_is_never_changed_or_done(ops) -> None:
    for cli in (FakeCli(readable=False), FakeCli(routes={"/": TARGET}, funnel=True)):
        w = wizard(ops, cli)
        assert step(w, "served").state in (UNKNOWN, NEEDS_YOU) and step(w, "served").action is None
        with pytest.raises(SetupConflict):
            w.apply("served")
        assert cli.calls == []


def test_someone_elses_listener_is_never_replaced(ops) -> None:
    for cli in (FakeCli(routes={"/": "http://127.0.0.1:3000"}),
                FakeCli(routes={"/": TARGET, "/other": "http://127.0.0.1:9"}),
                FakeCli(extra={"/files": {"Path": "C:/share"}}),
                FakeCli(tcp={"TCPForward": "127.0.0.1:22"})):
        w = wizard(ops, cli)
        s = step(w, "served")
        assert s.state == NEEDS_YOU and s.action is None, s.detail
        with pytest.raises(SetupConflict):
            w.apply("served")
        assert cli.calls == []


def test_an_existing_route_is_used_and_a_direct_vault_is_offered_the_finish(ops) -> None:
    cli = FakeCli(routes={"/": TARGET})
    w = wizard(ops, cli, front="direct")
    s = step(w, "served")
    assert s.state == NEEDS_YOU and s.action == "Finish setting up HTTPS"
    assert w.apply("served") == "already" and cli.calls == [] and not w.can_undo
    w.box["front"] = "proxied"
    assert step(w, "served").state == DONE


def test_undo_needs_the_same_listener_it_left(ops) -> None:
    cli = FakeCli()
    w = wizard(ops, cli)
    w.apply("served")
    cli.funnel = True
    with pytest.raises(SetupConflict):
        w.undo("served")
    cli.funnel = False
    cli.routes["/extra"] = "http://127.0.0.1:9000"
    with pytest.raises(SetupConflict):
        w.undo("served")
    del cli.routes["/extra"]
    assert w.undo("served") is True and not w.can_undo and cli.routes == {}
    assert "setup_serve_removed" in [r["op"] for r in ops.log.tail(5)]


def test_a_refused_route_is_recorded_as_not_applied(ops) -> None:
    cli = FakeCli(accept=False)
    w = wizard(ops, cli)
    with pytest.raises(SetupConflict):
        w.apply("served")
    assert not w.can_undo and "setup_serve_not_applied" in [r["op"] for r in ops.log.tail(5)]


def test_a_crash_after_the_intent_shows_pending_and_settles_later(ops) -> None:
    cli = FakeCli()
    w = wizard(ops, cli)
    record = ops.paths.root / ".api" / "setup_serve.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({"state": "pending", "action": "serve", "name": NAME, "target": TARGET}))
    cli.routes["/"] = TARGET                    # Tailscale did take it before the crash
    s = step(w, "served")
    assert s.state == UNKNOWN and s.action == "Check the pending change"
    assert w.apply("served") == "applied" and w.can_undo


def test_the_phone_step_counts_only_a_phone_paired_here_that_used_its_key(ops) -> None:
    cli = FakeCli(routes={"/": TARGET})
    w = wizard(ops, cli)
    reg = w.devices()
    old = reg.confirm(reg.claim(reg.new_ticket(), "Old phone", "app")["claim_id"])
    reg.last_seen[old.id] = 1.0
    phone = step(w, "phone")
    assert phone.state == NEEDS_YOU and phone.qr == f"https://{NAME}/app/vault.apk", "an old phone does not count"
    claim = reg.claim(reg.new_ticket(), "Galaxy", "app")
    new = reg.confirm(claim["claim_id"])
    w.note_paired(new.id)
    assert "waiting for the phone" in step(w, "phone").detail
    reg.authenticate(reg.status(claim["claim_id"])["token"])
    assert step(w, "phone").state == DONE


def test_without_a_phone_app_in_the_installation_the_wizard_says_so(ops) -> None:
    w = wizard(ops, FakeCli(routes={"/": TARGET}), apk=False)
    phone = step(w, "phone")
    assert phone.qr is None and "carries no phone app" in phone.detail


def test_the_wizard_changes_nothing_else(ops) -> None:
    w = wizard(ops, FakeCli())
    with pytest.raises(ValueError):
        w.apply("tailscale")
    with pytest.raises(ValueError):
        w.undo("phone")
