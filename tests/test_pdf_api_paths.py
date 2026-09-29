"""PDF path and authentication contract over real HTTP; synthetic files only."""
from io import BytesIO
from pathlib import Path
import json
import os

import pytest

from test_pdf_api import get, server
from tools.synthetic_viewer_files import form_pdf
from vault_v2.api import ApiServer, VaultAPI
from vault_v2.cards import CardStore
from vault_v2.health import HealthStore
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.tasks import TaskStore


@pytest.mark.parametrize("route", ["/api/pdf/info", "/api/pdf/page"])
@pytest.mark.parametrize("key", [None, "WRONG-SYNTHETIC-KEY"])
def test_authentication_refuses_before_source_open(server, monkeypatch, route, key):
    _srv, ops = server
    source = form_pdf(ops.paths.documents / "private-synthetic.pdf")
    source_opens = []
    original = Path.open

    def observed_open(path, *args, **kwargs):
        if path == source:
            source_opens.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", observed_open)
    status, headers, body = get(server, route, key=key, pane="documents",
                                rel=source.name, page=0)
    assert status == 401 and json.loads(body) == {"error": "bad key"}
    assert headers["Cache-Control"] == "no-store"
    assert source_opens == []
    assert ops.log.tail() == []


@pytest.mark.parametrize("route", ["/api/pdf/info", "/api/pdf/page"])
@pytest.mark.parametrize("case", [
    "outside-pane", "hidden-file", "hidden-directory", "service-file",
    "service-directory", "dotdot", "dotdot-cross-pane", "absolute-inside",
    "absolute-outside", "drive-relative", "alternate-stream", "directory",
    "non-pdf", "missing", "empty",
])
def test_http_refuses_files_outside_visible_pdf_selection(server, route, case):
    _srv, ops = server
    paths = ops.paths
    source = form_pdf(paths.documents / "form.pdf")
    pane, rel = "documents", source.name
    candidate = None
    if case == "outside-pane":
        pane, rel = ".trash", "hidden.pdf"
        candidate = paths.trash / rel
    elif case == "hidden-file":
        rel = ".hidden.pdf"
        candidate = paths.documents / rel
    elif case == "hidden-directory":
        rel = ".hidden/form.pdf"
        candidate = paths.documents / rel
    elif case == "service-file":
        rel = "form.trash.json"
        candidate = paths.documents / rel
    elif case == "service-directory":
        rel = "deleted.trash.json/form.pdf"
        candidate = paths.documents / rel
    elif case == "dotdot":
        rel = "../documents/form.pdf"
    elif case == "dotdot-cross-pane":
        rel = "../personal/form.pdf"
        candidate = paths.personal / "form.pdf"
    elif case == "absolute-inside":
        rel = str(source)
    elif case == "absolute-outside":
        candidate = paths.root.parent / "outside.pdf"
        rel = str(candidate)
    elif case == "drive-relative":
        rel = (source.drive or "C:") + "form.pdf"
    elif case == "alternate-stream":
        carrier = paths.documents / "carrier.bin"
        carrier.write_bytes(b"synthetic carrier")
        rel = carrier.name + ":embedded.pdf"
        candidate = paths.documents / rel
    elif case == "directory":
        rel = "directory.pdf"
        (paths.documents / rel).mkdir()
    elif case == "non-pdf":
        rel = "form.txt"
        candidate = paths.documents / rel
    elif case == "missing":
        rel = "missing.pdf"
    elif case == "empty":
        rel = ""
    if candidate is not None:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        form_pdf(candidate)
    status, headers, body = get(server, route, pane=pane, rel=rel, page=0)
    assert status == 400
    assert headers["Content-Type"].startswith("application/json")
    assert isinstance(json.loads(body)["error"], str)
    assert ops.log.tail() == []


@pytest.mark.skipif(os.name != "nt", reason="Windows junction boundary")
@pytest.mark.parametrize("route", ["/api/pdf/info", "/api/pdf/page"])
@pytest.mark.parametrize("location", ["inside-pane", "vault-root", "ancestor-of-root"])
def test_http_refuses_real_junctions_at_every_path_depth(tmp_path, route, location):
    import _winapi

    paths = VaultPaths(tmp_path / "real-parent" / "synthetic-vault").ensure()
    if location == "inside-pane":
        target = tmp_path / "junction-target"
        target.mkdir()
        source = form_pdf(target / "form.pdf")
        link = paths.documents / "linked"
        routed, rel = paths, "linked/form.pdf"
    else:
        source = form_pdf(paths.documents / "form.pdf")
        rel = source.name
        if location == "vault-root":
            target = paths.root
            link = tmp_path / "linked-vault"
            routed = VaultPaths(link)
        else:
            target = paths.root.parent
            link = tmp_path / "linked-parent"
            routed = VaultPaths(link / paths.root.name)
    before = source.read_bytes()
    _winapi.CreateJunction(str(target), str(link))
    try:
        assert link.is_junction()
        ops = VaultOps(routed, read_only=True)
        api = VaultAPI(ops, CardStore(routed.root / ".cards", ops.log),
                       TaskStore(routed.root / ".tasks", ops.log),
                       HealthStore(routed.root / ".health", ops.log))
        srv = ApiServer(api, "SYNTHETIC-PDF-KEY", tmp_path / "unused.html",
                        lambda text: "unused", lambda: {}, "127.0.0.1", 0)
        srv.start()
        try:
            status, _, body = get((srv, ops), route, pane="documents", rel=rel, page=0)
            assert status == 400
            assert "Linked" in json.loads(body)["error"]
            assert source.read_bytes() == before and ops.log.tail() == []
        finally:
            srv.stop()
    finally:
        link.rmdir()  # Remove this synthetic junction only, never its target.


@pytest.mark.parametrize("route", ["/api/pdf/info", "/api/pdf/page"])
def test_http_refuses_a_real_file_symlink(server, route):
    _srv, ops = server
    target = form_pdf(ops.paths.personal / "target.pdf")
    link = ops.paths.documents / "linked.pdf"
    try:
        link.symlink_to(target)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows file-symlink privilege is unavailable")
        raise
    assert link.is_symlink()
    status, _, body = get(server, route, pane="documents", rel=link.name, page=0)
    assert status == 400 and "Linked" in json.loads(body)["error"]
    assert ops.log.tail() == []


@pytest.mark.parametrize("route", ["/api/pdf/info", "/api/pdf/page"])
@pytest.mark.parametrize("literal,alternate", [
    ("percent%20literal.pdf", "percent literal.pdf"),
    ("literal%2e%2e.pdf", "literal...pdf"),
    ("plus+literal.pdf", "plus literal.pdf"),
    ("Счёт + июль.pdf", "Счёт   июль.pdf"),
])
def test_urlencoded_filename_opens_itself_after_one_decode(server, route, literal, alternate):
    _srv, ops = server
    reference = form_pdf(ops.paths.documents / "reference.pdf")
    intended = form_pdf(ops.paths.documents / literal)
    different = form_pdf(ops.paths.documents / alternate, xfa_only=True)
    bodies = []
    for source in (reference, intended, different):
        status, _, body = get(server, route, pane="documents", rel=source.name,
                              page=0, width=400)
        assert status == 200
        bodies.append(body)
    assert bodies[1] == bodies[0]
    assert bodies[1] != bodies[2], "The literal name must not open the alternate PDF"
    assert ops.log.tail() == []


@pytest.mark.parametrize("width,expected_size", [(1, (1, 1)), (1600, (1600, 960))])
def test_page_accepts_both_inclusive_width_limits(server, width, expected_size):
    from PIL import Image

    _srv, ops = server
    source = form_pdf(ops.paths.documents / "width.pdf")
    status, headers, body = get(server, "/api/pdf/page", pane="documents",
                                rel=source.name, page=0, width=width)
    assert status == 200 and headers["Content-Type"] == "image/png"
    with Image.open(BytesIO(body)) as rendered:
        assert rendered.size == expected_size
    assert ops.log.tail() == []


@pytest.mark.parametrize("route,missing", [
    ("/api/pdf/info", "pane"), ("/api/pdf/info", "rel"),
    ("/api/pdf/page", "pane"), ("/api/pdf/page", "rel"),
    ("/api/pdf/page", "page"),
])
def test_missing_required_query_returns_json_refusal(server, route, missing):
    _srv, ops = server
    source = form_pdf(ops.paths.documents / "required.pdf")
    query = {"pane": "documents", "rel": source.name, "page": 0}
    del query[missing]
    status, headers, body = get(server, route, **query)
    assert status == 400
    assert headers["Content-Type"].startswith("application/json")
    assert isinstance(json.loads(body)["error"], str)
    assert ops.log.tail() == []
