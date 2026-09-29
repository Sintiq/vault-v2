"""Explicit full Windows payload builder; build-time only, never executes payloads."""
from __future__ import annotations

import base64
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import tempfile
import sys
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from tools import build_runtime_0p as runtime
from vault_v2.releases import ReleaseDescription, verify_release
from vault_v2.release_stage import _https_location, _StageFailure

BuildError = runtime.BuildError
CATALOG = Path(__file__).with_name("windows-payload-files.json")
NOTICE = """Vault V2 Windows payload: RELEASE AND CLEAN-MACHINE QUALIFICATION OPEN

Program files only. No owner archive, private keys, settings, venv, history,
test fixture tools, runtime downloads or pip bootstrap are bundled.
Missing public release key or update channel disables updates; absent Android APK is recorded in
payload-metadata.json. A payload build is not an installer qualification.

Original Vault V2 code/documentation: MIT, see LICENSE. This does not relicense
third-party components. Python license: python/LICENSE.txt. Dependency licenses/notices remain in vendor,
including wheel dist-info and native-library notices. OCR model license remains
at vault_v2/ocr_models/LICENSE. runtime-assets.json records pinned upstream sources,
SHA256 values and lengths. windows-payload-files.json inventories the full output
except that inventory itself. License preservation is not a legal-compliance audit.
python/msvcp140.dll is an exact app-local copy of vendor/PySide6/msvcp140.dll
from the verified wheel by default. Original dependency notices remain in vendor. This lets
isolated workers resolve their C++ runtime without importing Qt or changing PATH.
If qt-subset.json is present, it records an explicit wheel-pinned selection and
every omitted member. Upstream dist-info/RECORD describes the original wheel,
not this subset; windows-payload-files.json is the actual output inventory.
If runtime-redist.json is present, its explicit hash-pinned runtime inputs replace
the named DLL paths, including the worker copy. Original wheel RECORD files then
describe upstream bytes, not those replacements, even without a Qt subset.
The redist report records byte provenance, not license eligibility or clearance.
MSVC-RECIPIENT-TERMS.txt, when explicitly supplied, is shown by the installer;
its inclusion and agreement mechanism do not certify legal adequacy.
If third-party-notices/ exists, it contains explicitly supplied, hash-checked
supplemental texts and their inputs.json. Inclusion does not attest completeness,
binary/source correspondence, license eligibility or permission to distribute.
"""


def _read_bounded(path: Path, limit: int, label: str) -> bytes:
    path = Path(path).absolute()
    runtime._plain_path(path)
    if not path.is_file():
        raise BuildError(f"Missing {label}")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if not raw or len(raw) > limit:
        raise BuildError(f"Invalid {label} size")
    return raw


def _public_key(path: Path | None) -> bytes | None:
    if path is None:
        return None
    raw = _read_bounded(path, 1024, "public key")
    try:
        key = serialization.load_der_public_key(base64.b64decode(raw.strip(b" \t\r\n\v\f"), validate=True))
        if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
            raise ValueError("not P256")
    except (TypeError, ValueError):
        raise BuildError("Invalid public key: expected base64 P-256 SPKI") from None
    return raw


_REDIST_NAMES = frozenset(name + ".dll" for name in (
    "msvcp140", "msvcp140_1", "msvcp140_2", "msvcp140_codecvt_ids",
    "vcruntime140", "vcruntime140_1", "concrt140", "vcamp140", "vccorlib140", "vcomp140",
))


def _redist_profile(path):
    if path is None:
        return None
    path = Path(path).absolute()
    def unique(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise BuildError("Duplicate redist profile key")
            result[name] = value
        return result
    try:
        profile = json.loads(_read_bounded(path, 32 * 1024, "redist profile"), object_pairs_hook=unique)
    except (ValueError, RecursionError):
        raise BuildError("Invalid redist profile JSON") from None
    if (not isinstance(profile, dict) or set(profile) != {"schema", "version", "files"}
            or profile["schema"] != "vault-msvc-redist@1"
            or not isinstance(profile["version"], str)
            or re.fullmatch(r"[0-9]{1,5}(?:\.[0-9]{1,5}){3}", profile["version"]) is None
            or not isinstance(profile["files"], list) or len(profile["files"]) != len(_REDIST_NAMES)):
        raise BuildError("Invalid redist profile")
    contents = {}
    for entry in profile["files"]:
        if (not isinstance(entry, dict) or set(entry) != {"name", "source", "size", "sha256"}
                or not isinstance(entry["name"], str) or entry["name"] not in _REDIST_NAMES
                or entry["name"] in contents
                or type(entry["size"]) is not int or not 0 < entry["size"] <= 8 * 1024 * 1024
                or not isinstance(entry["sha256"], str)
                or re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is None
                or not isinstance(entry["source"], str)
                or re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", entry["source"]) is None
                or any(part in {".", ".."} for part in entry["source"].split("/"))
                or entry["source"].split("/")[-1] != entry["name"]):
            raise BuildError("Invalid redist file entry")
        raw = _read_bounded(path.parent / entry["source"], entry["size"], "redist DLL")
        if len(raw) != entry["size"] or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise BuildError("Redist DLL digest/size mismatch")
        contents[entry["name"]] = raw
    return profile, contents


def _apply_redist(redist, staging):
    profile, contents = redist
    replaced = []
    for path in sorted(staging.rglob("*.dll")):
        if path.name.lower() not in _REDIST_NAMES:
            continue
        relative = path.relative_to(staging).as_posix()
        if path.parent.relative_to(staging).as_posix() not in {"python", "vendor/PySide6", "vendor/shiboken6"}:
            raise BuildError("Unexpected redist destination")
        runtime._plain_path(path)
        path.write_bytes(contents[path.name.lower()])
        replaced.append(relative)
    runtime._json_write(staging / "runtime-redist.json", {
        "schema": "vault-msvc-redist-applied@1", "profile": profile,
        "replaced_paths": replaced, "license_status": "not_attested_by_builder",
    })


def _notice_bundle(path):
    if path is None:
        return None
    path = Path(path).absolute()
    raw = _read_bounded(path, 64 * 1024, "notice manifest")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise BuildError("Duplicate notice manifest key")
            result[key] = value
        return result
    try:
        manifest = json.loads(raw, object_pairs_hook=unique)
    except ValueError:
        raise BuildError("Invalid notice manifest JSON") from None
    if (not isinstance(manifest, dict) or set(manifest) != {"schema", "files"}
            or manifest["schema"] != "vault-notice-inputs@1"
            or not isinstance(manifest["files"], list) or not 1 <= len(manifest["files"]) <= 256):
        raise BuildError("Invalid notice manifest schema")
    files, seen, total = {}, set(), 0
    for entry in manifest["files"]:
        if not isinstance(entry, dict) or set(entry) != {"name", "size", "sha256"}:
            raise BuildError("Invalid notice file record")
        name = entry["name"]
        if (not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\.txt", name) is None
                or name.split(".")[0].casefold() in runtime._RESERVED or name.casefold() in seen
                or type(entry["size"]) is not int or not 1 <= entry["size"] <= 2 * 1024 * 1024
                or not isinstance(entry["sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is None):
            raise BuildError("Invalid notice file identity")
        total += entry["size"]
        if total > 16 * 1024 * 1024:
            raise BuildError("Notice bundle exceeds total size limit")
        data = _read_bounded(path.parent / name, entry["size"], "notice file")
        if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise BuildError("Notice file integrity mismatch")
        seen.add(name.casefold())
        files[name] = data
    return raw, files


def _check_source_api(path: Path, expected: int) -> None:
    """Check declared metadata without importing or executing the application."""
    raw = _read_bounded(path, 1024 * 1024, "API_VERSION source")
    try:
        body = ast.parse(raw, filename="api.py").body
    except (SyntaxError, ValueError):
        raise BuildError("API_VERSION source is not valid Python") from None
    declarations = []
    for statement in body:
        if isinstance(statement, ast.Assign):
            targets = statement.targets
        elif isinstance(statement, (ast.AnnAssign, ast.AugAssign)):
            targets = [statement.target]
        else:
            continue
        if any(isinstance(target, ast.Name) and target.id == "API_VERSION" for target in targets):
            declarations.append(statement)
    if len(declarations) != 1:
        raise BuildError("API_VERSION requires one top-level literal assignment")
    declaration = declarations[0]
    if (not isinstance(declaration, (ast.Assign, ast.AnnAssign))
            or (isinstance(declaration, ast.Assign) and len(declaration.targets) != 1)
            or not isinstance(declaration.value, ast.Constant)
            or type(declaration.value.value) is not int
            or declaration.value.value != expected):
        raise BuildError("API_VERSION must be a literal integer matching the requested API")


def _android_package(apk_path, manifest_path, signature_path, public_key, api_version):
    parts = (apk_path, manifest_path, signature_path)
    if all(path is None for path in parts):
        return None
    if public_key is None or any(path is None for path in parts):
        raise BuildError("Android package requires explicit APK, manifest, signature and public key")
    raw = _read_bounded(manifest_path, 16384, "Android manifest")
    signature = _read_bounded(signature_path, 1024, "Android signature")
    release = verify_release(raw, signature, public_key, "android", 0, api_version)
    if not isinstance(release, ReleaseDescription) or release.file != "vault.apk":
        raise BuildError("Android release verification refused")
    path = Path(apk_path).absolute()
    runtime._plain_path(path)
    if not path.is_file() or path.stat().st_size != release.size:
        raise BuildError("Android package size mismatch")
    digest, size = runtime._file_digest(path)
    if size != release.size or digest != release.sha256:
        raise BuildError("Android package digest mismatch")
    return path, release, raw, signature


def _copy_android(android, staging):
    source, release, raw, signature = android
    runtime._plain_path(source)
    destination = staging / "vault_v2/web"
    digest, size = hashlib.sha256(), 0
    with source.open("rb") as incoming, (destination / "vault.apk").open("xb") as outgoing:
        while block := incoming.read(1024 * 1024):
            size += len(block)
            if size > release.size:
                raise BuildError("Android package changed while copying")
            digest.update(block)
            outgoing.write(block)
    if size != release.size or digest.hexdigest() != release.sha256:
        raise BuildError("Android package changed while copying")
    (destination / "vault-release.json").write_bytes(raw)
    (destination / "vault-release.json.sig").write_bytes(signature)


def _qt_profile(path, assets):
    if path is None:
        return None
    raw = _read_bounded(Path(path), 256 * 1024, "Qt profile")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise BuildError("Duplicate Qt profile key")
            result[key] = value
        return result
    try:
        profile = json.loads(raw, object_pairs_hook=unique)
    except ValueError:
        raise BuildError("Invalid Qt profile JSON") from None
    if (not isinstance(profile, dict) or set(profile) != {"schema", "wheel", "sha256", "members"}
            or profile.get("schema") != "vault-qt-subset@1"
            or not any(a["kind"] == "wheel" and a["filename"] == profile.get("wheel")
                       and a["sha256"] == profile.get("sha256") for a in assets)):
        raise BuildError("Qt profile must bind an exact verified wheel")
    members = profile["members"]
    if (not isinstance(members, list) or not members or len(members) > 4096
            or any(not isinstance(name, str) or not name.startswith("PySide6/")
                   or any(part in {"", ".", ".."} or re.fullmatch(r"[A-Za-z0-9_.+-]+", part) is None
                          for part in name.split("/")) for name in members)
            or len({name.casefold() for name in members}) != len(members)):
        raise BuildError("Invalid Qt profile members")
    return profile, hashlib.sha256(raw).hexdigest()


def _extract_qt_subset(cached, staging, selection):
    profile, profile_digest = selection
    # Extract into an isolated build directory so normal ZIP validation still
    # covers omitted members too. Never remove files from a published payload.
    unpacked = staging.parent / "qt-profile-source"
    runtime._extract(cached, unpacked, wheel=True)
    files = {p.relative_to(unpacked).as_posix(): p for p in unpacked.rglob("*") if p.is_file()}
    selected = set(profile["members"])
    if not selected <= files.keys():
        raise BuildError("Qt profile references missing wheel members")
    retained, omitted = [], []
    for name, source in sorted(files.items()):
        notice = any(word in name.casefold() for word in
                     ("license", "licence", "copying", "copyright", "notice", "third-party", "third_party"))
        if not name.startswith("PySide6/") or name in selected or notice:
            target = staging / "vendor" / name
            if target.exists():
                raise BuildError("Qt subset archive file collision")
            runtime._copy_source(source, target)
            retained.append(name)
        else:
            omitted.append(name)
    runtime._json_write(staging / "qt-subset.json", {
        "schema": "vault-qt-subset-result@1", "profile_sha256": profile_digest,
        "wheel": profile["wheel"], "wheel_sha256": profile["sha256"],
        "retained_members": retained, "omitted_members": omitted,
        "release_qualified": False,
        "notice_scope": "All non-PySide6 members and filename-matched notices preserved; completeness not attested",
    })


def build_payload(source_root: Path, output: Path, cache: Path, *, version: str,
                  version_code: int, api_version: int, manifest_path: Path | None = None,
                  public_key_path: Path | None = None, apk_path: Path | None = None,
                  apk_manifest_path: Path | None = None, apk_signature_path: Path | None = None,
                  update_channel: str | None = None, qt_profile_path: Path | None = None,
                  notice_bundle_path: Path | None = None,
                  redist_profile_path: Path | None = None,
                  recipient_terms_path: Path | None = None) -> Path:
    source_root, output, cache = (Path(p).absolute() for p in (source_root, output, cache))
    if (type(version) is not str or re.fullmatch(r"[\x20-\x7e]{1,64}", version) is None
            or any(type(value) is not int or not 1 <= value <= 2147483647 for value in (version_code, api_version))):
        raise BuildError("Invalid installed identity")
    for path in (source_root, output, cache):
        if ".." in path.parts:
            raise BuildError("Parent traversal is refused in build paths")
        runtime._plain_path(path)
    if output.exists():
        raise BuildError("Output already exists; choose a new directory")
    if output.is_relative_to(source_root) or cache.is_relative_to(source_root):
        raise BuildError("Output and cache must be outside the source checkout")
    if cache.is_relative_to(output) or output.is_relative_to(cache):
        raise BuildError("Output and cache must be separate directories")
    runtime._plain_path(CATALOG)
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    for relative in catalog["files"]:
        path = source_root / relative
        runtime._plain_path(path)
        if not path.is_file():
            raise BuildError(f"Required source is missing: {relative}")
    _check_source_api(source_root / "vault_v2/api.py", api_version)
    if update_channel is not None:
        try:
            _https_location(update_channel, "vault-release.json")
        except _StageFailure:
            raise BuildError("Invalid update channel: expected an explicit HTTPS directory URL") from None
    public_key = _public_key(public_key_path)
    notices = _notice_bundle(notice_bundle_path)
    redist = _redist_profile(redist_profile_path)
    recipient_terms = (_read_bounded(recipient_terms_path, 65536, "recipient terms")
                       if recipient_terms_path is not None else None)
    android = _android_package(apk_path, apk_manifest_path, apk_signature_path, public_key, api_version)
    manifest_path = Path(manifest_path or source_root / "tools/runtime-0p-assets.json").absolute()
    runtime._plain_path(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assets = runtime._validate_manifest(manifest)
    qt_selection = _qt_profile(qt_profile_path, assets)
    for asset in assets:
        runtime._fetch(asset, cache)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".vault-payload-", dir=output.parent) as temporary:
        staging = Path(temporary) / "payload"
        staging.mkdir()
        for relative in catalog["files"]:
            runtime._copy_source(source_root / relative, staging / relative)
        _check_source_api(staging / "vault_v2/api.py", api_version)
        for asset in assets:
            cached = cache / asset["filename"]
            runtime._verify(cached, asset)
            if qt_selection is not None and asset["filename"] == qt_selection[0]["wheel"]:
                _extract_qt_subset(cached, staging, qt_selection)
            elif asset["kind"] in {"embedded", "wheel"}:
                runtime._extract(cached, staging / ("python" if asset["kind"] == "embedded" else "vendor"),
                                 wheel=asset["kind"] == "wheel")
            else:
                runtime._copy_source(cached, staging / "vault_v2/ocr_models" / asset["filename"])
        runtime._configure_python(staging / "python")
        for name in ("python.exe", "pythonw.exe", "LICENSE.txt"):
            if not (staging / "python" / name).is_file():
                raise BuildError(f"Incomplete embedded runtime: missing {name}")
        # Workers start in isolated mode without Qt's DLL-directory registration.
        # Use only verified payload bytes, never a host/system runtime installation.
        for name in ("vcruntime140.dll", "vcruntime140_1.dll"):
            if not (staging / "python" / name).is_file():
                raise BuildError(f"Incomplete embedded runtime: missing {name}")
        cpp_source = staging / "vendor/PySide6/msvcp140.dll"
        cpp_destination = staging / "python/msvcp140.dll"
        if not cpp_source.is_file() or cpp_source.stat().st_size == 0:
            raise BuildError("Incomplete worker runtime: missing PySide6/msvcp140.dll")
        if cpp_destination.exists():
            raise BuildError("Worker runtime collision: python/msvcp140.dll")
        runtime._copy_source(cpp_source, cpp_destination)
        if redist is not None:
            _apply_redist(redist, staging)
        if recipient_terms is not None:
            (staging / "MSVC-RECIPIENT-TERMS.txt").write_bytes(recipient_terms)
        if public_key is not None:
            (staging / "vault_v2/release_key.pub").write_bytes(public_key)
        if update_channel is not None:
            runtime._json_write(staging / "vault_v2/update-channel.json", {
                "schema": "vault-v2-update-channel@1", "url": update_channel,
            })
        if android is not None:
            _copy_android(android, staging)
        if notices is not None:
            notice_root = staging / "third-party-notices"
            notice_root.mkdir()
            (notice_root / "inputs.json").write_bytes(notices[0])
            for name, raw in notices[1].items():
                (notice_root / name).write_bytes(raw)
        runtime._json_write(staging / "vault-install.json", {
            "schema": "vault-v2-install@1", "worker_python": "python/python.exe", "program_root": "../..",
            "version": version, "version_code": version_code, "api_version": api_version,
        })
        runtime._json_write(staging / "payload-metadata.json", {
            "schema": "vault-v2-payload@1", "version": version, "version_code": version_code,
            "api_version": api_version,
            "updates_status": ("disabled_no_public_key" if public_key is None else
                               "disabled_no_channel" if update_channel is None else "configured"),
            "android_package_status": "verified_release_bytes" if android else "absent", "release_status": "unqualified",
            "supplemental_notices_status": "included_not_release_cleared" if notices else "absent",
        })
        runtime._json_write(staging / "runtime-assets.json", manifest)
        runtime._json_write(staging / "payload-source-files.json", catalog)
        (staging / "PAYLOAD-NOTICES.txt").write_text(NOTICE, encoding="utf-8", newline="\n")
        inventory = []
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                digest, size = runtime._file_digest(path)
                inventory.append({"path": path.relative_to(staging).as_posix(), "size": size, "sha256": digest})
        runtime._json_write(staging / "windows-payload-files.json", {"schema": "vault-v2-payload-files@1", "files": inventory})
        runtime._plain_path(output)
        if output.exists():
            raise BuildError("Output appeared during build; publication refused")
        staging.rename(output)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--output", type=Path, required=True, help="new payload directory outside the source checkout")
    parser.add_argument("--cache", type=Path, required=True, help="explicit verified asset cache outside source/output")
    parser.add_argument("--version", required=True)
    parser.add_argument("--version-code", type=int, required=True)
    parser.add_argument("--api-version", type=int, required=True)
    parser.add_argument("--manifest", type=Path, help="explicit dependency manifest override; normally use the checked-in pins")
    parser.add_argument("--qt-profile", type=Path, help="explicit wheel-pinned Qt subset; omitted keeps the full wheel")
    parser.add_argument("--notice-bundle", type=Path, help="explicit hash-pinned supplemental notice manifest; not license clearance")
    parser.add_argument("--public-key", type=Path, help="explicit P-256 public key; missing means updates disabled")
    parser.add_argument("--update-channel", help="explicit HTTPS directory URL; missing means updates disabled")
    parser.add_argument("--apk", type=Path, help="optional explicit APK, requires both sidecars and public key")
    parser.add_argument("--apk-manifest", type=Path)
    parser.add_argument("--apk-signature", type=Path)
    parser.add_argument("--redist-profile", type=Path, help="explicit hash-pinned Microsoft x64 runtime inputs")
    parser.add_argument("--recipient-terms", type=Path, help="explicit Microsoft component agreement text, not legal clearance")
    args = parser.parse_args(argv)
    try:
        output = build_payload(args.source, args.output, args.cache, version=args.version,
                               version_code=args.version_code, api_version=args.api_version,
                               manifest_path=args.manifest, public_key_path=args.public_key,
                               apk_path=args.apk, apk_manifest_path=args.apk_manifest,
                               apk_signature_path=args.apk_signature, update_channel=args.update_channel,
                               qt_profile_path=args.qt_profile, notice_bundle_path=args.notice_bundle,
                               redist_profile_path=args.redist_profile,
                               recipient_terms_path=args.recipient_terms)
    except (BuildError, OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"Windows payload build refused or failed: {exc}", file=sys.stderr)
        return 1
    print(f"Windows payload built: {output}")
    print("Release status: unqualified. No installation or application launch performed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
