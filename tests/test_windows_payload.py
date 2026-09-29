"""Full payload seam: tiny synthetic archives, never execute their binaries."""
import hashlib
import base64
import json
import os
from pathlib import Path
import subprocess
import zipfile
import urllib.request

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

import pytest

from tools.build_windows_payload import BuildError, build_payload


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("Payload fixture tests must not download anything")
    monkeypatch.setattr(urllib.request, "urlopen", refuse)


def fixture_payload(tmp_path):
    source, cache, output = (tmp_path / p for p in ("source", "cache", "output"))
    allowlist = json.loads((Path(__file__).resolve().parents[1] / "tools" / "windows-payload-files.json").read_text())
    for relative in allowlist["files"]:
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic source/static asset\n")
    (source / "vault_v2/api.py").write_text("API_VERSION = 2\n", encoding="utf-8")
    cache.mkdir()
    with zipfile.ZipFile(cache / "python.zip", "w") as archive:
        archive.writestr("python.exe", b"fake executable never run")
        archive.writestr("pythonw.exe", b"fake GUI executable never run")
        archive.writestr("python314.zip", b"fake standard library")
        archive.writestr("python314._pth", b"python314.zip\n.\n")
        archive.writestr("LICENSE.txt", b"fixture Python license")
        archive.writestr("vcruntime140.dll", b"fixture embedded C runtime")
        archive.writestr("vcruntime140_1.dll", b"fixture embedded C runtime supplement")
    with zipfile.ZipFile(cache / "dependency.whl", "w") as archive:
        archive.writestr("dependency/__init__.py", b"VALUE = 5\n")
        archive.writestr("PySide6/msvcp140.dll", b"fixture pinned C++ runtime")
        archive.writestr("dependency-1.dist-info/licenses/LICENSE", b"fixture dependency notice")
    for name in ("eng.traineddata", "rus.traineddata", "LICENSE"):
        (cache / name).write_bytes(b"fixture model/license bytes")
    kinds = {"python.zip": "embedded", "dependency.whl": "wheel", "eng.traineddata": "model", "rus.traineddata": "model", "LICENSE": "model-license"}
    assets = [{"filename": name, "kind": kind, "url": "https://example.invalid/" + name,
               "size": (cache / name).stat().st_size,
               "sha256": hashlib.sha256((cache / name).read_bytes()).hexdigest()}
              for name, kind in kinds.items()]
    manifest = source / "assets.json"
    manifest.write_text(json.dumps({"schema": "vault-v2-runtime-assets@1", "assets": assets}))
    return source, cache, output, manifest


def build_fixture(source, cache, output, manifest, **kwargs):
    return build_payload(source, output, cache, version="1.0 test", version_code=7,
                         api_version=2, manifest_path=manifest, **kwargs)


def redist_fixture(tmp_path):
    root = tmp_path / "explicit-redist"
    root.mkdir()
    names = ("msvcp140", "msvcp140_1", "msvcp140_2", "msvcp140_codecvt_ids",
             "vcruntime140", "vcruntime140_1", "concrt140", "vcamp140", "vccorlib140", "vcomp140")
    entries = []
    for stem in names:
        name = stem + ".dll"
        data = b"explicit synthetic redistributable: " + name.encode("ascii")
        (root / name).write_bytes(data)
        entries.append({"name": name, "source": name, "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest()})
    profile = root / "profile.json"
    profile.write_text(json.dumps({"schema": "vault-msvc-redist@1", "version": "14.51.36247.0", "files": entries}))
    return profile


def test_explicit_recipient_terms_are_copied_exactly_and_inventoried(tmp_path):
    source, cache, output, manifest = fixture_payload(tmp_path)
    terms = tmp_path / "terms.txt"
    raw = b"Synthetic Microsoft-only component terms.\r\n"
    terms.write_bytes(raw)
    build_fixture(source, cache, output, manifest, recipient_terms_path=terms)
    assert (output / "MSVC-RECIPIENT-TERMS.txt").read_bytes() == raw
    inventory = json.loads((output / "windows-payload-files.json").read_text())
    entry = next(row for row in inventory["files"] if row["path"] == "MSVC-RECIPIENT-TERMS.txt")
    assert entry["size"] == len(raw)
    assert entry["sha256"] == hashlib.sha256(raw).hexdigest()


def test_explicit_redist_replaces_worker_and_qt_bytes_with_inventoried_provenance(tmp_path):
    source, cache, output, manifest = fixture_payload(tmp_path)
    profile = redist_fixture(tmp_path)
    (profile.parent / "unlisted-secret.txt").write_bytes(b"DO NOT COPY")
    build_fixture(source, cache, output, manifest, redist_profile_path=profile)
    for path in ("python/vcruntime140.dll", "python/vcruntime140_1.dll",
                 "python/msvcp140.dll", "vendor/PySide6/msvcp140.dll"):
        assert (output / path).read_bytes() == b"explicit synthetic redistributable: " + Path(path).name.encode("ascii")
    report = json.loads((output / "runtime-redist.json").read_text())
    assert report["profile"] == json.loads(profile.read_text())
    assert set(report["replaced_paths"]) == {"python/vcruntime140.dll", "python/vcruntime140_1.dll",
                                          "python/msvcp140.dll", "vendor/PySide6/msvcp140.dll"}
    assert not any(p.name == "unlisted-secret.txt" for p in output.rglob("*"))
    inventory = json.loads((output / "windows-payload-files.json").read_text())
    row = next(r for r in inventory["files"] if r["path"] == "python/msvcp140.dll")
    assert row["sha256"] == hashlib.sha256(b"explicit synthetic redistributable: msvcp140.dll").hexdigest()
    assert json.loads((output / "payload-metadata.json").read_text())["release_status"] == "unqualified"
    assert "runtime-redist.json" in (output / "PAYLOAD-NOTICES.txt").read_text()


def test_redist_duplicate_json_key_refuses_before_download_or_output(tmp_path):
    source, cache, _, manifest = fixture_payload(tmp_path)
    output = tmp_path / "never-created" / "output"
    profile = redist_fixture(tmp_path)
    profile.write_text(profile.read_text().replace('"version":', '"version": "0.0.0.0", "version":', 1))
    (cache / "python.zip").unlink()
    with pytest.raises(BuildError, match="[Rr]edist"):
        build_fixture(source, cache, output, manifest, redist_profile_path=profile)
    assert not output.parent.exists()


@pytest.mark.parametrize("fault", ["digest", "short", "long", "missing", "traversal", "absolute",
                                  "duplicate-name", "bool-size", "extra-field", "missing-file", "wrong-name"])
def test_invalid_explicit_redist_refuses_without_output_or_network(tmp_path, fault):
    source, cache, _, manifest = fixture_payload(tmp_path)
    output = tmp_path / "never-created" / "output"
    profile = redist_fixture(tmp_path)
    record = json.loads(profile.read_text())
    entry = record["files"][0]
    if fault == "digest":
        entry["sha256"] = "0" * 64
    elif fault == "short":
        entry["size"] -= 1
    elif fault == "long":
        entry["size"] += 1
    elif fault == "missing":
        (profile.parent / entry["source"]).unlink()
    elif fault == "traversal":
        entry["source"] = "../" + entry["source"]
    elif fault == "absolute":
        entry["source"] = "C:/Windows/System32/" + entry["source"]
    elif fault == "duplicate-name":
        record["files"][-1] = dict(entry)
    elif fault == "bool-size":
        entry["size"] = True
    elif fault == "extra-field":
        record["license_accepted"] = True
    elif fault == "missing-file":
        record["files"].pop()
    elif fault == "wrong-name":
        entry["name"] = "private.dll"
    profile.write_text(json.dumps(record))
    (cache / "python.zip").unlink()
    with pytest.raises(BuildError, match="[Rr]edist"):
        build_fixture(source, cache, output, manifest, redist_profile_path=profile)
    assert not output.parent.exists()


def test_full_payload_contains_declared_program_static_assets_not_private_siblings(tmp_path):
    source, cache, output, manifest = fixture_payload(tmp_path)
    for relative in ("vault_v2/release_key.pub", "vault_v2/update-channel.json", "vault_v2/secret_experiment.py", "vault_v2/web/vault.apk",
                     "data/private.pdf", ".chat/history.json", ".venv/secret.txt", "tools/synthetic_ocr_files.py"):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"NEVER INCLUDE AUTOMATICALLY")
    assert build_fixture(source, cache, output, manifest) == output
    assert (output / "vault_v2/launcher.py").is_file()
    assert (output / "vault_v2/update_dialog.py").is_file()
    assert (output / "vault_v2/update_service.py").is_file()
    assert (output / "vault_v2/web/index.html").is_file()
    assert (output / "vault_v2/web/icon-192.png").is_file()
    assert (output / "vault_v2/web/icon-512.png").is_file()
    assert (output / "vault_v2/web/manifest.webmanifest").is_file()
    assert not any(b"NEVER INCLUDE AUTOMATICALLY" in p.read_bytes() for p in output.rglob("*") if p.is_file())
    assert not (output / "tools").exists()
    assert json.loads((output / "vault-install.json").read_text()) == {
        "schema": "vault-v2-install@1", "worker_python": "python/python.exe", "program_root": "../..",
        "version": "1.0 test", "version_code": 7, "api_version": 2,
    }
    metadata = json.loads((output / "payload-metadata.json").read_text())
    assert metadata["updates_status"] == "disabled_no_public_key"
    assert metadata["android_package_status"] == "absent"
    assert metadata["release_status"] == "unqualified"


def test_explicit_notice_bundle_preserves_exact_verified_bytes_and_stays_unqualified(tmp_path):
    source, cache, output, manifest = fixture_payload(tmp_path)
    notice_root = tmp_path / "notices"
    notice_root.mkdir()
    raw = b"Public synthetic license\r\nAll terms retained.\r\n"
    (notice_root / "LIBRARY-LICENSE.txt").write_bytes(raw)
    notice_manifest = notice_root / "inputs.json"
    notice_manifest.write_text(json.dumps({"schema": "vault-notice-inputs@1", "files": [
        {"name": "LIBRARY-LICENSE.txt", "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()},
    ]}))
    (notice_root / "private-sibling.txt").write_bytes(b"not explicitly listed")
    build_fixture(source, cache, output, manifest, notice_bundle_path=notice_manifest)
    assert (output / "third-party-notices/LIBRARY-LICENSE.txt").read_bytes() == raw
    assert not (output / "third-party-notices/private-sibling.txt").exists()
    assert (output / "third-party-notices/inputs.json").read_bytes() == notice_manifest.read_bytes()
    inventory = json.loads((output / "windows-payload-files.json").read_text())
    assert "third-party-notices/LIBRARY-LICENSE.txt" in {entry["path"] for entry in inventory["files"]}
    metadata = json.loads((output / "payload-metadata.json").read_text())
    assert metadata["supplemental_notices_status"] == "included_not_release_cleared"
    assert metadata["release_status"] == "unqualified"


@pytest.mark.parametrize("fault", ["digest", "short", "long", "missing", "traversal", "reserved", "duplicate-name", "duplicate-key", "claim-complete", "bool-size", "empty"])
def test_invalid_notice_bundle_refuses_before_download_or_output(tmp_path, fault):
    source, cache, _, manifest = fixture_payload(tmp_path)
    output = tmp_path / "not-created" / "output"
    root = tmp_path / "notices"
    root.mkdir()
    data = b"public synthetic terms"
    (root / "LICENSE.txt").write_bytes(data)
    entry = {"name": "LICENSE.txt", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    record = {"schema": "vault-notice-inputs@1", "files": [entry]}
    if fault == "digest":
        entry["sha256"] = "0" * 64
    elif fault in {"short", "long"}:
        entry["size"] += -1 if fault == "short" else 1
    elif fault == "missing":
        entry["name"] = "MISSING.txt"
    elif fault == "traversal":
        entry["name"] = "../LICENSE.txt"
    elif fault == "reserved":
        entry["name"] = "NUL.txt"
    elif fault == "duplicate-name":
        record["files"].append({**entry, "name": "license.txt"})
    elif fault == "claim-complete":
        record["complete"] = True
    elif fault == "bool-size":
        entry["size"] = True
    elif fault == "empty":
        record["files"] = []
    raw = json.dumps(record)
    if fault == "duplicate-key":
        raw = raw[:-1] + ', "files": []}'
    notice_manifest = root / "inputs.json"
    notice_manifest.write_text(raw)
    (cache / "python.zip").unlink()
    with pytest.raises(BuildError, match="[Nn]otice"):
        build_fixture(source, cache, output, manifest, notice_bundle_path=notice_manifest)
    assert not output.parent.exists()


def test_inventory_covers_exact_payload_and_preserves_dependency_notices(tmp_path):
    source, cache, output, manifest = fixture_payload(tmp_path)
    build_fixture(source, cache, output, manifest)
    inventory = json.loads((output / "windows-payload-files.json").read_text())
    listed = {entry["path"] for entry in inventory["files"]}
    actual = {p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()}
    assert listed == actual - {"windows-payload-files.json"}
    assert {"vault-install.json", "payload-metadata.json", "payload-source-files.json", "runtime-assets.json",
            "PAYLOAD-NOTICES.txt", "README.md", "vault_v2/web/index.html", "python/LICENSE.txt",
            "vendor/dependency-1.dist-info/licenses/LICENSE", "vault_v2/ocr_models/LICENSE"} <= listed
    assert (output / "python/LICENSE.txt").read_bytes() == b"fixture Python license"
    assert (output / "vendor/dependency-1.dist-info/licenses/LICENSE").read_bytes() == b"fixture dependency notice"
    for entry in inventory["files"]:
        raw = (output / entry["path"]).read_bytes()
        assert entry["size"] == len(raw)
        assert entry["sha256"] == hashlib.sha256(raw).hexdigest()
    assert (output / "python/python314._pth").read_text() == "python314.zip\n.\n../vendor\n..\n"


def test_payload_delivers_original_project_license_without_replacing_dependency_terms(tmp_path):
    source, cache, output, manifest = fixture_payload(tmp_path)
    project_terms = b"Synthetic project license\r\nRetain this notice.\r\n"
    (source / "LICENSE").write_bytes(project_terms)
    build_fixture(source, cache, output, manifest)
    assert (output / "LICENSE").read_bytes() == project_terms
    inventory = json.loads((output / "windows-payload-files.json").read_text())
    assert "LICENSE" in {entry["path"] for entry in inventory["files"]}
    assert (output / "python/LICENSE.txt").read_bytes() == b"fixture Python license"
    assert (output / "vendor/dependency-1.dist-info/licenses/LICENSE").read_bytes() == b"fixture dependency notice"


def test_workers_get_verified_app_local_cpp_runtime_without_loading_qt(tmp_path):
    source, cache, output, manifest = fixture_payload(tmp_path)
    build_fixture(source, cache, output, manifest)
    runtime = output / "python/msvcp140.dll"
    assert runtime.is_file()
    assert runtime.read_bytes() == (output / "vendor/PySide6/msvcp140.dll").read_bytes()
    assert runtime.read_bytes() == b"fixture pinned C++ runtime"
    inventory = json.loads((output / "windows-payload-files.json").read_text())
    entry = next(item for item in inventory["files"] if item["path"] == "python/msvcp140.dll")
    assert entry["sha256"] == hashlib.sha256(runtime.read_bytes()).hexdigest()
    assert "python/msvcp140.dll" in (output / "PAYLOAD-NOTICES.txt").read_text()


@pytest.mark.parametrize("relative", ["README.md", "vault_v2/main.py", "vault_v2/launcher.py", "vault_v2/web/icon-512.png",
                                      "vault_v2/update_dialog.py", "vault_v2/update_service.py"])
def test_missing_required_input_refuses_before_creating_output_parent(tmp_path, relative):
    source, cache, _, manifest = fixture_payload(tmp_path)
    output = tmp_path / "not-created" / "output"
    (source / relative).unlink()
    with pytest.raises(BuildError, match="Required source"):
        build_fixture(source, cache, output, manifest)
    assert not output.parent.exists()


@pytest.mark.parametrize("choice", ["existing", "inside-source", "inside-cache", "cache-inside-output", "cache-inside-source"])
def test_unsafe_output_or_cache_is_refused_without_changing_existing_bytes(tmp_path, choice):
    source, cache, output, manifest = fixture_payload(tmp_path)
    if choice == "existing":
        output.mkdir()
        (output / "retain.txt").write_bytes(b"keep this")
    elif choice == "inside-source":
        output = source / "output"
    elif choice == "inside-cache":
        output = cache / "output"
    elif choice == "cache-inside-output":
        cache = output / "cache"
    else:
        cache = source / "cache"
    with pytest.raises(BuildError):
        build_fixture(source, cache, output, manifest)
    if choice == "existing":
        assert list(output.iterdir()) == [output / "retain.txt"]
        assert (output / "retain.txt").read_bytes() == b"keep this"
    else:
        assert not output.exists()


@pytest.mark.parametrize("field,value", [("version", ""), ("version", "x" * 65), ("version", "версия"),
                                        ("version", "line\n"), ("version_code", True), ("version_code", 0),
                                        ("version_code", 2147483648), ("api_version", False), ("api_version", -1)])
def test_invalid_installed_identity_refuses_before_any_build(tmp_path, field, value):
    source, cache, output, manifest = fixture_payload(tmp_path)
    args = {"version": "1.0", "version_code": 7, "api_version": 2, field: value}
    with pytest.raises(BuildError, match="identity"):
        build_payload(source, output, cache, manifest_path=manifest, **args)
    assert not output.exists()


def public_fixture(curve=None):
    private = ec.generate_private_key(curve or ec.SECP256R1())
    public = private.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return private, base64.b64encode(public)


def test_only_explicit_valid_public_key_is_bundled_and_identified(tmp_path):
    source, cache, output, manifest = fixture_payload(tmp_path)
    _, public = public_fixture()
    public_path = tmp_path / "explicit-public.pub"
    public_path.write_bytes(public)
    build_fixture(source, cache, output, manifest, public_key_path=public_path)
    assert (output / "vault_v2/release_key.pub").read_bytes() == public
    assert json.loads((output / "payload-metadata.json").read_text())["updates_status"] == "disabled_no_channel"


@pytest.mark.parametrize("with_key", [False, True])
def test_only_explicit_channel_is_bundled_with_honest_update_status(tmp_path, with_key):
    source, cache, output, manifest = fixture_payload(tmp_path)
    inputs = {"update_channel": "https://example.invalid/releases/windows/"}
    if with_key:
        _, public = public_fixture()
        key = tmp_path / "explicit-key.pub"
        key.write_bytes(public)
        inputs["public_key_path"] = key
    build_fixture(source, cache, output, manifest, **inputs)
    assert json.loads((output / "vault_v2/update-channel.json").read_text()) == {
        "schema": "vault-v2-update-channel@1", "url": inputs["update_channel"],
    }
    metadata = json.loads((output / "payload-metadata.json").read_text())
    assert metadata["updates_status"] == ("configured" if with_key else "disabled_no_public_key")
    assert any(entry["path"] == "vault_v2/update-channel.json"
               for entry in json.loads((output / "windows-payload-files.json").read_text())["files"])


@pytest.mark.parametrize("channel", [
    "", "http://example.invalid/releases", "file:///releases", "https:///releases",
    "https://user:password@example.invalid/releases", "https://example.invalid/releases?token=x",
    "https://example.invalid/releases#fragment", "https://example.invalid/../releases",
    "https://example.invalid/%2e%2e/releases", "https://example.invalid/with space",
    "https://example.invalid\\releases", "https://example.invalid:0/releases",
    "https://example.invalid:99999/releases", "https://example.invalid/\nreleases", 7,
])
def test_invalid_channel_refuses_before_download_or_output_creation(tmp_path, channel):
    source, cache, _, manifest = fixture_payload(tmp_path)
    output = tmp_path / "never-created" / "payload"
    (cache / "python.zip").unlink()
    with pytest.raises(BuildError, match="update channel"):
        build_fixture(source, cache, output, manifest, update_channel=channel)
    assert not output.parent.exists()


@pytest.mark.parametrize("kind", ["invalid", "oversized", "private-pem", "wrong-curve", "missing"])
def test_invalid_explicit_key_is_refused_not_silently_omitted(tmp_path, kind):
    source, cache, output, manifest = fixture_payload(tmp_path)
    public_path = tmp_path / "explicit-public.pub"
    raw = {"invalid": b"no", "oversized": b"x" * 1025, "private-pem": b"-----BEGIN PRIVATE KEY-----\nfixture"}.get(kind)
    if kind == "wrong-curve":
        _, raw = public_fixture(ec.SECP384R1())
    if kind != "missing":
        public_path.write_bytes(raw)
    with pytest.raises(BuildError, match="public key"):
        build_fixture(source, cache, output, manifest, public_key_path=public_path)
    assert not output.exists()


def apk_fixture(tmp_path):
    private, public = public_fixture()
    apk, key, manifest, signature = (tmp_path / p for p in ("chosen.apk", "chosen.pub", "android.json", "android.sig"))
    apk.write_bytes(b"PK synthetic APK, never installed")
    key.write_bytes(public)
    description = {"schema": "vault-v2-release@1", "platform": "android", "version": "2.0",
                   "version_code": 20, "file": "vault.apk", "size": apk.stat().st_size,
                   "sha256": hashlib.sha256(apk.read_bytes()).hexdigest(), "min_api_version": 2}
    raw = json.dumps(description).encode()
    manifest.write_bytes(raw)
    signature.write_bytes(base64.b64encode(private.sign(raw, ec.ECDSA(hashes.SHA256()))))
    return {"public_key_path": key, "apk_path": apk, "apk_manifest_path": manifest, "apk_signature_path": signature}


def test_explicit_signed_android_package_includes_exact_verified_sidecars(tmp_path):
    source, cache, output, manifest = fixture_payload(tmp_path)
    inputs = apk_fixture(tmp_path)
    build_fixture(source, cache, output, manifest, **inputs)
    for argument, relative in (("apk_path", "vault.apk"), ("apk_manifest_path", "vault-release.json"), ("apk_signature_path", "vault-release.json.sig")):
        assert (output / "vault_v2/web" / relative).read_bytes() == inputs[argument].read_bytes()
    assert json.loads((output / "payload-metadata.json").read_text())["android_package_status"] == "verified_release_bytes"


@pytest.mark.parametrize("fault", ["no-key", "no-manifest", "no-signature", "no-apk", "changed-apk", "changed-signature", "changed-manifest"])
def test_android_inputs_are_all_or_nothing_and_integrity_checked(tmp_path, fault):
    source, cache, output, manifest = fixture_payload(tmp_path)
    inputs = apk_fixture(tmp_path)
    missing = {"no-key": "public_key_path", "no-manifest": "apk_manifest_path", "no-signature": "apk_signature_path", "no-apk": "apk_path"}
    changed = {"changed-apk": "apk_path", "changed-signature": "apk_signature_path", "changed-manifest": "apk_manifest_path"}
    if fault in missing:
        inputs.pop(missing[fault])
    else:
        inputs[changed[fault]].write_bytes(b"changed")
    with pytest.raises(BuildError, match="Android"):
        build_fixture(source, cache, output, manifest, **inputs)
    assert not output.exists()


def change_archive(cache, manifest, name, members):
    with zipfile.ZipFile(cache / name, "w") as archive:
        for member, raw in members.items():
            archive.writestr(member, raw)
    records = json.loads(manifest.read_text())
    entry = next(item for item in records["assets"] if item["filename"] == name)
    entry["size"] = (cache / name).stat().st_size
    entry["sha256"] = hashlib.sha256((cache / name).read_bytes()).hexdigest()
    manifest.write_text(json.dumps(records))


def test_explicit_qt_subset_keeps_selected_runtime_and_all_notices(tmp_path):
    source, cache, output, manifest = fixture_payload(tmp_path)
    change_archive(cache, manifest, "dependency.whl", {
        "PySide6/msvcp140.dll": b"fixture pinned C++ runtime",
        "PySide6/QtCore.pyd": b"required core",
        "PySide6/QtUnused.dll": b"unused module",
        "PySide6/module/LICENSE.txt": b"native attribution",
        "dependency-1.dist-info/licenses/LICENSE": b"wheel attribution",
        "dependency-1.dist-info/RECORD": b"PySide6/QtUnused.dll,original-wheel-hash,13\r\n",
        "dependency-1.dist-info/METADATA": b"Metadata-Version: 2.4\r\nName: dependency\r\n",
    })
    profile = source / "qt-profile.json"
    profile.write_text(json.dumps({
        "schema": "vault-qt-subset@1", "wheel": "dependency.whl",
        "sha256": hashlib.sha256((cache / "dependency.whl").read_bytes()).hexdigest(),
        "members": ["PySide6/msvcp140.dll", "PySide6/QtCore.pyd"],
    }))
    build_fixture(source, cache, output, manifest, qt_profile_path=profile)
    assert (output / "vendor/PySide6/QtCore.pyd").read_bytes() == b"required core"
    assert not (output / "vendor/PySide6/QtUnused.dll").exists()
    assert (output / "vendor/PySide6/module/LICENSE.txt").read_bytes() == b"native attribution"
    assert (output / "vendor/dependency-1.dist-info/licenses/LICENSE").read_bytes() == b"wheel attribution"
    assert (output / "vendor/dependency-1.dist-info/RECORD").read_bytes() == b"PySide6/QtUnused.dll,original-wheel-hash,13\r\n"
    assert (output / "vendor/dependency-1.dist-info/METADATA").read_bytes() == b"Metadata-Version: 2.4\r\nName: dependency\r\n"
    record = json.loads((output / "qt-subset.json").read_text())
    assert record["omitted_members"] == ["PySide6/QtUnused.dll"]
    assert record["release_qualified"] is False


@pytest.mark.parametrize("fault", ["wrong-wheel", "wrong-hash", "missing", "escape", "duplicate", "empty", "extra-key", "duplicate-json-key"])
def test_bad_qt_profile_never_publishes_output(tmp_path, fault):
    source, cache, output, manifest = fixture_payload(tmp_path)
    record = {"schema": "vault-qt-subset@1", "wheel": "dependency.whl",
              "sha256": hashlib.sha256((cache / "dependency.whl").read_bytes()).hexdigest(),
              "members": ["PySide6/msvcp140.dll"]}
    if fault == "wrong-wheel":
        record["wheel"] = "unbound.whl"
    elif fault == "wrong-hash":
        record["sha256"] = "0" * 64
    elif fault == "missing":
        record["members"].append("PySide6/missing.dll")
    elif fault == "escape":
        record["members"].append("PySide6/../outside.dll")
    elif fault == "duplicate":
        record["members"].append("PySide6/MSVCP140.dll")
    elif fault == "empty":
        record["members"] = []
    elif fault == "extra-key":
        record["unrecognised"] = True
    profile = source / "qt-profile.json"
    raw = json.dumps(record)
    if fault == "duplicate-json-key":
        raw = raw[:-1] + ', "members": ["PySide6/msvcp140.dll"]}'
    profile.write_text(raw)
    with pytest.raises(BuildError, match="Qt profile"):
        build_fixture(source, cache, output, manifest, qt_profile_path=profile)
    assert not output.exists()
    assert not list(output.parent.glob(".vault-payload-*"))


@pytest.mark.parametrize("other_first", [False, True])
def test_qt_subset_cross_wheel_collision_refuses_without_publishing(tmp_path, other_first):
    source, cache, output, manifest = fixture_payload(tmp_path)
    wheel = cache / "other.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("PySide6/msvcp140.dll", b"conflicting runtime")
    records = json.loads(manifest.read_text())
    other = {"filename": wheel.name, "kind": "wheel",
             "url": "https://example.invalid/other.whl", "size": wheel.stat().st_size,
             "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}
    records["assets"].insert(0 if other_first else len(records["assets"]), other)
    manifest.write_text(json.dumps(records))
    profile = source / "qt-profile.json"
    profile.write_text(json.dumps({
        "schema": "vault-qt-subset@1", "wheel": "dependency.whl",
        "sha256": hashlib.sha256((cache / "dependency.whl").read_bytes()).hexdigest(),
        "members": ["PySide6/msvcp140.dll"],
    }))
    with pytest.raises(BuildError, match="collision"):
        build_fixture(source, cache, output, manifest, qt_profile_path=profile)
    assert not output.exists()
    assert not list(output.parent.glob(".vault-payload-*"))
    assert not (output.parent / "qt-profile-source").exists()


@pytest.mark.parametrize("missing", ["python.exe", "pythonw.exe", "LICENSE.txt", "vcruntime140.dll", "vcruntime140_1.dll"])
def test_incomplete_runtime_cannot_be_published_as_a_full_payload(tmp_path, missing):
    source, cache, output, manifest = fixture_payload(tmp_path)
    with zipfile.ZipFile(cache / "python.zip") as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    members.pop(missing)
    change_archive(cache, manifest, "python.zip", members)
    with pytest.raises(BuildError, match="runtime"):
        build_fixture(source, cache, output, manifest)
    assert not output.exists()


@pytest.mark.parametrize("kind", ["missing", "empty", "collision"])
def test_unusable_cpp_runtime_refuses_publication(tmp_path, kind):
    source, cache, output, manifest = fixture_payload(tmp_path)
    archive_name = "python.zip" if kind == "collision" else "dependency.whl"
    with zipfile.ZipFile(cache / archive_name) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    if kind == "missing":
        members.pop("PySide6/msvcp140.dll")
    elif kind == "empty":
        members["PySide6/msvcp140.dll"] = b""
    else:
        members["msvcp140.dll"] = b"must not overwrite this runtime"
    change_archive(cache, manifest, archive_name, members)
    before = (cache / archive_name).read_bytes()
    with pytest.raises(BuildError, match="runtime"):
        build_fixture(source, cache, output, manifest)
    assert not output.exists()
    assert not list(output.parent.glob(".vault-payload-*"))
    assert (cache / archive_name).read_bytes() == before


def test_altered_cached_dependency_is_refused(tmp_path):
    source, cache, output, manifest = fixture_payload(tmp_path)
    (cache / "dependency.whl").write_bytes(b"tampered")
    with pytest.raises(BuildError, match="SHA256"):
        build_fixture(source, cache, output, manifest)
    assert not output.exists()


@pytest.mark.parametrize("member", ["../outside.txt", "C:/outside.txt", "NUL.txt"])
def test_archive_escape_is_rejected_before_publication(tmp_path, member):
    source, cache, output, manifest = fixture_payload(tmp_path)
    change_archive(cache, manifest, "dependency.whl", {member: b"unsafe"})
    with pytest.raises(BuildError, match="Unsafe ZIP"):
        build_fixture(source, cache, output, manifest)
    assert not output.exists()
    assert not (tmp_path / "outside.txt").exists()


def test_two_dependencies_cannot_overwrite_each_other(tmp_path):
    source, cache, output, manifest = fixture_payload(tmp_path)
    extra = cache / "extra.whl"
    extra.write_bytes((cache / "dependency.whl").read_bytes())
    description = json.loads(manifest.read_text())
    description["assets"].append({"kind": "wheel", "filename": "extra.whl", "url": "https://example.invalid/extra.whl",
                                  "size": extra.stat().st_size, "sha256": hashlib.sha256(extra.read_bytes()).hexdigest()})
    manifest.write_text(json.dumps(description))
    with pytest.raises(BuildError, match="collision"):
        build_fixture(source, cache, output, manifest)
    assert not output.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction boundary")
@pytest.mark.parametrize("role", ["required-source", "output-parent", "cache"])
def test_windows_junction_is_refused_at_each_build_boundary(tmp_path, role):
    source, cache, output, manifest = fixture_payload(tmp_path)
    if role == "required-source":
        target = tmp_path / "separate-web"
        (source / "vault_v2/web").rename(target)
        link = source / "vault_v2/web"
    elif role == "output-parent":
        target = tmp_path / "separate-output"
        target.mkdir()
        link = tmp_path / "linked-output"
        output = link / "payload"
    else:
        target = cache
        link = tmp_path / "linked-cache"
        cache = link
    # Only a junction inside this test-owned root; never follow/delete a live link.
    command = "New-Item -ItemType Junction -Path '" + str(link).replace("'", "''") + "' -Target '" + str(target).replace("'", "''") + "' -ErrorAction Stop | Out-Null"
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command], check=True, capture_output=True, timeout=15)
    before = {p.relative_to(target).as_posix(): p.read_bytes() for p in target.rglob("*") if p.is_file()}
    with pytest.raises(BuildError, match="Linked paths"):
        build_fixture(source, cache, output, manifest)
    assert not output.exists()
    assert {p.relative_to(target).as_posix(): p.read_bytes() for p in target.rglob("*") if p.is_file()} == before


def test_cli_builds_only_explicit_payload_inputs(tmp_path, capsys):
    from tools.build_windows_payload import main
    source, cache, output, manifest = fixture_payload(tmp_path)
    assert main(["--source", str(source), "--output", str(output), "--cache", str(cache),
                 "--version", "0.0.0-qualification", "--version-code", "26092601", "--api-version", "2",
                 "--manifest", str(manifest)]) == 0
    assert json.loads((output / "vault-install.json").read_text())["version_code"] == 26092601
    assert "unqualified" in capsys.readouterr().out
    assert main(["--source", str(source), "--output", str(output), "--cache", str(cache),
                 "--version", "0.0.0-qualification", "--version-code", "26092601", "--api-version", "2",
                 "--manifest", str(manifest)]) == 1
    assert "refused" in capsys.readouterr().err


def test_cli_accepts_only_an_explicit_valid_channel(tmp_path, capsys):
    from tools.build_windows_payload import main
    source, cache, output, manifest = fixture_payload(tmp_path)
    args = ["--source", str(source), "--output", str(output), "--cache", str(cache),
            "--version", "test", "--version-code", "7", "--api-version", "2", "--manifest", str(manifest),
            "--update-channel"]
    assert main(args + ["http://example.invalid/releases"]) == 1
    assert not output.exists()
    assert "update channel" in capsys.readouterr().err
    assert main(args + ["https://example.invalid/releases"]) == 0
    assert json.loads((output / "vault_v2/update-channel.json").read_text())["url"] == "https://example.invalid/releases"


@pytest.mark.parametrize("source_api", [
    "API_VERSION = 1\n",
    "OTHER_VERSION = 2\n",
    "API_VERSION = int('2')\n",
    "API_VERSION = 2\nAPI_VERSION = 2\n",
    "API_VERSION = True\n",
    "API_VERSION = 2\nAPI_VERSION += 1\n",
    "if True:\n    API_VERSION = 2\n",
    "not valid Python !\n",
])
def test_declared_api_must_match_single_literal_source_assignment_before_build(tmp_path, source_api):
    source, cache, _, manifest = fixture_payload(tmp_path)
    (source / "vault_v2/api.py").write_text(source_api, encoding="utf-8")
    output = tmp_path / "never-created" / "payload"
    # Any attempted download is also refused by the network-boundary fixture.
    (cache / "python.zip").unlink()
    with pytest.raises(BuildError, match="API_VERSION"):
        build_fixture(source, cache, output, manifest)
    assert not output.parent.exists()


@pytest.mark.parametrize("source_api", ["API_VERSION = 2\n", "API_VERSION: int = 2\n"])
def test_source_api_is_parsed_without_executing_application(tmp_path, source_api):
    source, cache, output, manifest = fixture_payload(tmp_path)
    (source / "vault_v2/api.py").write_text("raise RuntimeError('must not import')\n" + source_api,
                                           encoding="utf-8")
    build_fixture(source, cache, output, manifest)
    assert json.loads((output / "vault-install.json").read_text())["api_version"] == 2
