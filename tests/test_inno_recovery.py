"""Recovery probe generation contract, never execute its install phase."""
from tools.qualify_inno_preflight import RECIPE


def test_recovery_probe_preserves_predicates_and_requires_explicit_retry():
    from tools.qualify_inno_recovery import make_probe

    raw = RECIPE.read_bytes()
    text = make_probe(raw, "c" * 32)
    source = raw.decode().replace("\r\n", "\n")
    admission = source[source.index("function AbsentMarker"):source.index("function PlainAncestors")]
    preflight = source[source.index("function Preflight:"):source.index("function InitializeSetup:")]
    assert admission in text
    assert preflight in text
    assert "AfterInstall: CaptureFirstWrite" in text
    assert "GetSHA256OfFile(FixedRoot + '\\versions\\200\\first.txt')" in text
    assert "FAULT ARMED: next entry is missing external source" in text
    assert "WizardSilent" in text and "MB_YESNOCANCEL, IDCANCEL" in text
    assert "ProductionDeinitializeSetup;" in text
    assert "ObserveAtDeinitialize;" in text
    assert "No automatic retry or fixture cleanup" in text
    assert "UninstallFilesDir={app}\\uninstall" in text
    assert "Uninstallable=no" not in text
    assert "CreateUninstallRegKey=no" not in text
    assert "Global\\VaultV2-SyntheticRecoveryProbe-" + "c" * 32 in text
    for forbidden in ("Global\\VaultV2-InstallAdmission-v1", "Programs\\VaultV2",
                      "Software\\VaultV2\\", "[Run]", "[Icons]", "Exec(", "ShellExec",
                      "DelTree", "RegDelete", "vault_v2.launcher"):
        assert forbidden not in text
    initialize = source[source.index("function InitializeSetup:"):source.index("function PrepareToInstall")]
    prepare = source[source.index("function PrepareToInstall"):source.index("procedure DeinitializeSetup;")]
    deinitialize = source[source.index("procedure DeinitializeSetup;"):source.index("function InitializeUninstall")]
    assert initialize.replace("function InitializeSetup:", "function ProductionInitializeSetup:") in text
    assert prepare in text
    assert deinitialize.replace("procedure DeinitializeSetup;", "procedure ProductionDeinitializeSetup;") in text
    assert source[source.index("function InitializeUninstall"):] in text


def test_retry_requires_failure_observation_marker_and_baseline_registry100():
    from tools.qualify_inno_recovery import make_probe

    text = make_probe(RECIPE.read_bytes(), "c" * 32)
    assert "VERIFIED-FIRST-WRITE-NO-SSDONE:" in text
    assert "RunRoot + '\\failure-observed.txt'" in text
    assert "FailureMarker <> 'VERIFIED-FIRST-WRITE-NO-SSDONE:' + FirstHash" in text
    assert "(Previous <> 100)" in text


def test_recovery_injects_native_missing_source_between_embedded_files_only_in_failure_mode():
    from tools.qualify_inno_recovery import make_probe

    text = make_probe(RECIPE.read_bytes(), "c" * 32)
    files = text.split("[Files]\n", 1)[1].split("[Registry]", 1)[0]
    entries = [line for line in files.splitlines() if line.startswith("Source:")]
    assert len(entries) == 3
    assert 'Source: "first.txt"' in entries[0]
    assert 'Source: "second.txt"' in entries[2]
    assert "missing-source.txt" in entries[1]
    assert "ExternalSize: 1" in entries[1]
    assert "Flags: external ignoreversion" in entries[1]
    assert "Check: InjectMissingSource" in entries[1]
    assert "Result := FailureMode and FirstWriteObserved" in text
    assert "RaiseException('CONTROLLED FAILURE AFTER VERIFIED FIRST WRITE')" not in text
    assert "skipifsourcedoesntexist" not in text
    assert "DelTree" not in text and "DeleteFile(" not in text
