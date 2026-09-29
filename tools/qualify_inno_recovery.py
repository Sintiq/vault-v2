"""Compile-only synthetic Inno missing-source failure after first write and retry.

Native outcomes are NOT RUN until observed in a disposable guest. The fault hook,
two tiny files, initial baseline, paths and identities are synthetic. This is not
the Q18 EXE, a process-crash test, or an uninstaller qualification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid

from tools.build_runtime_0p import BuildError, _file_digest, _plain_path
from tools.build_windows_installer import COMPILER_HASHES
from tools.qualify_inno_preflight import FIXED_RECIPE_SHA256, RECIPE, make_probe as preflight_probe

FIRST = b"SYNTHETIC RECOVERY VERSION 200 FIRST\r\n"
SECOND = b"SYNTHETIC RECOVERY VERSION 200 SECOND\r\n"
BASELINE = b"SYNTHETIC RECOVERY BASELINE 100\r\n"
CANARY = b"SYNTHETIC RECOVERY EXTERNAL CANARY\r\n"


def make_probe(raw: bytes, build_id: str) -> str:
    if hashlib.sha256(raw).hexdigest() != FIXED_RECIPE_SHA256:
        raise BuildError("Production recipe changed; review recovery extraction first")
    preflight_probe(raw, build_id)  # Reviewed recipe and synthetic ID validation seam.
    source = raw.decode("utf-8").replace("\r\n", "\n")
    first, last = "[Code]\n", "function InitializeUninstall: Boolean;"
    if source.count(first) != 1 or source.count(last) != 1:
        raise BuildError("Production recovery extraction boundary drift")
    code = source.split(first)[1]
    replacements = {
        "Admission = 'Global\\VaultV2-InstallAdmission-v1';":
            f"Admission = 'Global\\VaultV2-SyntheticRecoveryProbe-{build_id}';",
        "RegistryPath = 'Software\\VaultV2\\Installer';":
            f"RegistryPath = 'Software\\VaultV2-SyntheticRecoveryProbe\\{build_id}';",
        "var InstallerHandle: THandle;":
            "var InstallerHandle: THandle;\n RunRoot, ReportPath: String;\n"
            " FailureMode, ReportReady, FirstWriteObserved, SuccessfulInstall: Boolean;\n",
        "begin Result := ExpandConstant('{localappdata}\\Programs\\VaultV2'); end;":
            "begin Result := ExpandConstant('{localappdata}\\VaultV2-SyntheticRecoveryProbe\\"
            + build_id + "\\program'); end;",
        "function InitializeSetup: Boolean;": "function ProductionInitializeSetup: Boolean;",
        "procedure DeinitializeSetup;": "procedure ProductionDeinitializeSetup;",
    }
    for old, new in replacements.items():
        if code.count(old) != 1:
            raise BuildError("Production recovery substitution drift")
        code = code.replace(old, new)
    root = "{localappdata}\\VaultV2-SyntheticRecoveryProbe\\" + build_id
    header = f"""; GUEST-ONLY SYNTHETIC INSTALL PHASE; generated EXE is never run by builder.
; Production recipe SHA256: {FIXED_RECIPE_SHA256}
#define VersionCode 200
[Setup]
AppId=VaultV2-SyntheticRecoveryProbe-{build_id}
AppName=Vault V2 Synthetic Recovery Probe
AppVersion=200
DefaultDirName={root}\\program
UsePreviousAppDir=no
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=no
RestartApplications=no
RestartIfNeededByRun=no
UninstallDisplayName=Vault V2 Synthetic Recovery Probe {build_id}
UninstallFilesDir={{app}}\\uninstall
AppMutex=Global\\VaultV2-SyntheticRecoveryProbe-{build_id}.runtime
Compression=lzma2/fast
SolidCompression=yes
SetupLogging=yes
OutputBaseFilename=recovery-probe
[Files]
Source: "first.txt"; DestDir: "{{app}}\\versions\\200"; Flags: ignoreversion; AfterInstall: CaptureFirstWrite
Source: "{root}\\missing-source.txt"; DestDir: "{{app}}\\versions\\200"; ExternalSize: 1; Flags: external ignoreversion; Check: InjectMissingSource
Source: "second.txt"; DestDir: "{{app}}\\versions\\200"; Flags: ignoreversion
[Registry]
Root: HKCU; Subkey: "Software\\VaultV2-SyntheticRecoveryProbe\\{build_id}"; ValueType: dword; ValueName: "VersionCode"; ValueData: "200"; Flags: uninsdeletevalue
"""
    constants = (
        "const\n ProbeId = '" + build_id + "';\n"
        + " FirstHash = '" + hashlib.sha256(FIRST).hexdigest() + "';\n"
        + " SecondHash = '" + hashlib.sha256(SECOND).hexdigest() + "';\n"
        + " BaselineHash = '" + hashlib.sha256(BASELINE).hexdigest() + "';\n"
        + " CanaryHash = '" + hashlib.sha256(CANARY).hexdigest() + "';\n"
    )
    result = header + "[Code]\n" + constants + code + HARNESS
    for forbidden in ("Global\\VaultV2-InstallAdmission-v1", "Programs\\VaultV2", "Software\\VaultV2\\",
                      "[Run]", "[Icons]", "Exec(", "ShellExec", "DelTree", "RegDelete",
                      "vault_v2.launcher"):
        if forbidden in result:
            raise BuildError("Unexpected live installation authority in recovery probe")
    return result


HARNESS = r"""
procedure RecordLine(Value: String);
begin
 if not SaveStringToFile(ReportPath, Value + #13#10, True) then
  RaiseException('Cannot retain recovery report');
end;
procedure SaveNew(Path, Value: String);
begin
 if FileExists(Path) or DirExists(Path) then RaiseException('Evidence path already exists');
 if not SaveStringToFile(Path, Value, False) then RaiseException('Cannot retain fixture/evidence');
end;
function FileState(Path: String): String;
begin
 if FileExists(Path) then Result := GetSHA256OfFile(Path)
 else Result := 'ABSENT';
end;
function Snapshot: String;
var Version: Cardinal; RegistryState: String;
begin
 if RegQueryDWordValue(HKCU, RegistryPath, 'VersionCode', Version) then
  RegistryState := IntToStr(Version)
 else RegistryState := 'ABSENT-OR-UNREADABLE';
 Result := 'registry=' + RegistryState + ';baseline100=' +
  FileState(FixedRoot + '\versions\100\baseline.txt') + ';external=' +
  FileState(RunRoot + '\external-canary.txt') + ';target200-dir=' +
  IntToStr(Ord(DirExists(FixedRoot + '\versions\200'))) + ';first=' +
  FileState(FixedRoot + '\versions\200\first.txt') + ';second=' +
  FileState(FixedRoot + '\versions\200\second.txt') + ';uninstall-dir=' +
  IntToStr(Ord(DirExists(FixedRoot + '\uninstall'))) + ';unins000exe=' +
  FileState(FixedRoot + '\uninstall\unins000.exe') + ';unins000dat=' +
  FileState(FixedRoot + '\uninstall\unins000.dat');
end;
procedure RequirePreservedBaseline;
begin
 if FileState(FixedRoot + '\versions\100\baseline.txt') <> BaselineHash then
  RaiseException('Baseline100 changed or missing; no automatic repair');
 if FileState(RunRoot + '\external-canary.txt') <> CanaryHash then
  RaiseException('External canary changed or missing; no automatic repair');
end;
procedure MakeBaseline;
begin
 if DirExists(RunRoot) or FileExists(RunRoot) or RegKeyExists(HKCU, RegistryPath) then
  RaiseException('Failure mode requires a fresh fixture; no reuse or cleanup');
 if not ForceDirectories(FixedRoot + '\versions\100') then
  RaiseException('Cannot create fresh synthetic baseline');
 SaveNew(FixedRoot + '\versions\100\baseline.txt', 'SYNTHETIC RECOVERY BASELINE 100' + #13#10);
 SaveNew(RunRoot + '\external-canary.txt', 'SYNTHETIC RECOVERY EXTERNAL CANARY' + #13#10);
 if not RegWriteDWordValue(HKCU, RegistryPath, 'VersionCode', 100) then
  RaiseException('Cannot create synthetic registry100');
 RequirePreservedBaseline;
end;
procedure CaptureFirstWrite;
var Actual: String;
begin
 Actual := GetSHA256OfFile(FixedRoot + '\versions\200\first.txt');
 if Actual <> FirstHash then RaiseException('First installed file does not match embedded fixture');
 FirstWriteObserved := True;
 RecordLine('AFTER FIRST WRITE path=' + FixedRoot + '\versions\200\first.txt SHA256=' + Actual);
 if FailureMode then begin
  SaveNew(RunRoot + '\first-write.txt', Actual);
  RecordLine('FAULT ARMED: next entry is missing external source; observe native file-error dialog');
 end;
end;
function InjectMissingSource: Boolean;
begin
 Result := FailureMode and FirstWriteObserved;
end;
procedure CurStepChanged(CurStep: TSetupStep);
begin
 if CurStep = ssDone then begin
  SuccessfulInstall := True;
  RecordLine('NATIVE STEP ssDone observed');
 end;
end;
procedure ObserveAtDeinitialize;
var State, Outcome, Name: String; Current: Cardinal;
begin
 State := Snapshot;
 RecordLine('AT DEINITIALIZE ' + State);
 RequirePreservedBaseline;
 if not AbsentMarker(Admission + '.installer') then
  RaiseException('Admission marker still present after production release');
 RecordLine('BASELINE100 AND EXTERNAL CANARY PRESERVED; INSTALLER MARKER ABSENT');
 if FailureMode then begin
  Name := 'failure-at-deinitialize.txt';
  if not FirstWriteObserved then Outcome := 'NO VERIFIED FIRST WRITE; FAILURE TEST NOT QUALIFIED'
  else if SuccessfulInstall then Outcome := 'UNEXPECTED SUCCESS; CONTROLLED FAILURE NOT QUALIFIED'
  else Outcome := 'VERIFIED FIRST WRITE; NO ssDone; inspect native error/rollback GUI and log';
 end else begin
  Name := 'retry-at-deinitialize.txt';
  if SuccessfulInstall and FirstWriteObserved and
     RegQueryDWordValue(HKCU, RegistryPath, 'VersionCode', Current) and (Current = 200) and
     (FileState(FixedRoot + '\versions\200\first.txt') = FirstHash) and
     (FileState(FixedRoot + '\versions\200\second.txt') = SecondHash) then
   Outcome := 'RETRY FINISHED; BOTH EMBEDDED FILE HASHES AND REGISTRY200 MATCH'
  else Outcome := 'RETRY DID NOT COMPLETE; RETAIN STATE FOR MANUAL REVIEW';
 end;
 RecordLine(Outcome);
 SaveNew(RunRoot + '\' + Name, State + #13#10 + Outcome + #13#10);
 if FailureMode and FirstWriteObserved and (not SuccessfulInstall) and
    RegQueryDWordValue(HKCU, RegistryPath, 'VersionCode', Current) and (Current = 100) then
  SaveNew(RunRoot + '\failure-observed.txt', 'VERIFIED-FIRST-WRITE-NO-SSDONE:' + FirstHash);
end;
function InitializeSetup: Boolean;
var Choice: Integer; Previous: Cardinal; WrittenHash, FailureMarker: AnsiString;
begin
 Result := False;
 if WizardSilent then Exit; { Explicit GUI selection only; no unattended retry. }
 RunRoot := ExpandConstant('{localappdata}\VaultV2-SyntheticRecoveryProbe\') + ProbeId;
 Choice := SuppressibleMsgBox('Synthetic recovery only. YES: fresh missing-source failure after first write.' + #13#10 +
  'NO: explicit retry of this same retained fixture, without fault injection.' + #13#10 +
  'CANCEL: no action. No automatic retry or fixture cleanup.', mbConfirmation, MB_YESNOCANCEL, IDCANCEL);
 if Choice = IDCANCEL then Exit;
 try
  if (Choice <> IDYES) and (Choice <> IDNO) then RaiseException('No explicit mode selected');
  FailureMode := Choice = IDYES;
  if not PlainAncestors(RunRoot) then RaiseException('Synthetic root linked or inaccessible');
  if FailureMode then begin
   MakeBaseline;
   ReportPath := RunRoot + '\failure-report.txt';
  end else begin
   if not DirExists(RunRoot) or not PlainTree(RunRoot) then RaiseException('Retry fixture not plain');
   if not FileExists(RunRoot + '\failure-at-deinitialize.txt') or
      not LoadStringFromFile(RunRoot + '\first-write.txt', WrittenHash) or
      (WrittenHash <> FirstHash) then RaiseException('Missing completed failure observation; no retry');
   if not LoadStringFromFile(RunRoot + '\failure-observed.txt', FailureMarker) or
      (FailureMarker <> 'VERIFIED-FIRST-WRITE-NO-SSDONE:' + FirstHash) then
    RaiseException('No verified first-write failure without ssDone; no retry');
   if not RegQueryDWordValue(HKCU, RegistryPath, 'VersionCode', Previous) or (Previous <> 100) then
    RaiseException('Retry requires preserved registry100; no repair');
   RequirePreservedBaseline;
   ReportPath := RunRoot + '\retry-report.txt';
  end;
  SaveNew(ReportPath, 'SYNTHETIC INSTALL PHASE ONLY; not Q18 crash qualification' + #13#10);
  ReportReady := True;
  RecordLine('ROOT ' + RunRoot + '; MODE failure=' + IntToStr(Ord(FailureMode)));
  RecordLine('BEFORE ' + Snapshot);
  RecordLine('No automatic retry or fixture cleanup; native outcome must be observed.');
  Result := ProductionInitializeSetup;
 except
  MsgBox('Recovery probe stopped: ' + GetExceptionMessage, mbError, MB_OK);
 end;
end;
procedure DeinitializeSetup;
begin
 ProductionDeinitializeSetup;
 if ReportReady then begin
  try
   ObserveAtDeinitialize;
  except
   RecordLine('OBSERVER STOPPED: ' + GetExceptionMessage + '; no recovery verdict');
   MsgBox('Recovery observation incomplete: ' + GetExceptionMessage, mbError, MB_OK);
  end;
 end;
end;
"""


def compile_probe(compiler: Path, output_dir: Path) -> Path:
    if os.name != "nt":
        raise BuildError("Windows-only native probe compiler")
    compiler, output_dir = Path(compiler).absolute(), Path(output_dir).absolute()
    for path in (compiler, output_dir, RECIPE):
        _plain_path(path)
        if ".." in path.parts or any(c in str(path) for c in '\r\n"{}'):
            raise BuildError("Unsafe build path")
    if compiler.name != "ISCC.exe" or output_dir.exists():
        raise BuildError("Pinned compiler and a new output directory are required")
    for name, expected in COMPILER_HASHES.items():
        component = compiler.parent / name
        _plain_path(component)
        if not component.is_file() or _file_digest(component)[0] != expected:
            raise BuildError("Portable compiler component pin mismatch")
    raw = RECIPE.read_bytes()
    build_id = uuid.uuid4().hex
    generated = make_probe(raw, build_id)
    output_dir.mkdir(parents=True)
    script = output_dir / "recovery-probe.iss"
    script.write_text(generated, encoding="utf-8-sig")
    for name, data in (("first.txt", FIRST), ("second.txt", SECOND)):
        (output_dir / name).write_bytes(data)
    command = [str(compiler), "--no-ide-signtools", "/Q", f"/O{output_dir}", str(script)]
    try:
        compiled = subprocess.run(command, cwd=output_dir, capture_output=True, timeout=120,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BuildError("Probe compilation failed or timed out; output retained") from error
    (output_dir / "compiler.log").write_bytes(compiled.stdout + compiled.stderr)
    if compiled.returncode:
        raise BuildError("Probe compiler refused source; inspect retained compiler.log")
    executable = output_dir / "recovery-probe.exe"
    if not executable.is_file() or executable.read_bytes()[:2] != b"MZ":
        raise BuildError("Probe executable missing or invalid")
    evidence = {
        "schema": "vault-v2-native-recovery-probe@1", "build_id": build_id,
        "production_recipe_sha256": hashlib.sha256(raw).hexdigest(),
        "generated_recipe_sha256": _file_digest(script)[0],
        "executable_sha256": _file_digest(executable)[0],
        "compiler_components": COMPILER_HASHES, "native_execution": "NOT RUN",
        "rollback_outcome": "NOT RUN", "retry_outcome": "NOT RUN",
        "fixture_root": "%LOCALAPPDATA%/VaultV2-SyntheticRecoveryProbe/" + build_id,
        "registry_root": "HKCU/Software/VaultV2-SyntheticRecoveryProbe/" + build_id,
        "namespace": "Global\\VaultV2-SyntheticRecoveryProbe-" + build_id,
        "synthetic_changes": ["root/HKCU/AppId/Global names and version200", "prepared baseline100",
                              "two embedded canary files and failure-only missing external source",
                              "AfterInstall verifies first write; native file-error requires explicit Abort",
                              "explicit GUI mode and outside-program observation",
                              "InitializeSetup/DeinitializeSetup wrapped with original bodies retained",
                              "production uninstaller settings/callbacks retained with synthetic identity; never invoked",
                              "no icons, application launch or runtime payload"],
        "fixture_sha256": {n: hashlib.sha256(b).hexdigest() for n, b in
                           (("first", FIRST), ("second", SECOND), ("baseline", BASELINE), ("external", CANARY))},
        "command": command,
    }
    (output_dir / "build-evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    return executable


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compiler", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(compile_probe(args.compiler, args.output_dir))
    except (BuildError, OSError) as error:
        parser.exit(2, str(error) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
