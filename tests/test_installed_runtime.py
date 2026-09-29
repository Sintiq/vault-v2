"""Exercise the public runtime seams in a separate, synthetic package layout."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "vault_v2"


@pytest.fixture
def layout(tmp_path):
    bundle = tmp_path / "Программа с пробелами"
    package = bundle / "vault_v2"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    for name in ("runtime.py", "paths.py"):
        shutil.copyfile(SOURCE / name, package / name)
    return bundle


def run_seam(bundle, expression, *, env=None, frozen=False, embedded=False):
    child_env = dict(os.environ)
    child_env.pop("VAULT_V2_ROOT", None)
    child_env.pop("LOCALAPPDATA", None)
    child_env.update(env or {})
    script = (
        "import sys,json; from pathlib import Path; "
        f"sys.path.insert(0, {str(bundle)!r}); "
        + ("sys.frozen = True; " if frozen else "")
        + (f"sys.executable = {str(bundle / 'python/python.exe')!r}; " if embedded else "")
        + "from vault_v2 import runtime,paths; "
        + f"print(json.dumps(str({expression})))"
    )
    return subprocess.run([sys.executable, "-I", "-B", "-c", script],
                          cwd=bundle.parent, env=child_env, capture_output=True,
                          text=True, encoding="utf-8", timeout=10)


def installed(bundle):
    (bundle / "vault-install.json").write_text(json.dumps({
        "schema": "vault-v2-install@1", "worker_python": "python/python.exe",
    }), encoding="utf-8")
    executable = bundle / "python" / "python.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"synthetic placeholder, never executed")
    return executable


def test_development_worker_keeps_real_interpreter(layout):
    result = run_seam(layout, "runtime.worker_python()")
    assert result.returncode == 0, result.stderr
    assert Path(json.loads(result.stdout)) == Path(sys.executable)


def test_installed_worker_is_bundled_interpreter_not_host(layout):
    executable = installed(layout)
    result = run_seam(layout, "runtime.worker_python()")
    assert result.returncode == 0, result.stderr
    assert Path(json.loads(result.stdout)) == executable


def test_source_checkout_has_no_updater_interpreter(layout):
    result = run_seam(layout, "runtime.updater_python()")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == "None"


def test_installed_updater_uses_only_bundled_gui_python(layout):
    executable = installed(layout).with_name("pythonw.exe")
    executable.write_bytes(b"synthetic GUI placeholder, never executed")
    result = run_seam(layout, "runtime.updater_python()")
    assert result.returncode == 0, result.stderr
    assert Path(json.loads(result.stdout)) == executable


def test_missing_gui_python_does_not_fall_back_to_developer_python(layout):
    installed(layout)
    result = run_seam(layout, "runtime.updater_python()")
    assert result.returncode != 0
    assert "GUI Python" in result.stderr


def test_development_root_is_unchanged(layout):
    result = run_seam(layout, "paths.default_root()")
    assert result.returncode == 0, result.stderr
    assert Path(json.loads(result.stdout)) == layout / "data"


def test_installed_root_is_local_appdata_not_program_or_cwd(layout):
    installed(layout)
    appdata = layout.parent / "Local data"
    result = run_seam(layout, "paths.default_root()", env={"LOCALAPPDATA": str(appdata)})
    assert result.returncode == 0, result.stderr
    assert Path(json.loads(result.stdout)) == appdata / "VaultV2" / "data"
    assert not (layout / "data").exists(), "path selection must not create storage"


@pytest.mark.parametrize("is_installed", [False, True])
def test_explicit_external_root_is_preserved(layout, is_installed):
    if is_installed:
        installed(layout)
    chosen = layout.parent / "Chosen synthetic vault"
    result = run_seam(layout, "paths.default_root()", env={"VAULT_V2_ROOT": str(chosen)})
    assert result.returncode == 0, result.stderr
    assert Path(json.loads(result.stdout)) == chosen


def test_missing_embedded_executable_never_falls_back(layout):
    installed(layout).unlink()
    result = run_seam(layout, "runtime.worker_python()")
    assert result.returncode != 0
    assert "bundled Python" in result.stderr


def test_frozen_launcher_without_marker_is_not_a_worker(layout):
    result = run_seam(layout, "runtime.worker_python()", frozen=True)
    assert result.returncode != 0
    assert "install marker" in result.stderr


@pytest.mark.parametrize("expression", ["runtime.worker_python()", "paths.default_root()"])
def test_nonfrozen_embedded_python_without_marker_refuses_development_fallback(layout, expression):
    installed(layout)
    (layout / "vault-install.json").unlink()
    result = run_seam(layout, expression, embedded=True)
    assert result.returncode != 0
    assert "install marker" in result.stderr
    assert not (layout / "data").exists()


@pytest.mark.parametrize("marker", ["{", "[]", '{"schema":"other"}',
    '{"schema":"vault-v2-install@1","worker_python":"C:/elsewhere/python.exe"}',
    '{"schema":"vault-v2-install@1","worker_python":"../python.exe"}', " " * 8192])
@pytest.mark.parametrize("expression", ["runtime.worker_python()", "paths.default_root()"])
def test_invalid_marker_fails_closed(layout, marker, expression):
    (layout / "vault-install.json").write_text(marker, encoding="utf-8")
    result = run_seam(layout, expression)
    assert result.returncode != 0
    assert "install marker" in result.stderr


def test_missing_appdata_does_not_use_program_directory(layout):
    installed(layout)
    result = run_seam(layout, "paths.default_root()")
    assert result.returncode != 0
    assert "LOCALAPPDATA" in result.stderr


@pytest.mark.parametrize("where", ["program", "inside", "ancestor", "relative"])
def test_installed_root_cannot_overlap_program_files(layout, where):
    installed(layout)
    root = {"program": layout, "inside": layout / "data", "ancestor": layout.parent,
            "relative": Path("data")}[where]
    result = run_seam(layout, "paths.default_root()", env={"VAULT_V2_ROOT": str(root)})
    assert result.returncode != 0
    assert "Vault root" in result.stderr


def test_versioned_installation_protects_all_program_versions_from_data_root(layout):
    program = layout.parent / "Programs" / "VaultV2"
    version = program / "versions" / "2"
    shutil.copytree(layout, version)
    installed(version)
    marker = {"schema": "vault-v2-install@1", "worker_python": "python/python.exe",
              "program_root": "../..", "version_code": 2}
    (version / "vault-install.json").write_text(json.dumps(marker), encoding="utf-8")
    result = run_seam(version, "paths.default_root()", env={"VAULT_V2_ROOT": str(program / "data")})
    assert result.returncode != 0
    assert "Vault root" in result.stderr
    allowed = run_seam(version, "paths.default_root()", env={"VAULT_V2_ROOT": str(layout.parent / "Archive")})
    assert allowed.returncode == 0, allowed.stderr


def test_installed_launcher_explicit_root_cannot_bypass_program_separation(layout):
    installed(layout)
    for name in ("launcher.py", "installation.py"):
        shutil.copyfile(SOURCE / name, layout / "vault_v2" / name)
    expr = "__import__('vault_v2.launcher',fromlist=['prepare_arguments']).prepare_arguments(['Vault'," + repr(str(layout / "data")) + "])"
    result = run_seam(layout, expr)
    assert result.returncode != 0
    assert "program files" in result.stderr


@pytest.mark.parametrize("include_root", [True, False])
@pytest.mark.parametrize("entry", ["environment", "launcher"])
def test_versioned_marker_cannot_shrink_program_boundary_to_one_version(layout, include_root, entry):
    program = layout.parent / "Programs" / "VaultV2"
    version = program / "versions" / "2"
    shutil.copytree(layout, version)
    installed(version)
    marker = {"schema": "vault-v2-install@1", "worker_python": "python/python.exe",
              "version_code": 2}
    if include_root:
        marker["program_root"] = "."
    (version / "vault-install.json").write_text(json.dumps(marker), encoding="utf-8")
    if entry == "environment":
        result = run_seam(version, "paths.default_root()",
                          env={"VAULT_V2_ROOT": str(program / "data")})
    else:
        for name in ("launcher.py", "installation.py"):
            shutil.copyfile(SOURCE / name, version / "vault_v2" / name)
        expression = ("__import__('vault_v2.launcher',fromlist=['prepare_arguments'])"
                      ".prepare_arguments(['Vault'," + repr(str(program / "data")) + "])")
        result = run_seam(version, expression)
    assert result.returncode != 0, "versioned install accepted an archive inside its program tree"
    assert "install marker" in result.stderr
    assert not (program / "data").exists()
