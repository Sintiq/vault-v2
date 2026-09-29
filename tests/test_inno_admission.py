"""Compile-input contract only; no native admission objects are created here."""
import pytest

from tools.build_runtime_0p import BuildError
from tools.qualify_inno_preflight import RECIPE


def test_admission_probe_keeps_pascal_and_isolates_four_native_oracles():
    from tools.qualify_inno_admission import make_probe

    raw = RECIPE.read_bytes()
    probe = make_probe(raw, "b" * 32)
    source = raw.decode().replace("\r\n", "\n")
    functions = source[source.index("function AbsentMarker"):source.index("function PlainAncestors")]
    assert functions in probe
    assert "Global\\VaultV2-SyntheticAdmissionProbe-" + "b" * 32 in probe
    assert "RecordAdmission('absent-allows', True)" in probe
    assert "RecordAdmission('runtime-mutex-refuses', False)" in probe
    assert "RecordAdmission('event-collision-refuses', False)" in probe
    assert "RecordAdmission('released-handles-allows', True)" in probe
    assert "external 'CreateEventW@kernel32.dll stdcall'" in probe
    assert "if CollisionError <> 6 then RaiseException" in probe
    assert "same-user only; not cross-user" in probe
    assert "finally ReleaseAdmission; end" in probe
    for forbidden in ("Global\\VaultV2-InstallAdmission-v1", "Programs\\VaultV2",
                      "[Files]", "[Icons]", "[Registry]", "[Run]", "Exec(",
                      "ShellExec", "RegWrite", "RegDelete", "DelTree"):
        assert forbidden not in probe


def test_admission_probe_refuses_changed_production_recipe():
    from tools.qualify_inno_admission import make_probe

    with pytest.raises(BuildError, match="changed"):
        make_probe(RECIPE.read_bytes() + b"\n; changed", "b" * 32)


@pytest.mark.parametrize("build_id", ["../escape", "b" * 31, "G" * 32, "b'" * 16])
def test_admission_probe_refuses_namespace_injection(build_id):
    from tools.qualify_inno_admission import make_probe

    with pytest.raises(BuildError, match="build id"):
        make_probe(RECIPE.read_bytes(), build_id)
