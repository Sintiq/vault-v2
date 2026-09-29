"""The Devices dialog: pairing shows the device's code, Confirm pairs, Revoke shuts out."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from vault_v2.api import load_or_create_key
from vault_v2.devices import LEGACY_ID, DeviceRegistry
from vault_v2.devices_dialog import DevicesDialog, PairDialog, pairing_link
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths

URL = "https://vault-pc.example.ts.net/"


@pytest.fixture
def app():
    application = QApplication.instance() or QApplication([])
    yield application
    application.processEvents()


@pytest.fixture
def reg(tmp_path: Path):
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    load_or_create_key(ops.paths.root, ops.log)
    return DeviceRegistry(ops.paths.root, ops.log)


def test_the_qr_carries_a_ticket_and_the_address_never_the_key(reg) -> None:
    ticket = reg.new_ticket()
    app_link = pairing_link(URL, ticket, "app")
    parsed = urlparse(app_link)
    assert parsed.scheme == "vault-pair"
    qs = parse_qs(parsed.query)
    assert unquote(qs["u"][0]) == URL and qs["t"][0] == ticket
    assert pairing_link(URL, ticket, "browser") == URL + "#pair=" + ticket
    key = (reg.root / ".api" / "key").read_text(encoding="utf-8").strip()
    assert key not in app_link


def test_pairing_waits_shows_the_devices_code_and_pairs_on_confirm(app, reg) -> None:
    dlg = PairDialog(reg, URL, "app")
    dlg._poll()
    assert not dlg.confirm_btn.isEnabled(), "nothing to confirm before a device claims"
    claim = reg.claim(dlg.ticket, "Galaxy S20", "app")
    dlg._poll()
    assert dlg.code.text().replace(" ", "") == claim["code"]
    assert "Galaxy S20" in dlg.state.text() and dlg.confirm_btn.isEnabled()
    dlg.confirm_btn.click()
    assert dlg.paired is not None and dlg.paired.name == "Galaxy S20"
    assert "Approved" in dlg.state.text() and not dlg.used, "approved is not yet paired"
    token = reg.status(claim["claim_id"])["token"]
    dlg._watch_first_use()
    assert not dlg.used
    reg.authenticate(token)
    dlg._watch_first_use()
    assert dlg.used and "has opened the vault" in dlg.state.text()


def test_closing_without_confirm_leaves_no_usable_ticket(app, reg) -> None:
    dlg = PairDialog(reg, URL, "browser")
    ticket = dlg.ticket
    dlg.reject()
    assert not reg.ticket_alive(ticket)


def test_reject_gives_the_device_a_refusal(app, reg) -> None:
    dlg = PairDialog(reg, URL, "app")
    claim = reg.claim(dlg.ticket, "Stranger", "app")
    dlg._poll()
    dlg.reject_btn.click()
    assert reg.status(claim["claim_id"]) == {"state": "rejected"}
    assert dlg.paired is None


def test_the_list_warns_about_the_shared_key_and_revoke_shuts_a_device_out(app, reg, monkeypatch) -> None:
    ticket = reg.new_ticket()
    claim = reg.claim(ticket, "Old phone", "app")
    device = reg.confirm(claim["claim_id"])
    token = reg.status(claim["claim_id"])["token"]
    dlg = DevicesDialog(URL, reg, True)
    dlg.show()
    app.processEvents()
    names = [dlg.table.item(r, 0).text() for r in range(dlg.table.rowCount())]
    assert names[0].startswith("Key shared") and "Old phone" in names
    assert dlg.legacy_note.isVisible()
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    dlg.table.selectRow(names.index("Old phone"))
    dlg.revoke_btn.click()
    assert reg.authenticate(token) is None
    assert dlg.table.item(names.index("Old phone"), 4).text().startswith("Revoked")
    dlg.table.selectRow(0)
    dlg.revoke_btn.click()
    assert reg.legacy() is None and not dlg.legacy_note.isVisible()
    assert all(d.id != LEGACY_ID for d in dlg.devices)
    dlg.close()
