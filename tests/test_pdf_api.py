"""PDF preview contract over real loopback HTTP; synthetic files only."""
from io import BytesIO
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

from tools.synthetic_viewer_files import form_pdf
from vault_v2.api import ApiServer, VaultAPI
from vault_v2.cards import CardStore
from vault_v2.health import HealthStore
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.tasks import TaskStore


@pytest.fixture
def server(tmp_path):
    ops = VaultOps(VaultPaths(tmp_path / "synthetic-vault"))
    api = VaultAPI(ops, CardStore(ops.paths.root / ".cards", ops.log),
                   TaskStore(ops.paths.root / ".tasks", ops.log),
                   HealthStore(ops.paths.root / ".health", ops.log))
    srv = ApiServer(api, "SYNTHETIC-PDF-KEY", tmp_path / "unused.html",
                    lambda text: "unused", lambda: {}, "127.0.0.1", 0)
    srv.start()
    try:
        yield srv, ops
    finally:
        srv.stop()


def get(server, route, *, key="SYNTHETIC-PDF-KEY", **query):
    srv, _ops = server
    request = Request(srv.url.rstrip("/") + route + "?" + urlencode(query))
    if key is not None:
        request.add_header("Authorization", "Bearer " + key)
    try:
        response = urlopen(request, timeout=5)
    except HTTPError as error:
        response = error
    with response:
        return response.status, response.headers, response.read()


def test_info_reports_real_page_count_without_writes(server):
    _srv, ops = server
    source = form_pdf(ops.paths.documents / "filled.pdf")
    before = source.read_bytes()
    status, headers, body = get(server, "/api/pdf/info", pane="documents", rel=source.name)
    assert status == 200
    assert json.loads(body) == {"pages": 1, "warning": ""}
    assert headers["Cache-Control"] == "no-store"
    assert source.read_bytes() == before and ops.log.tail() == []


@pytest.mark.parametrize("with_appearance", [True, False])
def test_phone_png_shows_filled_form_values_at_default_width(server, with_appearance):
    from PIL import Image, ImageChops

    _srv, ops = server
    options = {"with_appearance": with_appearance, "encrypted": True,
               "encryption_algorithm": "AES-256"}
    filled = form_pdf(ops.paths.documents / "filled.pdf", **options)
    blank = form_pdf(ops.paths.documents / "blank.pdf", "", **options)
    before = filled.read_bytes()
    status, headers, body = get(server, "/api/pdf/page", pane="documents", rel=filled.name, page=0)
    assert status == 200
    assert headers["Content-Type"] == "image/png" and headers["Cache-Control"] == "no-store"
    assert int(headers["Content-Length"]) == len(body)
    assert "Content-Disposition" not in headers
    blank_status, _, empty = get(server, "/api/pdf/page", pane="documents", rel=blank.name, page=0)
    assert blank_status == 200
    actual = Image.open(BytesIO(body)).convert("RGB")
    baseline = Image.open(BytesIO(empty)).convert("RGB")
    assert actual.size == (1080, 648)
    field = ImageChops.difference(actual, baseline).crop((108, 229, 972, 351))
    assert sum(pixel != (0, 0, 0) for pixel in field.get_flattened_data()) > 100
    assert filled.read_bytes() == before and ops.log.tail() == []


def test_phone_limit_is_first_fifty_pages_not_a_false_document_count(server):
    from pypdf import PdfWriter

    _srv, ops = server
    path = ops.paths.documents / "fifty-one.pdf"
    writer = PdfWriter()
    for _ in range(51):
        writer.add_blank_page(width=400, height=240)
    with path.open("wb") as output:
        writer.write(output)
    assert json.loads(get(server, "/api/pdf/info", pane="documents", rel=path.name)[2])["pages"] == 51
    assert get(server, "/api/pdf/page", pane="documents", rel=path.name, page=49, width=100)[0] == 200
    status, _, body = get(server, "/api/pdf/page", pane="documents", rel=path.name, page=50)
    assert status == 400
    assert json.loads(body) == {"error": "too many pages for the phone"}
    assert ops.log.tail() == []


@pytest.mark.parametrize("name,value", [("page", -1), ("page", 1), ("page", "x"),
                                       ("page", "0.5"), ("page", ""), ("width", 0),
                                       ("width", 1601), ("width", ""), ("width", "x"),
                                       ("width", "1.5")])
def test_bad_page_or_width_is_a_json_refusal(server, name, value):
    _srv, ops = server
    path = form_pdf(ops.paths.documents / "form.pdf")
    query = {"pane": "documents", "rel": path.name, "page": 0, "width": 1080, name: value}
    status, headers, body = get(server, "/api/pdf/page", **query)
    assert status == 400
    assert headers["Content-Type"].startswith("application/json")
    assert isinstance(json.loads(body)["error"], str)
    assert ops.log.tail() == []


@pytest.mark.parametrize("holder", ["desktop", "/api/pdf/info", "/api/pdf/page"])
@pytest.mark.parametrize("route", ["/api/pdf/info", "/api/pdf/page"])
def test_http_refuses_busy_renderer_without_reading_or_queueing(server, monkeypatch, route, holder):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from vault_v2.pdf_preview import render_pdf_page

    _srv, ops = server
    active = form_pdf(ops.paths.documents / "desktop.pdf")
    incoming = form_pdf(ops.paths.documents / "phone.pdf")
    entered, release = Event(), Event()
    original, phone_reads = Path.open, []

    def blocked(path, *args, **kwargs):
        if path == active:
            entered.set()
            assert release.wait(10), "synthetic read was not released"
        if path == incoming:
            phone_reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        running = (pool.submit(render_pdf_page, ops.paths, active) if holder == "desktop" else
                   pool.submit(get, server, holder, pane="documents", rel=active.name, page=0))
        try:
            assert entered.wait(3)
            pending = pool.submit(get, server, route, pane="documents", rel=incoming.name, page=0)
            status, _, body = pending.result(timeout=2)
            assert status == 503 and json.loads(body) == {"error": "busy"}
            assert phone_reads == [] and not running.done()
        finally:
            release.set()
        completed = running.result(timeout=3)
        assert completed.png.startswith(b"\x89PNG") if holder == "desktop" else completed[0] == 200
    assert get(server, route, pane="documents", rel=incoming.name, page=0)[0] == 200
    assert ops.log.tail() == []


@pytest.mark.parametrize("kind,expected", [
    ("xfa", "this is an XFA form; its filled data may not show here"),
    ("bad-form", "PDF form structure could not be checked; filled data may not show."),
])
def test_info_warning_matches_desktop_without_modifying_source(server, kind, expected):
    from pypdf import PdfWriter
    from pypdf.generic import NameObject, NumberObject
    from vault_v2.pdf_preview import render_pdf_page

    _srv, ops = server
    source = form_pdf(ops.paths.documents / "warning.pdf", xfa_only=(kind == "xfa"))
    if kind == "bad-form":
        writer = PdfWriter(clone_from=source)
        writer.root_object[NameObject("/AcroForm")] = NumberObject(7)
        with source.open("wb") as output:
            writer.write(output)
    before = source.read_bytes()
    status, _, body = get(server, "/api/pdf/info", pane="documents", rel=source.name)
    assert status == 200 and json.loads(body) == {"pages": 1, "warning": expected}
    assert render_pdf_page(ops.paths, source).warning == expected
    assert source.read_bytes() == before and ops.log.tail() == []


def test_info_does_not_rasterize_and_failed_render_releases_busy_state(server):
    from pypdf import PdfWriter

    _srv, ops = server
    huge = ops.paths.documents / "tall.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=1, height=1000000)
    with huge.open("wb") as output:
        writer.write(output)
    good = form_pdf(ops.paths.documents / "good.pdf")
    corrupt = ops.paths.documents / "corrupt.pdf"
    corrupt.write_bytes(b"Synthetic broken PDF")
    assert get(server, "/api/pdf/info", pane="documents", rel=huge.name)[0] == 200
    for bad in (huge, corrupt):
        assert get(server, "/api/pdf/page", pane="documents", rel=bad.name, page=0)[0] == 400
        assert get(server, "/api/pdf/page", pane="documents", rel=good.name, page=0)[0] == 200
    assert ops.log.tail() == []


def test_pdf_reads_work_in_readonly_mode_while_root_writer_is_busy(server):
    from concurrent.futures import ThreadPoolExecutor

    _srv, ops = server
    path = form_pdf(ops.paths.documents / "read-only.pdf")
    with ThreadPoolExecutor(max_workers=1) as pool:
        with ops.log.write("synthetic unrelated writer"):
            ops.log.read_only = True
            try:
                for route in ("/api/pdf/info", "/api/pdf/page"):
                    status, _, _ = pool.submit(get, server, route, pane="documents",
                                              rel=path.name, page=0).result(timeout=3)
                    assert status == 200
            finally:
                ops.log.read_only = False
    assert ops.log.tail() == []


@pytest.mark.parametrize("route", ["/api/pdf/info", "/api/pdf/page"])
@pytest.mark.parametrize("failure", ["crash", "invalid-output"])
def test_pdf_child_failure_is_a_safe_http_refusal_then_recovery(server, monkeypatch, route, failure):
    import subprocess

    _srv, ops = server
    path = form_pdf(ops.paths.documents / "synthetic.pdf")
    before = path.read_bytes()
    original, children = subprocess.Popen, []
    script = ('import os; os._exit(17)' if failure == "crash" else
              'print("synthetic private child diagnostics, not a result")')

    def launch(argv, **kwargs):
        child = original([*argv[:3], "-c", script], **kwargs)
        children.append(child)
        return child

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "Popen", launch)
        status, headers, body = get(server, route, pane="documents", rel=path.name, page=0)
    assert status == 400
    assert headers["Content-Type"].startswith("application/json")
    assert "PDF" in json.loads(body)["error"]
    assert b"synthetic private" not in body
    assert children and all(child.poll() is not None for child in children)
    assert get(server, "/api/ping", key=None)[0] == 200
    assert get(server, route, pane="documents", rel=path.name, page=0)[0] == 200
    assert path.read_bytes() == before and ops.log.tail() == []


@pytest.mark.parametrize("route", ["/api/pdf/info", "/api/pdf/page"])
def test_busy_child_refuses_second_pdf_but_does_not_block_ping(server, monkeypatch, route):
    from concurrent.futures import ThreadPoolExecutor
    import subprocess
    from threading import Event

    _srv, ops = server
    path = form_pdf(ops.paths.documents / "synthetic.pdf")
    entered, children = Event(), []
    original = subprocess.Popen

    def launch(argv, **kwargs):
        child = original([*argv[:3], "-c",
                          'import sys,time,os; sys.stdin.buffer.read(); time.sleep(1); os._exit(17)'],
                         **kwargs)
        children.append(child)
        entered.set()
        return child

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "Popen", launch)
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(get, server, route, pane="documents", rel=path.name, page=0)
            assert entered.wait(3)
            assert get(server, "/api/ping", key=None)[0] == 200
            status, _, body = get(server, route, pane="documents", rel=path.name, page=0)
            assert status == 503 and json.loads(body) == {"error": "busy"}
            assert not running.done()
            assert running.result(timeout=4)[0] == 400
    assert len(children) == 1 and children[0].poll() is not None
    assert get(server, route, pane="documents", rel=path.name, page=0)[0] == 200
    assert ops.log.tail() == []
