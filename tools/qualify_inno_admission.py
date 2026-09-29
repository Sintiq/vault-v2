"""Compile-only same-user Pascal admission probe; never run generated software.

Named event/mutex collisions use real Windows objects in a fresh synthetic scope:
https://learn.microsoft.com/en-us/windows/win32/sync/object-names
No live Vault admission name, installer files, registry writes or shell commands.
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
        raise BuildError("Production recipe changed; review admission extraction first")
    preflight_probe(raw, build_id)  # Inherit reviewed recipe/build-id admission.
    source = raw.decode("utf-8").replace("\r\n", "\n")

    def section(first: str, last: str) -> str:
        if source.count(first) != 1 or source.count(last) != 1:
            raise BuildError("Production admission extraction boundary drift")
        start, end = source.index(first), source.index(last)
        if start >= end:
            raise BuildError("Production admission extraction order drift")
        return source[start:end]

    imports = section("var InstallerHandle: THandle;", "function WinFileAttributes")
    filesystem_import = section("function WinFileAttributes", "function WinFindFirst")
    admission = section("function AbsentMarker", "function PlainAncestors")
    ancestors = section("function PlainAncestors", "function FixedRoot")
    header = f"""; SYNTHETIC GUEST-ONLY; native execution not performed by this builder.
; Production recipe SHA256: {FIXED_RECIPE_SHA256}
[Setup]
AppId=VaultV2-SyntheticAdmissionProbe-{build_id}
AppName=Vault V2 Synthetic Admission Probe
AppVersion=1
DefaultDirName={{localappdata}}\\VaultV2-SyntheticAdmissionProbe\\unused
CreateAppDir=no
Uninstallable=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=no
RestartApplications=no
SetupLogging=yes
OutputBaseFilename=admission-probe
[Code]
const
 ProbeId = '{build_id}';
 Admission = 'Global\\VaultV2-SyntheticAdmissionProbe-{build_id}';
var RunRoot, ReportPath: String;
"""
    event_import = """function ProbeCreateEvent(Security: LONG_PTR; ManualReset, InitialState: BOOL;
 Name: String): THandle;
 external 'CreateEventW@kernel32.dll stdcall';
"""
    result = header + imports + filesystem_import + event_import + admission + ancestors + HARNESS
    for forbidden in ("Global\\VaultV2-InstallAdmission-v1", "Programs\\VaultV2", "Software\\VaultV2\\",
                      "[Files]", "[Icons]", "[Registry]", "[Run]", "Exec(", "ShellExec",
                      "RegWrite", "RegDelete", "DelTree", "InitializeUninstall"):
        if forbidden in result:
            raise BuildError("Unexpected product or installation authority in probe")
    if result.count("Global\\") != 1:
        raise BuildError("Admission namespace escaped its single synthetic constant")
    return result


HARNESS = r"""
procedure RecordLine(Value: String);
begin
 if not SaveStringToFile(ReportPath, Value + #13#10, True) then
  RaiseException('Cannot retain admission report');
end;
procedure RequireAbsentObjects;
begin
 if not AbsentMarker(Admission + '.gate') or
    not AbsentMarker(Admission + '.installer') or
    not AbsentMarker(Admission + '.runtime') then
  RaiseException('Synthetic objects already exist or cannot be identified as absent');
end;
procedure CloseFixture(var Handle: THandle);
begin
 if Handle <> 0 then begin
  if not WinCloseHandle(Handle) then RaiseException('Cannot close synthetic fixture handle');
  Handle := 0;
 end;
end;
procedure RecordAdmission(Name: String; Expected: Boolean);
var Observed: Boolean;
begin
 if InstallerHandle <> 0 then RaiseException('Unexpected prior admission handle');
 try
  Observed := AcquireAdmission;
  RecordLine('CASE ' + Name + ' accepted=' + IntToStr(Ord(Observed)) +
   ' expected=' + IntToStr(Ord(Expected)));
  if Observed <> Expected then RaiseException('Admission differed from native oracle');
  if Observed and (InstallerHandle = 0) then RaiseException('Accepted without installer marker');
  if (not Observed) and (InstallerHandle <> 0) then RaiseException('Refused with retained admission');
 finally ReleaseAdmission; end;
 if not AbsentMarker(Admission + '.installer') then
  RaiseException('Installer marker remained after release');
 RecordLine('INSTALLER MARKER RELEASED');
end;
procedure RunCases;
var Fixture, CollisionHandle: THandle; CreationError, CollisionError: DWORD;
begin
 RequireAbsentObjects;
 RecordAdmission('absent-allows', True);
 RequireAbsentObjects;
 Fixture := WinCreateMutex(0, False, Admission + '.runtime');
 CreationError := DLLGetLastError;
 if Fixture = 0 then RaiseException('Cannot create synthetic runtime mutex');
 try
  if CreationError = 183 then RaiseException('Runtime fixture was not fresh');
  if AbsentMarker(Admission + '.runtime') then RaiseException('Runtime marker not observable');
  RecordAdmission('runtime-mutex-refuses', False);
 finally CloseFixture(Fixture); end;
 RequireAbsentObjects;
 Fixture := ProbeCreateEvent(0, False, False, Admission + '.runtime');
 CreationError := DLLGetLastError;
 if Fixture = 0 then RaiseException('Cannot create synthetic event collision');
 try
  if CreationError = 183 then RaiseException('Event fixture was not fresh');
  CollisionHandle := WinOpenMutex($00100000, False, Admission + '.runtime');
  CollisionError := DLLGetLastError;
  if CollisionHandle <> 0 then begin
   CloseFixture(CollisionHandle);
   RaiseException('Event was unexpectedly opened as a mutex');
  end;
  RecordLine('EVENT COLLISION OpenMutexW error=' + IntToStr(CollisionError));
  if CollisionError <> 6 then RaiseException('Expected real ERROR_INVALID_HANDLE collision');
  RecordAdmission('event-collision-refuses', False);
 finally CloseFixture(Fixture); end;
 RequireAbsentObjects;
 RecordAdmission('released-handles-allows', True);
 RequireAbsentObjects;
 RecordLine('ALL SYNTHETIC OBJECTS ABSENT after checked handle release');
end;
function InitializeSetup: Boolean;
begin
 Result := False;
 RunRoot := ExpandConstant('{localappdata}\VaultV2-SyntheticAdmissionProbe\') + ProbeId;
 ReportPath := RunRoot + '\report.txt';
 try
  if not PlainAncestors(RunRoot) then RaiseException('Report root is linked or inaccessible');
  if DirExists(RunRoot) or FileExists(RunRoot) then RaiseException('Probe already ran; do not reuse');
  if not ForceDirectories(RunRoot) then RaiseException('Cannot create fresh report root');
  RecordLine('SYNTHETIC admission; same-user only; not cross-user');
  RecordLine('NAMESPACE ' + Admission);
  RecordLine('No installation; report retained. No live Vault objects used.');
  RunCases;
  RecordLine('COMPLETE: four same-user native oracles matched; not release qualification.');
  MsgBox('Synthetic admission probe complete. Report retained at ' + RunRoot, mbInformation, MB_OK);
 except
  ReleaseAdmission;
  MsgBox('Probe stopped: ' + GetExceptionMessage + #13#10 +
   'Report retained at ' + RunRoot, mbError, MB_OK);
 end;
 { False prevents the wizard and installation phase. }
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
    script = output_dir / "admission-probe.iss"
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
    executable = output_dir / "admission-probe.exe"
    if not executable.is_file() or executable.read_bytes()[:2] != b"MZ":
        raise BuildError("Probe executable missing or invalid")
    evidence = {
        "schema": "vault-v2-native-admission-probe@1", "build_id": build_id,
        "production_recipe_sha256": hashlib.sha256(raw).hexdigest(),
        "generated_recipe_sha256": _file_digest(script)[0],
        "executable_sha256": _file_digest(executable)[0],
        "compiler_components": COMPILER_HASHES, "native_execution": "NOT RUN",
        "scope": "same-user synthetic Windows objects; not cross-user",
        "fixture_root": "%LOCALAPPDATA%/VaultV2-SyntheticAdmissionProbe/" + build_id,
        "namespace": "Global\\VaultV2-SyntheticAdmissionProbe-" + build_id,
        "cases": ["absent: ACCEPT", "runtime mutex: REFUSE", "event collision: REFUSE", "released: ACCEPT"],
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
