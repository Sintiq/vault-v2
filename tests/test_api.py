"""The phone API: the same vault, the same receipts, one lock more."""

from __future__ import annotations

import io
import json
import subprocess
import urllib.error
import urllib.request
import urllib.response
from email.message import Message
from pathlib import Path

import pytest

from vault_v2.api import ApiServer, VaultAPI, bind_host, front_door, load_or_create_key
from vault_v2.cards import Card, CardStore
from vault_v2.health import HealthStore
from vault_v2.ops import VaultError, VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.receipts import sha256_file
from vault_v2.tasks import Task, TaskStore


@pytest.fixture()
def env(tmp_path: Path):
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    cards = CardStore(ops.paths.root / ".cards", ops.log)
    tasks = TaskStore(ops.paths.root / ".tasks", ops.log)
    health = HealthStore(ops.paths.root / ".health", ops.log)
    api = VaultAPI(ops, cards, tasks, health)
    return ops, cards, tasks, api


def _mk(p: Path, text: str = "hello") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# -- the API itself ----------------------------------------------------------


def test_listing_carries_the_card_and_its_confirmed_flag(env) -> None:
    ops, cards, _tasks, api = env
    f = _mk(ops.paths.documents / "policy.txt", "Insurance policy 2026.")
    cards.propose(Card.build(sha256_file(f), f.name, "TEXT", shelf="INSURANCE",
                             topics=["insurance"], year=2026, origin="AGENT"), f)
    item = api.list_pane("documents")["items"][0]
    assert item["shelf"] == "INSURANCE" and item["confirmed"] is False
    cards.confirm(Card.build(sha256_file(f), f.name, "TEXT", shelf="INSURANCE",
                             topics=["insurance"], year=2026, origin="HUMAN"), f)
    assert api.list_pane("documents")["items"][0]["confirmed"] is True


def test_a_path_cannot_climb_out_of_its_pane(env) -> None:
    _ops, _cards, _tasks, api = env
    for bad in ("../personal", "../../..", "sub/../../personal"):
        with pytest.raises(VaultError):
            api.list_pane("documents", bad)
    with pytest.raises(VaultError):
        api.list_pane("nowhere")


def test_only_text_can_be_read(env) -> None:
    ops, _cards, _tasks, api = env
    _mk(ops.paths.staging / "note.txt", "plain words")
    (ops.paths.staging / "scan.pdf").write_bytes(b"%PDF-1.4 binary")
    assert api.read_file("staging", "note.txt")["text"] == "plain words"
    with pytest.raises(VaultError):
        api.read_file("staging", "scan.pdf")


def test_a_viewer_gets_scans_and_photographs_but_not_anything(env) -> None:
    """Most real documents are a scan; a vault that only serves .txt is blind."""
    ops, _cards, _tasks, api = env
    pdf = ops.paths.documents / "Policy.pdf"
    pdf.write_bytes(b"%PDF-1.4 pretend")
    photo = ops.paths.documents / "Receipt.JPG"
    photo.write_bytes(b"\xff\xd8\xff pretend")
    _mk(ops.paths.documents / "notes.txt", "plain")
    (ops.paths.documents / "sheet.xlsx").write_bytes(b"PK pretend")

    by_name = {i["name"]: i for i in api.list_pane("documents")["items"]}
    assert by_name["Policy.pdf"]["view"] == "PDF"
    assert by_name["Receipt.JPG"]["view"] == "IMAGE"
    assert by_name["notes.txt"]["view"] == "TEXT"
    assert by_name["sheet.xlsx"]["view"] == "NONE"

    data, ctype, name = api.blob("documents", "Policy.pdf")
    assert data.startswith(b"%PDF") and ctype == "application/pdf" and name == "Policy.pdf"
    assert api.blob("documents", "Receipt.JPG")[1] == "image/jpeg"
    with pytest.raises(VaultError):
        api.blob("documents", "sheet.xlsx")
    with pytest.raises(VaultError):
        api.blob("documents", "../../etc/passwd")


def test_the_demo_scan_and_picture_are_real_files(tmp_path: Path) -> None:
    """If the fixture were malformed, a viewer failing on it would mean nothing."""
    from pypdf import PdfReader
    from PIL import Image

    from tools.seed_demo_documents import tiny_pdf, tiny_png

    reader = PdfReader(io.BytesIO(tiny_pdf("Blue Shield PPO-4471 2026")))
    assert len(reader.pages) == 1
    assert "PPO-4471" in reader.pages[0].extract_text()

    picture = Image.open(io.BytesIO(tiny_png()))
    assert picture.format == "PNG" and picture.size == (320, 200)
    assert picture.getpixel((10, 10)) != picture.getpixel((10, 60)), "the stripes must differ"


def test_a_file_too_big_for_a_phone_is_refused_not_streamed(env, monkeypatch) -> None:
    ops, _cards, _tasks, api = env
    from vault_v2 import api as api_module

    monkeypatch.setattr(api_module, "BLOB_MAX_BYTES", 16)
    (ops.paths.documents / "huge.pdf").write_bytes(b"%PDF" + b"x" * 100)
    with pytest.raises(VaultError, match="too large"):
        api.blob("documents", "huge.pdf")


def test_upload_lands_in_staging_and_leaves_a_receipt(env) -> None:
    ops, _cards, _tasks, api = env
    res = api.upload("bill.txt", b"Amount due 42")
    assert (ops.paths.staging / "bill.txt").read_bytes() == b"Amount due 42"
    assert res["sha256"] == sha256_file(ops.paths.staging / "bill.txt")
    assert ops.log.tail(1)[0]["op"] == "import"
    assert not list((ops.paths.root / ".api" / "incoming").iterdir()), "no copy left behind"


def test_transfer_and_trash_go_through_ops(env) -> None:
    ops, _cards, _tasks, api = env
    _mk(ops.paths.staging / "a.txt")
    api.transfer("staging", "a.txt", "documents", move=True)
    assert (ops.paths.documents / "a.txt").exists() and not (ops.paths.staging / "a.txt").exists()
    api.transfer("documents", "a.txt", "personal", move=False)
    assert (ops.paths.personal / "a.txt").exists() and (ops.paths.documents / "a.txt").exists()
    api.trash("personal", "a.txt")
    assert not (ops.paths.personal / "a.txt").exists()
    assert [r["op"] for r in ops.log.tail(3)] == ["move", "copy", "trash"]
    assert ops.log.verify() > 0


def test_the_vault_never_binds_to_every_interface(monkeypatch) -> None:
    """Direct or proxied, the local wifi must never be able to reach the vault."""
    assert front_door({}) == "direct"
    assert front_door({"front": "PROXIED"}) == "proxied"
    assert front_door({"front": "nonsense"}) == "direct"
    assert bind_host({"front": "proxied"}) == "127.0.0.1"
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], 0, stdout="100.64.0.10\n"))
    direct = bind_host({})
    assert direct != "0.0.0.0"
    assert direct is None or direct.startswith("100.")


def test_the_phone_hands_over_its_door_key_without_anyone_reading_it(env) -> None:
    ops, _cards, _tasks, api = env
    ticket = api.devices.new_ticket()
    claim = api.devices.claim(ticket, "Phone", "app")
    phone = api.devices.confirm(claim["claim_id"])
    assert api.pair_door("http://100.64.0.2:8779/", " s3cret-door-key ", phone) == {
        "paired": True, "address": "http://100.64.0.2:8779"}
    written = json.loads((ops.paths.root / ".door" / "config.json").read_text(encoding="utf-8"))
    assert written == {"address": "http://100.64.0.2:8779", "key": "s3cret-door-key", "device_id": phone.id}
    with pytest.raises(VaultError):
        api.pair_door("http://100.64.0.2:8779", "k"), "no device, no door"

    receipt = ops.log.tail(1)[0]
    assert receipt["op"] == "door_paired"
    assert "s3cret-door-key" not in json.dumps(receipt), "the key must not land in the receipts"

    for bad_address in ("100.64.0.2:8779", "", "ftp://x"):
        with pytest.raises(VaultError):
            api.pair_door(bad_address, "k", phone)
    with pytest.raises(VaultError):
        api.pair_door("http://100.64.0.2:8779", "   ", phone)


def test_ticking_a_task_is_receipted(env) -> None:
    ops, _cards, tasks, api = env
    tasks.add(Task("t1", "abc", "bill.txt", "Pay the bill", "2026-10-01", "Pay by 2026-10-01", "AGENT"))
    api.set_task_done("t1", True)
    assert api.list_tasks()["tasks"][0]["done"] is True
    assert ops.log.tail(1)[0]["op"] == "task_done"


# -- over the wire -----------------------------------------------------------


@pytest.fixture()
def server(env, tmp_path: Path):
    ops, _cards, _tasks, api = env
    page = tmp_path / "index.html"
    page.write_text("<html>vault</html>", encoding="utf-8")
    srv = ApiServer(api, load_or_create_key(ops.paths.root), page,
                    lambda text: "answer: " + text,
                    lambda: {"backend": "fake", "ready": True, "history": []},
                    "127.0.0.1", 0)
    srv.start()
    yield srv, ops
    srv.stop()


def _get(srv, path: str, key: str | None):
    req = urllib.request.Request(srv.url.rstrip("/") + path)
    if key:
        req.add_header("Authorization", "Bearer " + key)
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def test_without_the_key_nothing_opens(server) -> None:
    srv, _ops = server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(srv, "/api/files?pane=staging", None)
    assert exc.value.code == 401
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(srv, "/api/files?pane=staging", "not-the-key")
    assert exc.value.code == 401
    assert _get(srv, "/api/files?pane=staging", srv.key)["pane"] == "staging"


def test_the_page_is_served_and_the_gate_is_not_on_the_phone(server) -> None:
    srv, _ops = server
    with urllib.request.urlopen(srv.url, timeout=5) as r:
        assert b"vault" in r.read()
    for path in ("/api/export", "/api/exports"):
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(srv, path, srv.key)
        assert exc.value.code == 404, "export stays at the desk"


def test_chat_shares_one_conversation(server) -> None:
    srv, _ops = server
    assert _get(srv, "/api/chat", srv.key) == {"backend": "fake", "ready": True, "history": []}
    req = urllib.request.Request(srv.url.rstrip("/") + "/api/chat",
                                 data=json.dumps({"text": "hi"}).encode(),
                                 headers={"Authorization": "Bearer " + srv.key,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        assert json.loads(r.read())["reply"] == "answer: hi"


def test_phone_chat_unavailable_is_a_sanitized_503(env, tmp_path: Path) -> None:
    from vault_v2.agent import LocalModelUnavailable

    ops, _cards, _tasks, api = env
    page = _mk(tmp_path / "index.html", "synthetic test page")

    def unavailable(_text):
        raise LocalModelUnavailable("PRIVATE path or upstream error must not leave the server")

    srv = ApiServer(api, load_or_create_key(ops.paths.root), page, unavailable,
                    lambda: {"ready": False, "history": []}, "127.0.0.1", 0)
    srv.start()
    try:
        req = urllib.request.Request(srv.url.rstrip("/") + "/api/chat",
                                     data=json.dumps({"text": "synthetic question"}).encode(),
                                     headers={"Authorization": "Bearer " + srv.key,
                                              "Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        assert exc.value.code == 503
        body = json.loads(exc.value.read())
        assert body == {"error": "Local-only: selected model unavailable or blocked; "
                        "cloud fallback is disabled. Check Ollama and local settings."}
    finally:
        srv.stop()


@pytest.mark.parametrize("offline, expected", [
    (True, "Ollama is not running on this PC"),
    (False, "model wanted:local is not installed — run: ollama pull wanted:local"),
])
def test_phone_chat_reports_the_local_readiness_reason(env, tmp_path: Path, monkeypatch,
                                                      offline: bool, expected: str) -> None:
    """Real ChatPane, model selection and HTTP API; only model HTTP is synthetic."""
    from PySide6.QtWidgets import QApplication
    from vault_v2.chat import ChatPane

    application = QApplication.instance() or QApplication([])
    ops, _cards, _tasks, api = env
    page = _mk(tmp_path / "index.html", "synthetic test page")
    real_http_open = urllib.request.HTTPHandler.http_open
    api_port = []
    model_requests = []

    def model_http(handler, request):
        if request.host == "127.0.0.1:11434":
            model_requests.append(request.full_url)
            assert request.full_url == "http://127.0.0.1:11434/api/tags"
            if offline:
                raise urllib.error.URLError(ConnectionRefusedError("synthetic unavailable server"))
            body = b'{"models":[{"name":"llama3.1:8b"}]}'
            response = urllib.response.addinfourl(io.BytesIO(body), Message(), request.full_url, 200)
            response.msg = "synthetic HTTP response"
            return response
        assert api_port and request.host == f"127.0.0.1:{api_port[0]}"
        return real_http_open(handler, request)

    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", model_http)
    pane = ChatPane(ops.paths.staging, {"ollama_model": "wanted:local"})
    srv = ApiServer(api, load_or_create_key(ops.paths.root), page,
                    pane.remote_send, pane.remote_state, "127.0.0.1", 0)
    srv.start()
    api_port.append(srv.port)
    try:
        request = urllib.request.Request(srv.url.rstrip("/") + "/api/chat",
                                         data=json.dumps({"text": "synthetic request"}).encode(),
                                         headers={"Authorization": "Bearer " + srv.key,
                                                  "Content-Type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with pytest.raises(urllib.error.HTTPError) as error:
            opener.open(request, timeout=5)
        assert error.value.code == 503
        assert json.loads(error.value.read()) == {"error": expected}
        assert pane.remote_state()["history"] == []
        assert model_requests == ["http://127.0.0.1:11434/api/tags"]
    finally:
        srv.stop()
        pane.close()
        pane.deleteLater()
        application.processEvents()
