"""Actual local child recognition at the OCR process interface."""
from io import BytesIO
import json
import os
from pathlib import Path
import socket
import subprocess
import time

from PIL import Image, ImageDraw, ImageFont
import pytest

from vault_v2.errors import VaultError
from vault_v2.ocr_process import recognize_image


def synthetic_image():
    picture = Image.new("RGB", (1600, 400), "white")
    draw = ImageDraw.Draw(picture)
    font = ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf", 42)
    draw.text((45, 60), "Payment is due by October 15, 2026", font=font, fill="black")
    draw.text((45, 180), "Оплатить до 15 октября 2026 года", font=font, fill="black")
    stream = BytesIO()
    picture.save(stream, format="PNG")
    return stream.getvalue()


def test_local_child_reads_english_and_russian_without_python_network(monkeypatch):
    image = synthetic_image()

    def no_network(*args, **kwargs):
        raise AssertionError("OCR attempted a Python socket")

    monkeypatch.setattr(socket, "socket", no_network)
    text = recognize_image(image)
    assert "Payment is due by October 15, 2026" in text
    assert "Оплатить до 15 октября 2026 года" in text


def synthetic_child(monkeypatch, script):
    """Substitute only the external program; retain real pipes/job/deadline."""
    original = subprocess.Popen
    children, launches = [], []

    def launch(argv, **kwargs):
        launches.append((argv, kwargs))
        child = original([*argv[:3], "-c", script], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", launch)
    return children, launches


@pytest.mark.parametrize("version", ["tesseract 5.5.2", "tesseract 5.5.3"])
def test_pinned_engine_versions_reach_real_ocr(monkeypatch, version):
    from vault_v2 import ocr_process

    worker = Path(ocr_process.__file__).with_name("ocr_worker.py")
    # Substitute the external engine's version report, not the admission policy.
    # Recognition still uses the real child, models, pipes and deadline.
    children, _ = synthetic_child(monkeypatch,
        "import runpy, tesserocr; "
        f"tesserocr.tesseract_version=lambda: {version!r}; "
        f"runpy.run_path({str(worker)!r}, run_name='__main__')")
    text = recognize_image(synthetic_image())
    assert "Payment is due by October 15, 2026" in text
    assert "Оплатить до 15 октября 2026 года" in text
    assert all(child.poll() is not None for child in children)


@pytest.mark.parametrize("version", ["tesseract 5.5.1", "tesseract 5.5.30", "tesseract 5.5.3-custom"])
def test_unpinned_engine_versions_refuse_before_reading_image(monkeypatch, version):
    from vault_v2 import ocr_process

    worker = Path(ocr_process.__file__).with_name("ocr_worker.py")
    synthetic_child(monkeypatch,
        "import runpy, tesserocr; "
        f"tesserocr.tesseract_version=lambda: {version!r}; "
        f"runpy.run_path({str(worker)!r}, run_name='__main__')")
    with pytest.raises(VaultError, match="engine version is refused"):
        recognize_image(b"not an image; engine must be refused first")


def test_malformed_worker_error_is_a_safe_refusal_not_an_exception_type_leak(monkeypatch):
    children, _ = synthetic_child(monkeypatch, 'print(\'{"error":{}}\')')
    with pytest.raises(VaultError, match="local child failed"):
        recognize_image(b"synthetic input")
    assert all(child.poll() is not None for child in children)


@pytest.mark.parametrize("reply", ["not JSON", "[]", "null", '{"text":5}',
                                   '{"text":"ok","extra":"untrusted"}',
                                   '{"error":[]}', '{"error":"unknown private data"}'],
                         ids=["not-json", "list", "null", "non-text", "extra-field", "list-error", "unknown-error"])
def test_invalid_worker_output_is_refused_and_not_exposed(monkeypatch, reply):
    children, _ = synthetic_child(monkeypatch, "import sys; sys.stdout.write(" + repr(reply) + ")")
    with pytest.raises(VaultError) as failure:
        recognize_image(b"synthetic input")
    assert "private data" not in str(failure.value)
    assert all(child.poll() is not None for child in children)


def test_child_does_not_inherit_parent_credentials_and_temp_directory_is_removed(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-secret")
    monkeypatch.setenv("PYTHONPATH", "synthetic-untrusted-python-path")
    children, launches = synthetic_child(monkeypatch,
        'import json, os; print(json.dumps({"text":json.dumps(dict(os.environ)),"version":"tesseract 5.5.3"}))')
    child_env = json.loads(recognize_image(b"synthetic input"))
    assert all(name not in child_env for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "PYTHONPATH"))
    assert "synthetic-secret" not in str(child_env)
    assert all(child.poll() is not None for child in children)
    assert all(not Path(kwargs["cwd"]).exists() for _, kwargs in launches)
    assert all(argv[1:3] == ["-I", "-B"] and not kwargs["shell"] for argv, kwargs in launches)


@pytest.mark.parametrize("script,payload", [
    ("import time; time.sleep(60)", b"small input"),
    ("import time; time.sleep(60)", b"x" * (2 * 1024**2)),
], ids=["idle-child", "blocked-input"])
def test_deadline_reaps_child_even_when_it_does_not_read_input(monkeypatch, script, payload):
    children, launches = synthetic_child(monkeypatch, script)
    started = time.monotonic()
    with pytest.raises(VaultError, match="deadline exceeded"):
        recognize_image(payload, timeout_s=0.3)
    assert time.monotonic() - started < 6
    assert all(child.poll() is not None for child in children)
    assert all(not Path(kwargs["cwd"]).exists() for _, kwargs in launches)


@pytest.mark.parametrize("script", [
    'import os; os._exit(17)',
    'import sys; print(\'{"text":"not a success"}\'); sys.exit(17)',
    'import sys; [sys.stdout.buffer.write(b"x" * 8192) for _ in range(1024)]',
    'import json; print(json.dumps({"text":"x"*100001}))',
], ids=["crash", "crash-with-valid-output", "output-flood", "text-too-large"])
def test_child_crash_or_output_flood_does_not_return_text(monkeypatch, script):
    children, launches = synthetic_child(monkeypatch, script)
    with pytest.raises(VaultError):
        recognize_image(b"synthetic input", timeout_s=5)
    assert all(child.poll() is not None for child in children)
    assert launches and all(not Path(kwargs["cwd"]).exists() for _, kwargs in launches)


@pytest.mark.skipif(os.name != "nt", reason="Windows kill-on-close process job")
def test_deadline_reaps_worker_descendants_too(tmp_path, monkeypatch):
    import _winapi

    pid_file = tmp_path / "synthetic-child.pid"
    script = (
        "import subprocess, sys, time; from pathlib import Path; "
        "child=subprocess.Popen([sys.executable,'-I','-c','import time; time.sleep(60)']); "
        f"Path({str(pid_file)!r}).write_text(str(child.pid)); "
        "time.sleep(60)"
    )
    children, _ = synthetic_child(monkeypatch, script)
    with pytest.raises(VaultError, match="deadline exceeded"):
        recognize_image(b"synthetic input", timeout_s=1)
    assert all(child.poll() is not None for child in children)
    pid = int(pid_file.read_text())
    try:
        handle = _winapi.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    except OSError:
        pass  # Process object already disappeared.
    else:
        try:
            assert _winapi.WaitForSingleObject(handle, 0) == _winapi.WAIT_OBJECT_0
        finally:
            _winapi.CloseHandle(handle)


@pytest.mark.parametrize("mode,expected", [("missing", "models are missing"),
                                          ("tampered", "SHA256 mismatch")])
def test_real_worker_refuses_missing_or_tampered_models_in_a_temporary_copy(tmp_path, monkeypatch, mode, expected):
    import shutil
    from vault_v2 import ocr_process

    worker = tmp_path / "ocr_worker.py"
    shutil.copyfile(Path(ocr_process.__file__).with_name("ocr_worker.py"), worker)
    if mode == "tampered":
        models = tmp_path / "ocr_models"
        models.mkdir()
        (models / "eng.traineddata").write_bytes(b"synthetic tampered model")
        (models / "rus.traineddata").write_bytes(b"synthetic tampered model")
    original = subprocess.Popen
    children = []

    def launch(argv, **kwargs):
        child = original([*argv[:3], str(worker)], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", launch)
    with pytest.raises(VaultError, match=expected):
        recognize_image(b"synthetic input")
    assert all(child.poll() is not None for child in children)


def test_duplicate_child_json_fields_are_not_silently_overwritten(monkeypatch):
    synthetic_child(monkeypatch, 'print(\'{"text":"first","text":"second"}\')')
    with pytest.raises(VaultError):
        recognize_image(b"synthetic input")


@pytest.mark.parametrize("deadline", [0, -1, float("inf"), float("nan"), True, "1", 10**1000],
                         ids=["zero", "negative", "infinity", "nan", "bool", "string", "huge-integer"])
def test_invalid_deadline_is_refused_before_launch(monkeypatch, deadline):
    def no_launch(*args, **kwargs):
        raise AssertionError("invalid deadline cannot launch a child")

    monkeypatch.setattr(subprocess, "Popen", no_launch)
    with pytest.raises(VaultError, match="deadline is invalid"):
        recognize_image(b"synthetic input", timeout_s=deadline)


@pytest.mark.parametrize("format", ["TIFF", "PNG", "GIF"])
def test_multiframe_image_is_refused_instead_of_silently_reading_only_first_frame(format):
    first, second = Image.new("RGB", (200, 120), "white"), Image.new("RGB", (200, 120), "black")
    stream = BytesIO()
    try:
        first.save(stream, format=format, save_all=True, append_images=[second])
    finally:
        first.close()
        second.close()
    with pytest.raises(VaultError, match="Multi-page or animated image"):
        recognize_image(stream.getvalue())
