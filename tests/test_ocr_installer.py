"""Manual installer refuses untrusted bytes before package/model effects."""
from io import BytesIO
from pathlib import Path
import subprocess
import sys
import urllib.request

import pytest


def test_bad_download_hash_never_runs_pip_or_publishes_models(tmp_path, monkeypatch):
    from tools.install_local_ocr import InstallError, install_local_ocr

    requested = []

    def download(request, **kwargs):
        requested.append(request.full_url)
        return BytesIO(b"wrong synthetic distribution")

    def forbidden_process(*args, **kwargs):
        pytest.fail("pip must not run for a SHA256 mismatch")

    monkeypatch.setattr(urllib.request, "urlopen", download)
    monkeypatch.setattr(subprocess, "run", forbidden_process)
    destination = tmp_path / "models"
    with pytest.raises(InstallError, match="SHA256 mismatch"):
        install_local_ocr(destination)
    assert len(requested) == 1
    assert not destination.exists()


@pytest.mark.parametrize("bad_language", ["eng", "rus"])
def test_bad_model_hash_refuses_before_pip_and_before_publication(tmp_path, monkeypatch, bad_language):
    from tools.install_local_ocr import InstallError, MODELS, WHEEL, install_local_ocr

    repository = Path(__file__).resolve().parent.parent
    wheel = repository / ".venv" / "ocr-wheel-probe" / WHEEL.filename
    if not wheel.is_file():
        pytest.skip("optional previously verified vendor-wheel fixture is not retained in Git")
    payloads = {WHEEL.url: wheel.read_bytes()}
    payloads.update({asset.url: (repository / "vault_v2" / "ocr_models" / asset.filename).read_bytes()
                     for asset in MODELS})
    payloads[next(asset.url for asset in MODELS if asset.filename == f"{bad_language}.traineddata")] = b"tampered model"
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, **kwargs: BytesIO(payloads[request.full_url]))

    def forbidden_process(*args, **kwargs):
        pytest.fail("pip must not run before every model hash passes")

    monkeypatch.setattr(subprocess, "run", forbidden_process)
    destination = tmp_path / "models"
    with pytest.raises(InstallError, match=f"SHA256 mismatch: {bad_language}"):
        install_local_ocr(destination)
    assert not destination.exists()


def test_without_explicit_install_flag_does_not_use_network_or_pip(monkeypatch, capsys):
    from tools.install_local_ocr import main

    def forbidden(*args, **kwargs):
        pytest.fail("help-only invocation cannot install or download")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    assert main([]) == 0
    assert "--install" in capsys.readouterr().out


def test_wrong_python_refused_before_network_or_pip(tmp_path, monkeypatch):
    from tools.install_local_ocr import InstallError, install_local_ocr

    def forbidden(*args, **kwargs):
        pytest.fail("unsupported host cannot install or download")

    monkeypatch.setattr(sys, "version_info", (3, 13, 0))
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    with pytest.raises(InstallError, match="CPython 3.12 Windows AMD64"):
        install_local_ocr(tmp_path / "models")


def test_verified_assets_use_offline_pip_and_publish_exact_models(tmp_path, monkeypatch):
    from tools.install_local_ocr import MODELS, WHEEL, install_local_ocr

    repository = Path(__file__).resolve().parent.parent
    wheel = repository / ".venv" / "ocr-wheel-probe" / WHEEL.filename
    if not wheel.is_file():
        pytest.skip("optional previously verified vendor-wheel fixture is not retained in Git")
    payloads = {WHEEL.url: wheel.read_bytes()}
    payloads.update({asset.url: (repository / "vault_v2" / "ocr_models" / asset.filename).read_bytes()
                     for asset in MODELS})
    requested, commands = [], []

    def download(request, **kwargs):
        requested.append(request.full_url)
        return BytesIO(payloads[request.full_url])

    def completed_install(argv, **kwargs):
        assert len(requested) == 3, "the installer verifies all assets before pip"
        assert argv[:-1] == [sys.executable, "-I", "-m", "pip", "--isolated", "install",
                            "--disable-pip-version-check", "--no-index", "--no-deps"]
        assert Path(argv[-1]).read_bytes() == payloads[WHEEL.url]
        assert kwargs["shell"] is False and kwargs["timeout"] == 120
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(urllib.request, "urlopen", download)
    monkeypatch.setattr(subprocess, "run", completed_install)
    destination = tmp_path / "models"
    install_local_ocr(destination)
    assert len(commands) == 1
    assert sorted(path.name for path in destination.iterdir()) == ["eng.traineddata", "rus.traineddata"]
    for asset in MODELS:
        assert (destination / asset.filename).read_bytes() == payloads[asset.url]
