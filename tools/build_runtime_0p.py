"""Explicit build-time downloads for a synthetic-only Windows runtime 0P bundle."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
from urllib.parse import urlsplit
import urllib.request
import zipfile


_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
             *(f"lpt{i}" for i in range(1, 10))}
TOOLS = ("synthetic_ocr_files.py", "synthetic_viewer_files.py",
         "qualify_runtime_0p.py", "qualify_pdf_process_preview.py")
NOTICE = """Vault V2 runtime 0P: SYNTHETIC QUALIFICATION CANDIDATE ONLY

The pinned runtime version, sources and security status are recorded in
runtime-assets.json. Upstream metadata does not establish local qualification.
This package is not qualified for production or clean machines. It omits web
static assets and a complete desktop installer. No runtime network installer,
pip bootstrap, developer venv, or owner data is included by this builder.

The Python license remains at python/LICENSE.txt. Wheel licenses, notices,
metadata and bundled native-library notices remain inside vendor. Tesseract
model licensing remains at vault_v2/ocr_models/LICENSE. runtime-assets.json
records pinned upstream sources, SHA256 values and lengths. File inventory is
recorded separately in runtime-bundle-files.json. Preservation of these notices
does not itself qualify all third-party redistribution obligations.
"""


class BuildError(Exception):
    """A bundle input was refused; no completed output was published."""


def _archive_members(path: Path):
    with zipfile.ZipFile(path) as archive:
        members, seen, total = [], set(), 0
        for info in archive.infolist():
            name = info.orig_filename.rstrip("/")
            parts = name.split("/")
            mode = info.external_attr >> 16
            if (not name or "\\" in name or ":" in name or "\x00" in name
                    or any(not p or p in {".", ".."} or p.rstrip(" .") != p
                           or p.split(".")[0].casefold() in _RESERVED for p in parts)
                    or any(ord(c) < 32 for c in name)
                    or stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}
                    or (info.flag_bits & 1) or name.casefold() in seen):
                raise BuildError(f"Unsafe ZIP member in {path.name}: {info.filename!r}")
            total += info.file_size
            if info.file_size > 512 * 1024 * 1024 or total > 1024 * 1024 * 1024:
                raise BuildError(f"Unsafe ZIP expanded size: {path.name}")
            seen.add(name.casefold())
            members.append(info)
        return members


def _plain_path(path: Path):
    for candidate in (*reversed(path.parents), path):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            raise BuildError(f"Linked paths are refused: {candidate}")


def _copy_source(source: Path, destination: Path):
    _plain_path(source)
    if not source.is_file():
        raise BuildError(f"Required source is missing: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes())


def _extract(archive_path: Path, destination: Path, *, wheel: bool):
    members = _archive_members(archive_path)
    with zipfile.ZipFile(archive_path) as archive:
        for info in members:
            parts = info.filename.rstrip("/").split("/")
            if wheel and len(parts) > 2 and parts[0].endswith(".data") and parts[1] in {"purelib", "platlib"}:
                parts = parts[2:]
            target = destination.joinpath(*parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise BuildError(f"Archive file collision: {target.relative_to(destination)}")
            with archive.open(info) as incoming, target.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)


def _json_write(path: Path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def _configure_python(python_root: Path):
    stdlib = [path for path in python_root.glob("python*.zip")
              if path.is_file() and re.fullmatch(r"python3[0-9]+\.zip", path.name)]
    pth_files = list(python_root.glob("*._pth"))
    if (len(stdlib) != 1 or len(pth_files) != 1
            or pth_files[0].name != stdlib[0].stem + "._pth"
            or not pth_files[0].is_file()):
        raise BuildError("Embedded runtime must have exactly one stdlib ZIP and matching ._pth")
    pth_files[0].write_text(f"{stdlib[0].name}\n.\n../vendor\n..\n", encoding="utf-8", newline="\n")


def _validate_manifest(manifest):
    if not isinstance(manifest, dict) or manifest.get("schema") != "vault-v2-runtime-assets@1":
        raise BuildError("Invalid asset manifest schema")
    assets = manifest.get("assets")
    if not isinstance(assets, list) or not assets:
        raise BuildError("Invalid asset list")
    names, kinds = set(), []
    for asset in assets:
        if not isinstance(asset, dict):
            raise BuildError("Invalid asset record")
        name, kind = asset.get("filename", ""), asset.get("kind", "")
        url = urlsplit(asset.get("url", ""))
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", name)
                or name.endswith(".") or name.split(".")[0].casefold() in _RESERVED
                or name.casefold() in names or kind not in {"embedded", "wheel", "model", "model-license"}
                or url.scheme != "https" or not url.netloc or url.username or url.password
                or not isinstance(asset.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", asset["sha256"])
                or type(asset.get("size")) is not int or not 0 < asset["size"] <= 512 * 1024 * 1024
                or (kind == "embedded" and not name.endswith(".zip"))
                or (kind == "wheel" and not name.endswith(".whl"))
                or (kind == "model" and name not in {"eng.traineddata", "rus.traineddata"})
                or (kind == "model-license" and name != "LICENSE")):
            raise BuildError(f"Invalid asset: {name!r}")
        names.add(name.casefold())
        kinds.append(kind)
    if kinds.count("embedded") != 1 or kinds.count("model-license") != 1 or kinds.count("model") != 2 or "wheel" not in kinds:
        raise BuildError("Invalid asset roles: embedded, wheels, two models and model license required")
    return assets


def _file_digest(path: Path):
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _verify(path: Path, asset):
    _plain_path(path)
    digest, size = _file_digest(path)
    if digest != asset["sha256"]:
        raise BuildError(f"SHA256 mismatch: {asset['filename']}")
    if size != asset["size"]:
        raise BuildError(f"Size mismatch: {asset['filename']}")


def _fetch(asset, cache: Path):
    target = cache / asset["filename"]
    _plain_path(target)
    if target.exists():
        _verify(target, asset)
        return target
    cache.mkdir(parents=True, exist_ok=True)
    print(f"Fetching pinned asset: {asset['filename']} ({asset['size']} bytes)", flush=True)
    request = urllib.request.Request(asset["url"], headers={"User-Agent": "Vault-V2-explicit-runtime-0P-build"})
    pending = None
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if urlsplit(response.geturl()).scheme != "https":
                raise BuildError("Download redirected away from HTTPS")
            with tempfile.NamedTemporaryFile(dir=cache, prefix=".runtime-asset-", delete=False) as stream:
                pending = Path(stream.name)
                remaining = asset["size"] + 1
                while remaining:
                    block = response.read(min(1024 * 1024, remaining))
                    if not block:
                        break
                    stream.write(block)
                    remaining -= len(block)
        _verify(pending, asset)
        _plain_path(target)
        if target.exists():
            _verify(target, asset)
        else:
            os.replace(pending, target)
        return target
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


def build_bundle(source_root: Path, output: Path, cache: Path, *, manifest_path: Path | None = None):
    source_root, output, cache = (Path(p).absolute() for p in (source_root, output, cache))
    for path in (source_root, output, cache):
        if ".." in path.parts:
            raise BuildError("Parent traversal is refused in build paths")
        _plain_path(path)
    if output.exists():
        raise BuildError("Output already exists; choose a new directory")
    if output.is_relative_to(source_root) or cache.is_relative_to(source_root):
        raise BuildError("Output and cache must be outside the source checkout")
    if cache.is_relative_to(output) or output.is_relative_to(cache):
        raise BuildError("Output and cache must be separate directories")
    manifest_path = manifest_path or source_root / "tools" / "runtime-0p-assets.json"
    _plain_path(Path(manifest_path).absolute())
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    assets = _validate_manifest(manifest)
    for asset in assets:
        _fetch(asset, cache)
        if asset["kind"] in {"embedded", "wheel"}:
            _archive_members(cache / asset["filename"])
    _plain_path(source_root / "vault_v2")
    sources = sorted((source_root / "vault_v2").glob("*.py"))
    if not (source_root / "vault_v2" / "__init__.py").is_file():
        raise BuildError("Source package must contain __init__.py")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".vault-runtime-0p-", dir=output.parent) as temporary:
        staging = Path(temporary) / "bundle"
        staging.mkdir()
        for source in sources:
            _copy_source(source, staging / "vault_v2" / source.name)
        for name in TOOLS:
            _copy_source(source_root / "tools" / name, staging / "tools" / name)
        for asset in assets:
            cached = cache / asset["filename"]
            _verify(cached, asset)
            if asset["kind"] in {"embedded", "wheel"}:
                _extract(cached, staging / ("python" if asset["kind"] == "embedded" else "vendor"),
                         wheel=asset["kind"] == "wheel")
            else:
                target = staging / "vault_v2" / "ocr_models" / asset["filename"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(cached.read_bytes())
        _configure_python(staging / "python")
        _json_write(staging / "vault-install.json", {"schema": "vault-v2-install@1", "worker_python": "python/python.exe"})
        _json_write(staging / "runtime-assets.json", manifest)
        (staging / "RUNTIME-0P-NOTICES.txt").write_text(NOTICE, encoding="utf-8", newline="\n")
        inventory = []
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                digest, size = _file_digest(path)
                inventory.append({"path": path.relative_to(staging).as_posix(), "size": size,
                                  "sha256": digest})
        _json_write(staging / "runtime-bundle-files.json", {"schema": "vault-v2-runtime-files@1", "files": inventory})
        _plain_path(output)
        if output.exists():
            raise BuildError("Output appeared during build; publication refused")
        staging.rename(output)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="new bundle directory, outside source checkout")
    parser.add_argument("--cache", required=True, type=Path, help="asset cache outside source checkout and output")
    args = parser.parse_args(argv)
    try:
        output = build_bundle(Path(__file__).resolve().parent.parent, args.output, args.cache)
    except (BuildError, OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"Runtime 0P build refused or failed: {exc}", file=sys.stderr)
        return 1
    print(f"Built synthetic-only runtime 0P: {output}")
    print("Pinned runtime metadata is in runtime-assets.json; production and clean-machine qualification remain OPEN.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
