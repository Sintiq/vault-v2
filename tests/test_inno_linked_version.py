"""Generation seam only: never execute the native junction probe."""
import pytest

from tools.build_runtime_0p import BuildError
from tools.qualify_inno_preflight import RECIPE


def test_linked_version_probe_retains_predicate_and_has_bounded_native_oracles():
    from tools.qualify_inno_linked_version import make_probe

    raw = RECIPE.read_bytes()
    probe = make_probe(raw, "a" * 32)
    original = raw.decode().replace("\r\n", "\n")
    preflight = original[original.index("function Preflight:"):original.index("function InitializeSetup:")]
    assert preflight.replace("ExpandConstant('{app}')", "ProbeRoot") in probe
    assert "RunLinkedCase('ordinary-retained50', False, True)" in probe
    assert "RunLinkedCase('junction-retained50', True, False)" in probe
    assert "external 'DeviceIoControl@kernel32.dll stdcall'" in probe
    assert "external 'CreateFileW@kernel32.dll stdcall'" in probe
    assert "VerifyJunction(LinkPath, TargetPath)" in probe
    assert "if BeforeState <> AfterState then RaiseException" in probe
    assert "GetSHA256OfFile(TargetPath + '\\canary.txt')" in probe
    assert "Fixtures retained; no cleanup" in probe
    for forbidden in ("[Files]", "[Icons]", "[Registry]", "[Run]", "Global\\",
                      "Software\\VaultV2\\", "Programs\\VaultV2", "Exec(",
                      "ShellExec", "DelTree", "RegDelete", "AcquireAdmission"):
        assert forbidden not in probe


def test_linked_probe_refuses_recipe_drift_before_native_generation():
    from tools.qualify_inno_linked_version import make_probe

    with pytest.raises(BuildError, match="changed"):
        make_probe(RECIPE.read_bytes() + b"\n; changed", "a" * 32)


def test_linked_probe_emits_diagnostic_stages_without_changing_its_oracles():
    from tools.qualify_inno_linked_version import make_probe

    probe = make_probe(RECIPE.read_bytes(), "a" * 32)
    for stage in ("fixture-start", "junction-create", "junction-verified",
                  "registry-write", "before-state", "preflight", "after-state",
                  "hash-current100", "hash-older50", "hash-external-target"):
        assert "'STAGE " + stage + "'" in probe
    assert "RunLinkedCase('ordinary-retained50', False, True)" in probe
    assert "RunLinkedCase('junction-retained50', True, False)" in probe


def test_linked_state_reads_verified_target_without_traversing_junction():
    from tools.qualify_inno_linked_version import make_probe

    probe = make_probe(RECIPE.read_bytes(), "a" * 32)
    assert "function FixtureState(TargetPath: String; Linked: Boolean): String;" in probe
    assert "if Linked then begin\n  VerifyJunction(ProbeRoot + '\\versions\\50', TargetPath);\n  OlderHash := GetSHA256OfFile(TargetPath + '\\canary.txt');" in probe
    assert "end else OlderHash := GetSHA256OfFile(ProbeRoot + '\\versions\\50\\canary.txt');" in probe
    assert "BeforeState := FixtureState(TargetPath, Linked);" in probe
    assert "AfterState := FixtureState(TargetPath, Linked);" in probe
    assert "OLDER50-EVIDENCE verified-junction-target-direct-hash; no traversal" in probe


@pytest.mark.parametrize("build_id", ["../escape", "a" * 31, "G" * 32, "a'" * 16])
def test_linked_probe_inherits_fresh_namespace_id_validation(build_id):
    from tools.qualify_inno_linked_version import make_probe

    with pytest.raises(BuildError, match="build id"):
        make_probe(RECIPE.read_bytes(), build_id)
