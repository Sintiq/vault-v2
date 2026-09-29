"""Source-generation checks only; never run a generated native probe."""
import pytest

from tools.qualify_inno_preflight import BuildError, RECIPE, make_probe


def test_probe_retains_native_preflight_and_excludes_install_authority():
    raw = RECIPE.read_bytes()
    probe = make_probe(raw, "a" * 32)
    original = raw.decode().replace("\r\n", "\n")
    preflight = original[original.index("function Preflight:"):original.index("function InitializeSetup:")]
    assert preflight.replace("ExpandConstant('{app}')", "ProbeRoot") in probe
    assert "external 'FindFirstFileW@kernel32.dll stdcall'" in probe
    assert "RegQueryDWordValue(HKCU, RegistryPath, 'VersionCode', Previous)" in probe
    assert "RunCase('leftover300', 100, True, '300', True, False)" in probe
    assert "RunCase('older-retained', 100, True, '50', True, True)" in probe
    assert "if BeforeState <> AfterState then RaiseException" in probe
    assert "GetSHA256OfFile(Path)" in probe
    assert "Result := False;\n RunRoot" in probe
    for forbidden in ("[Files]", "[Icons]", "[Registry]", "[Run]", "Global\\", "Software\\VaultV2\\",
                      "Programs\\VaultV2", "DelTree", "RegDelete", "Exec("):
        assert forbidden not in probe


def test_changed_production_recipe_refused_before_generation():
    with pytest.raises(BuildError, match="changed"):
        make_probe(RECIPE.read_bytes() + b"\n; changed", "a" * 32)


@pytest.mark.parametrize("build_id", ["../escape", "a" * 31, "G" * 32, "a'" * 16])
def test_build_id_cannot_change_native_paths(build_id):
    with pytest.raises(BuildError, match="build id"):
        make_probe(RECIPE.read_bytes(), build_id)
