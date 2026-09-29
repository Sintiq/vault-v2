"""PDF public operations keep crashes, hangs and parsing out of their caller."""
import subprocess
import time
import json
from io import BytesIO
import os
from pathlib import Path
import threading

import pytest

from vault_v2.errors import VaultError
from vault_v2.pdf_preview import render_pdf_bytes_page


def synthetic_child(monkeypatch, script):
    """Replace only the external executable; keep real pipes and Job cleanup."""
    original = subprocess.Popen
    children, launches = [], []

    def launch(argv, **kwargs):
        child = original([*argv[:3], "-c", script], **kwargs)
        children.append(child)
        launches.append((argv, kwargs))
        return child

    monkeypatch.setattr(subprocess, "Popen", launch)
    return children, launches


def test_render_deadline_reaps_a_child_that_does_not_read_input(monkeypatch):
    children, _ = synthetic_child(monkeypatch, "import time; time.sleep(60)")
    started = time.monotonic()
    with pytest.raises(VaultError, match="deadline exceeded"):
        render_pdf_bytes_page(b"x" * (2 * 1024**2), timeout_s=0.3)
    assert time.monotonic() - started < 6
    assert children and all(child.poll() is not None for child in children)


def test_real_child_renders_form_value_without_saved_appearance(tmp_path):
    from io import BytesIO
    from PIL import Image, ImageChops
    from tools.synthetic_viewer_files import form_pdf

    filled = form_pdf(tmp_path / "filled.pdf", with_appearance=False, encrypted=True,
                      encryption_algorithm="AES-256").read_bytes()
    blank = form_pdf(tmp_path / "blank.pdf", "", with_appearance=False, encrypted=True,
                     encryption_algorithm="AES-256").read_bytes()
    result = render_pdf_bytes_page(filled, width=1200)
    empty = render_pdf_bytes_page(blank, width=1200)
    assert result.page_count == 1 and result.page_index == 0
    with Image.open(BytesIO(result.png)) as actual, Image.open(BytesIO(empty.png)) as baseline:
        difference = ImageChops.difference(actual.convert("RGB"), baseline.convert("RGB"))
        assert sum(pixel != (0, 0, 0) for pixel in difference.get_flattened_data()) > 100


def test_public_render_and_extraction_never_import_native_pdf_into_caller(tmp_path):
    import sys
    from pathlib import Path
    from tools.synthetic_ocr_files import text_pdf

    source = text_pdf(tmp_path / "synthetic.pdf")
    program = """
import sys
from pathlib import Path
from vault_v2.pdf_preview import render_pdf_bytes_page
from vault_v2.text_extract import extract_text
def assert_no_pdf_imports():
    assert not any(name.split('.')[0] in {'pypdf', 'pypdfium2', 'pypdfium2_raw'} for name in sys.modules)
assert_no_pdf_imports()
data = Path(sys.argv[1]).read_bytes()
assert render_pdf_bytes_page(data, width=400).page_count == 1
assert 'Payment is due' in extract_text(data, '.pdf').text
assert_no_pdf_imports()
"""
    result = subprocess.run([sys.executable, "-B", "-c", program, str(source)],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True,
                            text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_child_cannot_return_a_different_requested_image_width(monkeypatch):
    from PIL import Image

    output = BytesIO()
    with Image.new("RGB", (800, 100), "white") as picture:
        picture.save(output, format="PNG")
    frame = json.dumps({"page_count": 1, "page_index": 0, "warning": ""}).encode() + b"\n" + output.getvalue()
    synthetic_child(monkeypatch, "import sys; sys.stdout.buffer.write(" + repr(frame) + ")")
    with pytest.raises(VaultError, match="invalid child image size"):
        render_pdf_bytes_page(b"synthetic input", width=400)


def test_child_truncated_png_is_refused_before_it_reaches_the_caller(monkeypatch):
    from PIL import Image

    output = BytesIO()
    with Image.new("RGB", (400, 100), "white") as picture:
        picture.save(output, format="PNG")
    frame = json.dumps({"page_count": 1, "page_index": 0, "warning": ""}).encode() + b"\n" + output.getvalue()[:33]
    synthetic_child(monkeypatch, "import sys; sys.stdout.buffer.write(" + repr(frame) + ")")
    with pytest.raises(VaultError, match="invalid child image"):
        render_pdf_bytes_page(b"synthetic input", width=400)


@pytest.mark.parametrize("script", [
    "import os; os._exit(17)",
    "import sys; sys.stdout.buffer.write(b'not JSON\\nprivate-data')",
    "import sys; sys.stdout.buffer.write(b'{\"error\":\"private-data\"}\\n')",
    "import sys; sys.stdout.buffer.write(b'{\"error\":{}}\\n')",
    "import sys; sys.stdout.buffer.write(b'{\"page_count\":1,\"page_count\":2,\"warning\":\"\"}\\n')",
    "import sys; [sys.stdout.buffer.write(b'x' * 8192) for _ in range(9000)]",
], ids=["crash", "malformed", "unknown-error", "typed-error", "duplicate-json", "output-flood"])
def test_child_failures_are_safe_refusals_with_all_owned_children_reaped(monkeypatch, script):
    children, launches = synthetic_child(monkeypatch, script)
    with pytest.raises(VaultError) as error:
        render_pdf_bytes_page(b"synthetic input")
    assert "private-data" not in str(error.value)
    assert children and all(child.poll() is not None for child in children)
    assert all(not Path(kwargs["cwd"]).exists() for _, kwargs in launches)


@pytest.mark.parametrize("deadline", [0, -1, 11, float("inf"), float("nan"), True, "1", 10**1000],
                         ids=["zero", "negative", "over-ceiling", "inf", "nan", "bool", "string", "huge"])
def test_invalid_deadline_never_launches_child(monkeypatch, deadline):
    def no_launch(*args, **kwargs):
        raise AssertionError("Invalid PDF deadline must be refused before process launch")

    monkeypatch.setattr(subprocess, "Popen", no_launch)
    with pytest.raises(VaultError, match="deadline is invalid"):
        render_pdf_bytes_page(b"synthetic input", timeout_s=deadline)


def test_cancel_reaps_running_child_and_releases_admission(monkeypatch):
    from vault_v2.pdf_preview import PdfPreviewBusy

    children, _ = synthetic_child(monkeypatch, "import time; time.sleep(60)")
    cancel = threading.Event()
    timer = threading.Timer(0.2, cancel.set)
    timer.start()
    try:
        with pytest.raises(VaultError, match="cancelled"):
            render_pdf_bytes_page(b"synthetic input", cancel_event=cancel)
    finally:
        timer.cancel()
        timer.join()
    assert all(child.poll() is not None for child in children)
    with pytest.raises(VaultError, match="deadline exceeded") as error:
        render_pdf_bytes_page(b"synthetic next input", wait=False, timeout_s=0.2)
    assert not isinstance(error.value, PdfPreviewBusy)


def test_waiting_for_another_pdf_has_its_own_deadline_and_cancel(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from vault_v2.pdf_preview import PdfPreviewBusy

    children, _ = synthetic_child(monkeypatch, "import time; time.sleep(60)")
    first_cancel = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(render_pdf_bytes_page, b"synthetic input", cancel_event=first_cancel)
        try:
            end = time.monotonic() + 3
            while not children and time.monotonic() < end:
                time.sleep(0.01)
            assert children
            with pytest.raises(PdfPreviewBusy):
                render_pdf_bytes_page(b"second input", wait=False)
            with pytest.raises(VaultError, match="deadline exceeded"):
                render_pdf_bytes_page(b"second input", timeout_s=0.1)
            cancelled = threading.Event()
            cancelled.set()
            with pytest.raises(VaultError, match="cancelled"):
                render_pdf_bytes_page(b"second input", cancel_event=cancelled)
            assert len(children) == 1
        finally:
            first_cancel.set()
        with pytest.raises(VaultError, match="cancelled"):
            future.result(timeout=6)
    assert all(child.poll() is not None for child in children)


def test_child_receives_no_credentials_and_private_directory_disappears(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-secret")
    monkeypatch.setenv("HTTP_PROXY", "synthetic-secret")
    monkeypatch.setenv("PYTHONPATH", "synthetic-untrusted-path")
    children, launches = synthetic_child(monkeypatch,
        "import os,sys; forbidden={'ANTHROPIC_API_KEY','OPENAI_API_KEY','HTTP_PROXY','PYTHONPATH'}; "
        "sys.stdout.buffer.write(b'{\"error\":\"decode\"}\\n' if not forbidden.intersection(os.environ) else b'bad')")
    with pytest.raises(VaultError, match="cannot be decoded"):
        render_pdf_bytes_page(b"synthetic input")
    assert all(child.poll() is not None for child in children)
    assert all(argv[1:3] == ["-I", "-B"] and not kwargs["shell"] for argv, kwargs in launches)
    assert all(not Path(kwargs["cwd"]).exists() for _, kwargs in launches)


@pytest.mark.skipif(os.name != "nt", reason="Windows kill-on-close Job")
def test_deadline_reaps_worker_descendants(tmp_path, monkeypatch):
    import _winapi

    pid_file = tmp_path / "synthetic-descendant.pid"
    script = (
        "import subprocess, sys, time; from pathlib import Path; "
        "child=subprocess.Popen([sys.executable,'-I','-c','import time; time.sleep(60)']); "
        f"Path({str(pid_file)!r}).write_text(str(child.pid)); time.sleep(60)"
    )
    children, _ = synthetic_child(monkeypatch, script)
    with pytest.raises(VaultError, match="deadline exceeded"):
        render_pdf_bytes_page(b"synthetic input", timeout_s=1)
    assert all(child.poll() is not None for child in children)
    pid = int(pid_file.read_text())
    try:
        handle = _winapi.OpenProcess(0x00100000, False, pid)
    except OSError:
        pass
    else:
        try:
            assert _winapi.WaitForSingleObject(handle, 0) == _winapi.WAIT_OBJECT_0
        finally:
            _winapi.CloseHandle(handle)


def test_oversize_visible_source_is_refused_before_read_or_launch(tmp_path, monkeypatch):
    from vault_v2.ops import VaultOps
    from vault_v2.paths import VaultPaths
    from vault_v2.pdf_preview import read_pdf_info

    paths = VaultPaths(tmp_path / "synthetic-vault")
    VaultOps(paths)
    source = paths.documents / "synthetic.pdf"
    source.write_bytes(b"synthetic input")
    original_stat = Path.stat

    def oversized_stat(path, *args, **kwargs):
        actual = original_stat(path, *args, **kwargs)
        if path == source and kwargs.get("follow_symlinks", True):
            values = list(actual)
            values[6] = 512 * 1024 * 1024 + 1
            return os.stat_result(values)
        return actual

    def no_access(*args, **kwargs):
        raise AssertionError("Oversize source must fail before open/launch")

    monkeypatch.setattr(Path, "stat", oversized_stat)
    monkeypatch.setattr(Path, "open", no_access)
    monkeypatch.setattr(subprocess, "Popen", no_access)
    with pytest.raises(VaultError, match="512 MiB"):
        read_pdf_info(paths, source)


def test_native_text_child_cannot_misattribute_another_page(monkeypatch):
    from vault_v2.text_extract import extract_text

    metadata = {"page_count": 2, "page_index": 1, "warning": ""}
    body = {"native": "Synthetic text long enough to avoid OCR", "fields": "", "warnings": []}
    frame = json.dumps(metadata).encode() + b"\n" + json.dumps(body).encode()
    synthetic_child(monkeypatch, "import sys; sys.stdout.buffer.write(" + repr(frame) + ")")
    with pytest.raises(VaultError, match="invalid child result"):
        extract_text(b"synthetic input", ".pdf")


@pytest.mark.parametrize("operation", ["info", "text"])
def test_info_and_native_extraction_survive_child_crash_without_receipts(tmp_path, monkeypatch, operation):
    from vault_v2.ops import VaultOps
    from vault_v2.paths import VaultPaths
    from vault_v2.pdf_preview import read_pdf_info
    from vault_v2.text_extract import extract_text

    ops = VaultOps(VaultPaths(tmp_path / "synthetic-vault"))
    source = ops.paths.documents / "synthetic.pdf"
    source.write_bytes(b"synthetic unchanged bytes")
    before = ops.log.tail()
    children, _ = synthetic_child(monkeypatch, "import os; os._exit(17)")
    with pytest.raises(VaultError, match="local child failed"):
        if operation == "info":
            read_pdf_info(ops.paths, source)
        else:
            extract_text(source.read_bytes(), ".pdf")
    assert source.read_bytes() == b"synthetic unchanged bytes"
    assert ops.log.tail() == before
    assert children and all(child.poll() is not None for child in children)


def test_info_hung_parser_has_the_same_bounded_deadline(tmp_path, monkeypatch):
    from vault_v2.ops import VaultOps
    from vault_v2.paths import VaultPaths
    from vault_v2.pdf_preview import read_pdf_info

    paths = VaultPaths(tmp_path / "synthetic-vault")
    VaultOps(paths)
    source = paths.documents / "synthetic.pdf"
    source.write_bytes(b"synthetic input")
    children, _ = synthetic_child(monkeypatch, "import time; time.sleep(60)")
    with pytest.raises(VaultError, match="deadline exceeded"):
        read_pdf_info(paths, source, timeout_s=0.2)
    assert children and all(child.poll() is not None for child in children)
