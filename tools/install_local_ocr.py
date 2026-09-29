"""Explicit private-use setup only; the Vault runtime never imports this tool.

Run with the application's CPython 3.12 x64 Windows venv and --install.
All downloads are fixed and checked before pip or model publication.
"""
import argparse
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import sysconfig
import tempfile
import urllib.request


@dataclass(frozen=True)
class Asset:
    filename: str
    url: str
    sha256: str
    size: int


MODEL_COMMIT = "87416418657359cb625c412a48b6e1d6d41c29bd"
WHEEL = Asset(
    "tesserocr-2.10.0-cp312-cp312-win_amd64.whl",
    "https://github.com/simonflueckiger/tesserocr-windows_build/releases/download/"
    "tesserocr-v2.10.0-tesseract-5.5.2/tesserocr-2.10.0-cp312-cp312-win_amd64.whl",
    "e05d41a2b0e6f38f3a5195d05a73674d72152a775d1b8ebe481ca9306f94d27a", 4213119,
)
MODELS = (
    Asset("eng.traineddata", f"https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/{MODEL_COMMIT}/eng.traineddata",
          "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2", 4113088),
    Asset("rus.traineddata", f"https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/{MODEL_COMMIT}/rus.traineddata",
          "e16e5e036cce1d9ec2b00063cf8b54472625b9e14d893a169e2b0dedeb4df225", 3861738),
)
MODEL_DIRECTORY = Path(__file__).resolve().parent.parent / "vault_v2" / "ocr_models"


class InstallError(Exception):
    pass


def check_host():
    if (sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 12)
            or sys.platform != "win32" or sysconfig.get_platform() != "win-amd64"
            or struct.calcsize("P") != 8):
        raise InstallError("Use CPython 3.12 Windows AMD64; no alternate wheel is selected.")
    if sys.prefix == sys.base_prefix:
        raise InstallError("Run with the application's .venv Python, not a global interpreter.")


def verify_asset(data: bytes, asset: Asset):
    if hashlib.sha256(data).hexdigest() != asset.sha256:
        raise InstallError(f"SHA256 mismatch: {asset.filename}; nothing from this download is installed.")
    if len(data) != asset.size:
        raise InstallError(f"Size mismatch: {asset.filename}.")


def _download(asset: Asset, directory: Path) -> Path:
    request = urllib.request.Request(asset.url, headers={"User-Agent": "Vault-V2-explicit-private-OCR-setup"})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(asset.size + 1)
    verify_asset(data, asset)
    path = directory / asset.filename
    path.write_bytes(data)
    return path


def _plain_destination(path: Path):
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            raise InstallError("Linked model destinations are refused.")


def install_local_ocr(model_directory: Path = MODEL_DIRECTORY):
    """Explicit setup operation. No runtime caller and no implicit invocation."""
    check_host()
    model_directory = Path(model_directory).absolute()
    if ".." in model_directory.parts:
        raise InstallError("Model destination must not contain parent traversal.")
    _plain_destination(model_directory)
    for asset in MODELS:
        _plain_destination(model_directory / asset.filename)
    with tempfile.TemporaryDirectory(prefix="vault-ocr-install-") as temporary:
        staging = Path(temporary)
        downloaded = {asset.filename: _download(asset, staging) for asset in (WHEEL, *MODELS)}
        # Check again immediately before the package effect. This is not an
        # adversarial same-user filesystem protection or a whole-install rollback.
        for asset in (WHEEL, *MODELS):
            verify_asset(downloaded[asset.filename].read_bytes(), asset)
        subprocess.run(
            [sys.executable, "-I", "-m", "pip", "--isolated", "install", "--disable-pip-version-check",
             "--no-index", "--no-deps", str(downloaded[WHEEL.filename])],
            check=True, timeout=120, capture_output=True, text=True, shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        _plain_destination(model_directory)
        model_directory.mkdir(parents=True, exist_ok=True)
        for asset in MODELS:
            target = model_directory / asset.filename
            _plain_destination(target)
            data = downloaded[asset.filename].read_bytes()
            verify_asset(data, asset)
            with tempfile.NamedTemporaryFile(dir=model_directory, prefix=".ocr-install-", delete=False) as stream:
                pending = Path(stream.name)
                stream.write(data)
            try:
                os.replace(pending, target)
            finally:
                pending.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true", help="explicitly download and install the pinned private-use OCR stack")
    args = parser.parse_args(argv)
    if not args.install:
        parser.print_help()
        return 0
    try:
        install_local_ocr()
    except (InstallError, OSError, subprocess.SubprocessError) as exc:
        print(f"OCR setup refused or failed: {exc}", file=sys.stderr)
        return 1
    print("Pinned local OCR setup complete. No owner documents were read. Restart Vault after setup.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
