"""Compile, never execute, a guest-only linked-old-version Pascal probe.

Uses the reviewed public preflight-generation seam. No production recipe changes.
Junction layout/API references:
https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/ns-ntifs-_reparse_data_buffer
https://learn.microsoft.com/en-us/windows/win32/api/winioctl/ni-winioctl-fsctl_set_reparse_point
https://learn.microsoft.com/en-us/windows/win32/fileio/reparse-point-operations
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


def make_probe(raw: bytes, build_id: str) -> str:
    if hashlib.sha256(raw).hexdigest() != FIXED_RECIPE_SHA256:
        raise BuildError("Production recipe changed; linked probe requires reviewed fixed recipe")
    original = preflight_probe(raw, build_id)
    boundary = "procedure RecordLine(Value: String);"
    if original.count(boundary) != 1:
        raise BuildError("Preflight generation seam drift")
    prefix = original.split(boundary)[0]
    prefix = prefix.replace("SyntheticPreflightProbe", "SyntheticLinkedVersionProbe")
    prefix = prefix.replace("Synthetic Preflight Probe", "Synthetic Linked Version Probe")
    prefix = prefix.replace("OutputBaseFilename=preflight-probe", "OutputBaseFilename=linked-version-probe")
    insertion = "function WinFileAttributes"
    if prefix.count(insertion) != 1:
        raise BuildError("Native declaration seam drift")
    prefix = prefix.replace(insertion, JUNCTION_DECLARATIONS + insertion)
    result = prefix + HARNESS
    for forbidden in ("Global\\", "Software\\VaultV2\\", "Programs\\VaultV2", "[Files]",
                      "[Icons]", "[Registry]", "[Run]", "Exec(", "ShellExec", "DelTree",
                      "RegDelete", "AcquireAdmission", "InitializeUninstall"):
        if forbidden in result:
            raise BuildError("Unexpected installation or shell authority in linked probe")
    return result


JUNCTION_DECLARATIONS = r"""
type
 { Mount-point REPARSE_DATA_BUFFER: 8-byte header, 8-byte name offsets,
   then WCHAR path bytes. Inno 7.1.0 has contiguous record fields/WideChar=2. }
 TJunctionBuffer = record
  Tag: DWORD;
  DataLength, Reserved: Word;
  SubstituteOffset, SubstituteLength, PrintOffset, PrintLength: Word;
  PathBuffer: array[0..2047] of WideChar;
 end;
function JunctionOpen(Name: String; Access, Share: DWORD; Security: LONG_PTR;
 Creation, Flags: DWORD; Template: THandle): THandle;
 external 'CreateFileW@kernel32.dll stdcall';
function JunctionIo(Handle: THandle; Code: DWORD; var Input: TJunctionBuffer;
 InputSize: DWORD; var Output: TJunctionBuffer; OutputSize: DWORD;
 var Returned: DWORD; Overlapped: LONG_PTR): BOOL;
 external 'DeviceIoControl@kernel32.dll stdcall';
function JunctionClose(Handle: THandle): BOOL;
 external 'CloseHandle@kernel32.dll stdcall';
"""

HARNESS = r"""
procedure RecordLine(Value: String);
begin
 if not SaveStringToFile(ReportPath, Value + #13#10, True) then
  RaiseException('Cannot retain probe report');
end;
procedure MakeDirectory(Path: String);
begin
 if not PlainAncestors(Path) then RaiseException('Fixture ancestor is not plain');
 if DirExists(Path) or FileExists(Path) then RaiseException('Fixture already exists');
 if not ForceDirectories(Path) then RaiseException('Cannot create synthetic fixture');
end;
procedure MakeCanary(Path: String);
begin
 if FileExists(Path) then RaiseException('Canary already exists');
 if not SaveStringToFile(Path, 'SYNTHETIC-LINKED-VERSION-CANARY' + #13#10, False) then
  RaiseException('Cannot create synthetic canary');
end;
procedure CheckJunctionScope(LinkPath, TargetPath: String);
begin
 if (LinkPath <> ProbeRoot + '\versions\50') or
    (TargetPath <> RunRoot + '\junction-target') then
  RaiseException('Junction outside exact synthetic scope');
 if (Length(TargetPath) < 3) or (TargetPath[2] <> ':') or
    (TargetPath[3] <> '\') or (Length(TargetPath) > 512) then
  RaiseException('Junction target requires a short local drive path');
 if not DirExists(TargetPath) or not PlainAncestors(TargetPath) then
  RaiseException('Synthetic target is not a plain directory');
end;
procedure FillJunction(var Data: TJunctionBuffer; TargetPath: String);
var Substitute: String; Index, Offset: Integer;
begin
 Substitute := '\??\' + TargetPath;
 Data.Tag := $A0000003; { IO_REPARSE_TAG_MOUNT_POINT }
 Data.Reserved := 0;
 Data.SubstituteOffset := 0;
 Data.SubstituteLength := Length(Substitute) * 2;
 Data.PrintOffset := (Length(Substitute) + 1) * 2;
 Data.PrintLength := Length(TargetPath) * 2;
 Data.DataLength := 8 + Data.PrintOffset + Data.PrintLength + 2;
 if Data.DataLength > 4104 then RaiseException('Junction buffer exceeded');
 for Index := 0 to 2047 do Data.PathBuffer[Index] := #0;
 for Index := 1 to Length(Substitute) do Data.PathBuffer[Index - 1] := Substitute[Index];
 Offset := Data.PrintOffset div 2;
 for Index := 1 to Length(TargetPath) do Data.PathBuffer[Offset + Index - 1] := TargetPath[Index];
end;
procedure VerifyJunction(LinkPath, TargetPath: String);
var Handle: THandle; Expected, Actual, Unused: TJunctionBuffer;
 Returned, Attributes: DWORD; Index: Integer;
begin
 CheckJunctionScope(LinkPath, TargetPath);
 Attributes := WinFileAttributes(LinkPath);
 if (Attributes = $FFFFFFFF) or ((Attributes and $410) <> $410) then
  RaiseException('Expected native directory reparse point');
 FillJunction(Expected, TargetPath);
 Handle := JunctionOpen(LinkPath, 0, 7, 0, 3, $02200000, 0);
 if NativeInt(Handle) = -1 then RaiseException('Cannot open junction for verification');
 try
  { FSCTL_GET_REPARSE_POINT; no input, fixed 4112-byte output record. }
  if not JunctionIo(Handle, $900A8, Unused, 0, Actual, 4112, Returned, 0) then
   RaiseException('Cannot read native junction data');
  if (Returned <> 8 + Expected.DataLength) or (Actual.Tag <> Expected.Tag) or
     (Actual.DataLength <> Expected.DataLength) or
     (Actual.SubstituteOffset <> Expected.SubstituteOffset) or
     (Actual.SubstituteLength <> Expected.SubstituteLength) or
     (Actual.PrintOffset <> Expected.PrintOffset) or
     (Actual.PrintLength <> Expected.PrintLength) then
   RaiseException('Native junction tag/length/offset mismatch');
  for Index := 0 to ((Expected.DataLength - 8) div 2) - 1 do
   if Actual.PathBuffer[Index] <> Expected.PathBuffer[Index] then
    RaiseException('Native junction target mismatch');
 finally
  if not JunctionClose(Handle) then RaiseException('Cannot close junction verification handle');
 end;
end;
procedure MakeJunction(LinkPath, TargetPath: String);
var Handle: THandle; Data, Unused: TJunctionBuffer; Returned: DWORD;
begin
 CheckJunctionScope(LinkPath, TargetPath);
 MakeDirectory(LinkPath); { Fresh empty fixture; never replace an existing object. }
 FillJunction(Data, TargetPath);
 { GENERIC_WRITE; OPEN_EXISTING; BACKUP_SEMANTICS | OPEN_REPARSE_POINT. }
 Handle := JunctionOpen(LinkPath, $40000000, 0, 0, 3, $02200000, 0);
 if NativeInt(Handle) = -1 then RaiseException('Cannot open fresh fixture for junction creation');
 try
  { FSCTL_SET_REPARSE_POINT; no output. Failure remains a stopped fixture. }
  if not JunctionIo(Handle, $900A4, Data, 8 + Data.DataLength, Unused, 0, Returned, 0) then
   RaiseException('Native junction creation failed; no fallback or cleanup');
 finally
  if not JunctionClose(Handle) then RaiseException('Cannot close junction creation handle');
 end;
 VerifyJunction(LinkPath, TargetPath);
end;
function FixtureState(TargetPath: String; Linked: Boolean): String;
var Current: Cardinal; CurrentHash, OlderHash, ExternalHash: String;
begin
 if not RegQueryDWordValue(HKCU, RegistryPath, 'VersionCode', Current) then
  RaiseException('Cannot read synthetic registry');
 RecordLine('STAGE hash-current100');
 CurrentHash := GetSHA256OfFile(ProbeRoot + '\versions\100\canary.txt');
 RecordLine('STAGE hash-older50');
 if Linked then begin
  VerifyJunction(ProbeRoot + '\versions\50', TargetPath);
  OlderHash := GetSHA256OfFile(TargetPath + '\canary.txt');
 end else OlderHash := GetSHA256OfFile(ProbeRoot + '\versions\50\canary.txt');
 RecordLine('STAGE hash-external-target');
 ExternalHash := GetSHA256OfFile(TargetPath + '\canary.txt');
 Result := 'registry=' + IntToStr(Current) + ';current100=' +
  CurrentHash + ';older50=' + OlderHash + ';external-target=' +
  ExternalHash + ';target200-dir=' +
  IntToStr(Ord(DirExists(ProbeRoot + '\versions\200'))) + ';target200-file=' +
  IntToStr(Ord(FileExists(ProbeRoot + '\versions\200')));
end;
procedure RunLinkedCase(Name: String; Linked, ExpectedAccept: Boolean);
var TargetPath, LinkPath, BeforeState, AfterState, Refusal, Observed: String;
begin
 ProbeRoot := RunRoot + '\' + Name + '\program';
 RegistryPath := 'Software\VaultV2-SyntheticLinkedVersionProbe\' + ProbeId + '\' + Name;
 RecordLine('BEGIN ' + Name);
 RecordLine('STAGE fixture-start');
 if RegKeyExists(HKCU, RegistryPath) then RaiseException('Fixture registry exists');
 MakeDirectory(ProbeRoot + '\versions\100');
 MakeCanary(ProbeRoot + '\versions\100\canary.txt');
 LinkPath := ProbeRoot + '\versions\50';
 TargetPath := RunRoot + '\junction-target';
 if Linked then begin
  RecordLine('STAGE junction-create');
  MakeJunction(LinkPath, TargetPath);
  RecordLine('STAGE junction-verified');
 end
 else begin
  MakeDirectory(LinkPath);
  MakeCanary(LinkPath + '\canary.txt');
 end;
 RecordLine('STAGE registry-write');
 if not RegWriteDWordValue(HKCU, RegistryPath, 'VersionCode', 100) then
  RaiseException('Cannot create synthetic registry fixture');
 RecordLine('STAGE before-state');
 if Linked then
  RecordLine('OLDER50-EVIDENCE verified-junction-target-direct-hash; no traversal');
 BeforeState := FixtureState(TargetPath, Linked);
 RecordLine('CASE ' + Name + ' root=' + ProbeRoot + ' key=HKCU\' + RegistryPath);
 RecordLine('BEFORE ' + BeforeState);
 RecordLine('STAGE preflight');
 Refusal := Preflight;
 RecordLine('STAGE after-state');
 AfterState := FixtureState(TargetPath, Linked);
 if Linked then VerifyJunction(LinkPath, TargetPath);
 if Refusal = '' then Observed := 'ACCEPT' else Observed := 'REFUSE';
 RecordLine('RESULT ' + Observed);
 RecordLine('REFUSAL ' + Refusal);
 RecordLine('AFTER ' + AfterState);
 if BeforeState <> AfterState then RaiseException('Preflight changed fixture state');
 RecordLine('CHECKED-STATE UNCHANGED');
 if (Refusal = '') <> ExpectedAccept then RaiseException('Native result differed from oracle');
end;
function InitializeSetup: Boolean;
begin
 Result := False;
 RunRoot := ExpandConstant('{localappdata}\VaultV2-SyntheticLinkedVersionProbe\') + ProbeId;
 ReportPath := RunRoot + '\report.txt';
 try
  if RegKeyExists(HKCU, 'Software\VaultV2-SyntheticLinkedVersionProbe\' + ProbeId) then
   RaiseException('Probe already ran; preserve fixtures and use a new build');
  MakeDirectory(RunRoot);
  MakeDirectory(RunRoot + '\junction-target');
  MakeCanary(RunRoot + '\junction-target\canary.txt');
  RecordLine('SYNTHETIC native linked-version preflight; no installation');
  RecordLine('Fixtures retained; no cleanup. Not a concurrent-writer sandbox.');
  RunLinkedCase('ordinary-retained50', False, True);
  RunLinkedCase('junction-retained50', True, False);
  RecordLine('COMPLETE: both native oracles matched; not release qualification.');
  MsgBox('Synthetic linked-version probe complete. Fixtures retained at ' + RunRoot, mbInformation, MB_OK);
 except
  MsgBox('Probe stopped: ' + GetExceptionMessage + #13#10 +
   'Partial fixtures retained at ' + RunRoot, mbError, MB_OK);
 end;
 { False: never reach setup wizard or installation. }
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
    script = output_dir / "linked-version-probe.iss"
    script.write_text(generated, encoding="utf-8-sig")
    command = [str(compiler), "--no-ide-signtools", "/Q", f"/O{output_dir}", str(script)]
    try:
        compiled = subprocess.run(command, cwd=output_dir, capture_output=True, timeout=120,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BuildError("Probe compilation failed or timed out; output retained") from error
    (output_dir / "compiler.log").write_bytes(compiled.stdout + compiled.stderr)
    if compiled.returncode:
        raise BuildError("Probe compiler refused source; inspect retained compiler.log")
    executable = output_dir / "linked-version-probe.exe"
    if not executable.is_file() or executable.read_bytes()[:2] != b"MZ":
        raise BuildError("Probe executable missing or invalid")
    evidence = {
        "schema": "vault-v2-native-linked-version-probe@1", "build_id": build_id,
        "production_recipe_sha256": hashlib.sha256(raw).hexdigest(),
        "generated_recipe_sha256": _file_digest(script)[0],
        "executable_sha256": _file_digest(executable)[0],
        "compiler_components": COMPILER_HASHES, "native_execution": "NOT RUN",
        "fixture_root": "%LOCALAPPDATA%/VaultV2-SyntheticLinkedVersionProbe/" + build_id,
        "registry_root": "HKCU/Software/VaultV2-SyntheticLinkedVersionProbe/" + build_id,
        "cases": ["ordinary-retained50: ACCEPT", "junction-retained50: REFUSE"],
        "cleanup": "none; retain fixtures", "command": command,
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
