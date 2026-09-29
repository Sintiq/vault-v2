"""Bundle builder boundary: synthetic archives only, no runtime execution."""
import hashlib
from io import BytesIO
import json
from pathlib import Path
import stat
import zipfile

import pytest

from tools.build_runtime_0p import BuildError, build_bundle


def fixture_bundle(tmp_path, *, python_tag="312"):
    source, cache, output = (tmp_path / name for name in ("source", "cache", "output"))
    (source / "vault_v2").mkdir(parents=True)
    (source / "vault_v2" / "__init__.py").write_text("VALUE = 7\n", encoding="utf-8")
    (source / "tools").mkdir()
    for name in ("synthetic_ocr_files.py", "synthetic_viewer_files.py",
                 "qualify_runtime_0p.py", "qualify_pdf_process_preview.py"):
        (source / "tools" / name).write_text("# synthetic tool\n", encoding="utf-8")
    cache.mkdir()
    with zipfile.ZipFile(cache / "python.zip", "w") as archive:
        archive.writestr("python.exe", b"synthetic executable, never run")
        archive.writestr(f"python{python_tag}.zip", b"synthetic standard library")
        archive.writestr(f"python{python_tag}._pth", f"python{python_tag}.zip\n.\n#import site\n")
        archive.writestr("LICENSE.txt", "synthetic Python license")
    with zipfile.ZipFile(cache / "dependency.whl", "w") as archive:
        archive.writestr("dependency/__init__.py", "VALUE = 5\n")
        archive.writestr("dependency-1.dist-info/licenses/LICENSE", "synthetic wheel license")
    for name in ("eng.traineddata", "rus.traineddata", "LICENSE"):
        (cache / name).write_bytes(b"synthetic fixture")
    kinds = {"python.zip": "embedded", "dependency.whl": "wheel",
             "eng.traineddata": "model", "rus.traineddata": "model", "LICENSE": "model-license"}
    assets = [{"filename": name, "kind": kind,
               "url": "https://example.invalid/" + name,
               "size": (cache / name).stat().st_size,
               "sha256": hashlib.sha256((cache / name).read_bytes()).hexdigest()}
              for name, kind in kinds.items()]
    manifest = source / "assets.json"
    manifest.write_text(json.dumps({"schema": "vault-v2-runtime-assets@1", "assets": assets}), encoding="utf-8")
    return source, output, cache, manifest


def test_cp314_bundle_uses_its_matching_isolated_import_configuration(tmp_path):
    source, output, cache, manifest = fixture_bundle(tmp_path, python_tag="314")
    build_bundle(source, output, cache, manifest_path=manifest)
    assert (output / "python" / "python314._pth").read_text(encoding="utf-8") == "python314.zip\n.\n../vendor\n..\n"
    assert not (output / "python" / "python312._pth").exists()
    notice = (output / "RUNTIME-0P-NOTICES.txt").read_text(encoding="utf-8")
    assert "3.12.10" not in notice
    assert "not qualified for production or clean machines" in notice


@pytest.mark.parametrize("runtime_members", [
    pytest.param(("python314.zip", "python313.zip", "python314._pth"), id="multiple-stdlib-zips"),
    pytest.param(("python314.zip", "python313._pth"), id="mismatched-pth"),
    pytest.param(("python314.zip", "python314._pth", "python._pth"), id="extra-pth"),
])
def test_ambiguous_embedded_import_configuration_refuses_output_publication(tmp_path, runtime_members):
    source, output, cache, manifest_path = fixture_bundle(tmp_path, python_tag="314")
    archive_path = cache / "python.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("python.exe", b"synthetic executable, never run")
        for member in runtime_members:
            archive.writestr(member, b"synthetic runtime member")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    asset = next(item for item in manifest["assets"] if item["kind"] == "embedded")
    asset.update(size=archive_path.stat().st_size,
                 sha256=hashlib.sha256(archive_path.read_bytes()).hexdigest())
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BuildError, match="exactly one stdlib ZIP and matching"):
        build_bundle(source, output, cache, manifest_path=manifest_path)
    assert not output.exists()


def test_hash_mismatch_refuses_bundle_without_publishing_output(tmp_path):
    source, output, cache, manifest = fixture_bundle(tmp_path)
    (cache / "eng.traineddata").write_bytes(b"tampered! fixture")
    with pytest.raises(BuildError, match="SHA256"):
        build_bundle(source, output, cache, manifest_path=manifest)
    assert not output.exists()


@pytest.mark.parametrize("member,mode", [
    ("../escaped.txt", 0), ("/absolute.txt", 0), ("C:/escape.txt", 0),
    ("package\\escape.txt", 0), ("package/../escape.txt", 0),
    ("package/link", stat.S_IFLNK | 0o777), ("NUL.txt", 0),
])
def test_unsafe_zip_member_is_refused_before_output_publication(tmp_path, member, mode):
    source, output, cache, manifest_path = fixture_bundle(tmp_path)
    archive_path = cache / "dependency.whl"
    with zipfile.ZipFile(archive_path, "w") as archive:
        info = zipfile.ZipInfo(member)
        info.external_attr = mode << 16
        archive.writestr(info, "outside target")
    if "\\" in member:
        # ZipInfo on Windows normalizes separators when creating fixtures.
        archive_path.write_bytes(archive_path.read_bytes().replace(
            member.replace("\\", "/").encode(), member.encode()))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    asset = next(item for item in manifest["assets"] if item["filename"] == "dependency.whl")
    asset.update(size=archive_path.stat().st_size,
                 sha256=hashlib.sha256(archive_path.read_bytes()).hexdigest())
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BuildError, match="Unsafe ZIP"):
        build_bundle(source, output, cache, manifest_path=manifest_path)
    assert not output.exists()
    assert not (tmp_path / "escaped.txt").exists()


def test_bundle_contains_only_app_sources_allowlisted_tools_and_verified_assets(tmp_path):
    source, output, cache, manifest = fixture_bundle(tmp_path)
    (source / "owner-document.txt").write_text("private", encoding="utf-8")
    (source / "vault_v2" / "secret.json").write_text("private", encoding="utf-8")
    (source / "vault_v2" / ".venv").mkdir()
    (source / "vault_v2" / ".venv" / "injected.py").write_text("private", encoding="utf-8")
    (source / "tools" / "install_local_ocr.py").write_text("unexpected setup", encoding="utf-8")
    (source / "vault_v2" / "ocr_models").mkdir()
    (source / "vault_v2" / "ocr_models" / "personal.traineddata").write_text("private", encoding="utf-8")
    build_bundle(source, output, cache, manifest_path=manifest)
    marker = json.loads((output / "vault-install.json").read_text(encoding="utf-8"))
    assert marker["schema"] == "vault-v2-install@1"
    assert marker["worker_python"] == "python/python.exe"
    assert (output / "python" / "python312._pth").read_text(encoding="utf-8") == "python312.zip\n.\n../vendor\n..\n"
    assert (output / "vendor" / "dependency" / "__init__.py").exists()
    assert (output / "vendor" / "dependency-1.dist-info" / "licenses" / "LICENSE").exists()
    assert (output / "vault_v2" / "ocr_models" / "LICENSE").read_bytes() == b"synthetic fixture"
    assert {p.relative_to(output / "vault_v2").as_posix() for p in (output / "vault_v2").rglob("*") if p.is_file()} == {
        "__init__.py", "ocr_models/eng.traineddata", "ocr_models/rus.traineddata", "ocr_models/LICENSE",
    }
    assert {p.name for p in (output / "tools").iterdir()} == {
        "synthetic_ocr_files.py", "synthetic_viewer_files.py", "qualify_runtime_0p.py", "qualify_pdf_process_preview.py",
    }
    assert not (output / "owner-document.txt").exists()


@pytest.mark.parametrize("change", [
    {"filename": "../outside.traineddata"}, {"kind": "arbitrary-file"},
    {"url": "http://example.invalid/asset"}, {"sha256": "not-a-digest"},
])
def test_invalid_asset_manifest_is_refused_before_fetch_or_publication(tmp_path, change):
    source, output, cache, manifest_path = fixture_bundle(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["assets"][0].update(change)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BuildError, match="Invalid asset"):
        build_bundle(source, output, cache, manifest_path=manifest_path)
    assert not output.exists()


@pytest.mark.parametrize("corrupt", [False, True])
def test_explicit_build_fetches_missing_asset_and_verifies_it_before_use(tmp_path, monkeypatch, corrupt):
    source, output, cache, manifest = fixture_bundle(tmp_path)
    (cache / "eng.traineddata").unlink()

    class Response(BytesIO):
        def geturl(self):
            return "https://example.invalid/eng.traineddata"

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs:
                        Response(b"corrupt fixture!" if corrupt else b"synthetic fixture"))
    if corrupt:
        with pytest.raises(BuildError, match="SHA256"):
            build_bundle(source, output, cache, manifest_path=manifest)
        assert not output.exists()
        assert not (cache / "eng.traineddata").exists()
    else:
        build_bundle(source, output, cache, manifest_path=manifest)
        assert (output / "vault_v2" / "ocr_models" / "eng.traineddata").read_bytes() == b"synthetic fixture"


def test_existing_output_is_preserved_without_fetching(tmp_path, monkeypatch):
    source, output, cache, manifest = fixture_bundle(tmp_path)
    output.mkdir()
    owner_file = output / "owner.txt"
    owner_file.write_bytes(b"keep existing bytes")
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: pytest.fail("unexpected fetch"))
    with pytest.raises(BuildError, match="already exists"):
        build_bundle(source, output, cache, manifest_path=manifest)
    assert owner_file.read_bytes() == b"keep existing bytes"


def test_linked_app_source_is_refused_without_copying_external_bytes(tmp_path):
    source, output, cache, manifest = fixture_bundle(tmp_path)
    private = tmp_path / "private.txt"
    private.write_bytes(b"never package these bytes")
    try:
        (source / "vault_v2" / "linked.py").symlink_to(private)
    except OSError:
        pytest.skip("Host does not permit creation of the filesystem link fixture")
    with pytest.raises(BuildError, match="Linked paths"):
        build_bundle(source, output, cache, manifest_path=manifest)
    assert not output.exists()


def test_correct_hash_with_wrong_published_size_is_refused(tmp_path):
    source, output, cache, manifest_path = fixture_bundle(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["assets"][0]["size"] += 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BuildError, match="Size mismatch"):
        build_bundle(source, output, cache, manifest_path=manifest_path)
    assert not output.exists()
