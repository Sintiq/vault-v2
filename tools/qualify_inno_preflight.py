"""Compile (never run) a synthetic native probe of a pinned Inno preflight.

The generated EXE is guest-only test software. Its InitializeSetup creates and
retains synthetic fixtures, calls extracted production Pascal, then returns False.
It never reaches installation. See docs/inno-preflight-probe.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid

from tools.build_windows_installer import COMPILER_HASHES
from tools.build_runtime_0p import BuildError, _file_digest, _plain_path


OLD_RECIPE_SHA256 = "9a9dc630002a0a103e13aeaed2480e6b7eea57db7438ac7bc5d2b34e678b3c99"
# Agreement-only change reviewed; extracted predicates/admission are unchanged.
# Native agreement-page/decline/silent checks remain a separate qualification.
FIXED_RECIPE_SHA256 = "9015c548f80b7f19e69fc2eb9aea580f371f1ec6b27b80b699e4259f9eb0dfb4"
RECIPE = Path(__file__).resolve().parent / "windows/vault-v2.iss"


def _between(text: str, first: str, last: str) -> str:
    if text.count(first) != 1 or text.count(last) != 1:
        raise BuildError("Production extraction boundary drift")
    start, end = text.index(first), text.index(last)
    if start >= end:
        raise BuildError("Production extraction order drift")
    return text[start:end]


def make_probe(raw: bytes, build_id: str) -> str:
    recipe_digest = hashlib.sha256(raw).hexdigest()
    if recipe_digest not in (OLD_RECIPE_SHA256, FIXED_RECIPE_SHA256):
        raise BuildError("Production recipe changed; review and repin extraction first")
    if len(build_id) != 32 or any(c not in "0123456789abcdef" for c in build_id):
        raise BuildError("Invalid synthetic build id")
    source = raw.decode("utf-8").replace("\r\n", "\n")
    declarations = _between(source, " MaxTreeDepth = 64;", "var InstallerHandle: THandle;")
    imports = _between(source, "function WinFileAttributes", "function AbsentMarker")
    functions = _between(source, "function PlainAncestors", "function InitializeSetup")
    replacements = {
        "begin Result := ExpandConstant('{localappdata}\\Programs\\VaultV2'); end;":
            "begin Result := ProbeRoot; end;",
        "ExpandConstant('{app}')": "ProbeRoot",
    }
    for old, new in replacements.items():
        if functions.count(old) != 1:
            raise BuildError("Production substitution drift")
        functions = functions.replace(old, new)
    # Numeric preprocessor constant supplied here, not a change to Pascal logic.
    header = f"""; SYNTHETIC GUEST-ONLY PROBE; never run on the working host.
; Production recipe SHA256: {recipe_digest}
#define VersionCode 200
[Setup]
AppId=VaultV2-SyntheticPreflightProbe-{build_id}
AppName=Vault V2 Synthetic Preflight Probe
AppVersion=1
DefaultDirName={{localappdata}}\\VaultV2-SyntheticPreflightProbe\\unused
CreateAppDir=no
Uninstallable=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=no
RestartApplications=no
SetupLogging=yes
OutputBaseFilename=preflight-probe
[Code]
const
 ProbeId = '{build_id}';
 FixedRecipe = {'True' if recipe_digest == FIXED_RECIPE_SHA256 else 'False'};
"""
    result = (header + declarations
              + "var ProbeRoot, RegistryPath, RunRoot, ReportPath: String;\n"
              + imports + functions + HARNESS)
    for forbidden in ("Global\\", "Software\\VaultV2\\", "Programs\\VaultV2", "[Files]",
                      "[Icons]", "[Registry]", "[Run]", "AcquireAdmission", "InitializeUninstall"):
        if forbidden in result:
            raise BuildError("Unexpected production installation authority in probe")
    return result


HARNESS = r"""
procedure RecordLine(Value: String);
begin
 if not SaveStringToFile(ReportPath, Value + #13#10, True) then
  RaiseException('Cannot retain probe report');
end;
procedure MakeDirectory(Path: String);
begin
 if DirExists(Path) or FileExists(Path) then RaiseException('Fixture already exists');
 if not ForceDirectories(Path) then RaiseException('Cannot create synthetic fixture');
end;
procedure MakeCanary(Path: String);
begin
 if FileExists(Path) then RaiseException('Canary already exists');
 if not SaveStringToFile(Path, 'SYNTHETIC-PREFLIGHT-CANARY' + #13#10, False) then
  RaiseException('Cannot create canary');
end;
procedure MakeVersion(Name: String);
begin
 MakeDirectory(ProbeRoot + '\versions\' + Name);
 MakeCanary(ProbeRoot + '\versions\' + Name + '\canary.txt');
end;
function CanaryState(Path: String): String;
begin
 if FileExists(Path) then Result := GetSHA256OfFile(Path)
 else Result := 'ABSENT';
end;
function FixtureState(Extra: String): String;
var Current: Cardinal;
begin
 if not RegQueryDWordValue(HKCU, RegistryPath, 'VersionCode', Current) then
  RaiseException('Cannot read synthetic registry');
 Result := 'registry=' + IntToStr(Current) + ';current100=' +
  CanaryState(ProbeRoot + '\versions\100\canary.txt') + ';target200=' +
  CanaryState(ProbeRoot + '\versions\200\canary.txt') + ';external=' +
  CanaryState(RunRoot + '\external-canary.txt');
 if Extra <> '' then Result := Result + ';extra=' + Extra + ':' +
  CanaryState(ProbeRoot + '\versions\' + Extra + '\canary.txt');
 Result := Result + ';target-dir=' + IntToStr(Ord(DirExists(ProbeRoot + '\versions\200')));
end;
procedure RunCase(Name: String; Previous: Cardinal; HasCurrent: Boolean;
 Extra: String; LegacyAccept, SafeAccept: Boolean);
var BeforeState, AfterState, Refusal, Observed, Legacy, Safe: String;
begin
 ProbeRoot := RunRoot + '\' + Name + '\program';
 RegistryPath := 'Software\VaultV2-SyntheticPreflightProbe\' + ProbeId + '\' + Name;
 if RegKeyExists(HKCU, RegistryPath) then RaiseException('Synthetic registry already exists');
 MakeDirectory(ProbeRoot + '\versions');
 if HasCurrent then MakeVersion('100');
 if Extra <> '' then MakeVersion(Extra);
 if not RegWriteDWordValue(HKCU, RegistryPath, 'VersionCode', Previous) then
  RaiseException('Cannot create synthetic registry fixture');
 BeforeState := FixtureState(Extra);
 RecordLine('CASE ' + Name + ' root=' + ProbeRoot + ' key=HKCU\' + RegistryPath);
 RecordLine('BEFORE ' + BeforeState);
 Refusal := Preflight;
 AfterState := FixtureState(Extra);
 if Refusal = '' then Observed := 'ACCEPT' else Observed := 'REFUSE';
 if LegacyAccept then Legacy := 'ACCEPT' else Legacy := 'REFUSE';
 if SafeAccept then Safe := 'ACCEPT' else Safe := 'REFUSE';
 RecordLine('RESULT ' + Observed + ' old-oracle=' + Legacy + ' safety-oracle=' + Safe);
 RecordLine('REFUSAL ' + Refusal);
 RecordLine('AFTER ' + AfterState);
 if BeforeState <> AfterState then RaiseException('Preflight changed fixture state');
 RecordLine('CHECKED-STATE UNCHANGED');
 if FixedRecipe then begin
  if Observed <> Safe then RaiseException('Fixed recipe differed from safety oracle');
 end else if Observed <> Legacy then RaiseException('Old recipe differed from red/control oracle');
end;
function InitializeSetup: Boolean;
begin
 Result := False;
 RunRoot := ExpandConstant('{localappdata}\VaultV2-SyntheticPreflightProbe\') + ProbeId;
 ReportPath := RunRoot + '\report.txt';
 try
  if RegKeyExists(HKCU, 'Software\VaultV2-SyntheticPreflightProbe\' + ProbeId) then
   RaiseException('This probe already ran; keep its fixtures and use a new build');
  if not PlainAncestors(RunRoot) then RaiseException('Synthetic root is linked or inaccessible');
  MakeDirectory(RunRoot);
  MakeCanary(RunRoot + '\external-canary.txt');
  RecordLine('SYNTHETIC native preflight only; target=200; no installation');
  RunCase('leftover300', 100, True, '300', True, False);
  RunCase('leftover150', 100, True, '150', True, False);
  RunCase('missing-current', 100, False, '', True, False);
  RunCase('malformed-version', 100, True, 'not-a-code', True, False);
  RunCase('overflow-version', 100, True, '2147483648', True, False);
  RunCase('older-retained', 100, True, '50', True, True);
  RunCase('existing-target', 100, True, '200', False, False);
  RunCase('equal-registry', 200, True, '', False, False);
  RunCase('newer-registry', 300, True, '', False, False);
  if FixedRecipe then RecordLine('COMPLETE: all safety oracles matched. This is not release acceptance.')
  else RecordLine('COMPLETE: all old-recipe oracles matched. This is not release acceptance.');
  MsgBox('Synthetic preflight probe complete. Fixtures retained at ' + RunRoot, mbInformation, MB_OK);
 except
  MsgBox('Probe stopped: ' + GetExceptionMessage + #13#10 +
   'Any partial fixtures remain at ' + RunRoot, mbError, MB_OK);
 end;
 { False prevents the setup wizard, install phase and all payload operations. }
end;
"""


def compile_probe(compiler: Path, output_dir: Path, recipe: Path = RECIPE) -> Path:
    if os.name != "nt":
        raise BuildError("Windows-only native probe compiler")
    compiler, output_dir, recipe = (Path(p).absolute() for p in (compiler, output_dir, recipe))
    for path in (compiler, output_dir, recipe):
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
    raw = recipe.read_bytes()
    build_id = uuid.uuid4().hex
    generated = make_probe(raw, build_id)
    output_dir.mkdir(parents=True)
    script = output_dir / "preflight-probe.iss"
    script.write_text(generated, encoding="utf-8-sig")
    command = [str(compiler), "--no-ide-signtools", "/Q", f"/O{output_dir}", str(script)]
    try:
        compiled = subprocess.run(command, cwd=output_dir, capture_output=True, timeout=120,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        raise BuildError("Probe compilation failed or timed out; output retained") from None
    (output_dir / "compiler.log").write_bytes(compiled.stdout + compiled.stderr)
    if compiled.returncode:
        raise BuildError("Probe compiler refused source; inspect retained compiler.log")
    executable = output_dir / "preflight-probe.exe"
    if not executable.is_file() or executable.read_bytes()[:2] != b"MZ":
        raise BuildError("Probe executable missing or invalid")
    evidence = {
        "schema": "vault-v2-native-preflight-probe@1", "build_id": build_id,
        "production_recipe_sha256": hashlib.sha256(raw).hexdigest(),
        "generated_recipe_sha256": _file_digest(script)[0],
        "executable_sha256": _file_digest(executable)[0],
        "compiler_components": COMPILER_HASHES, "native_execution": "NOT RUN",
        "fixture_root": "%LOCALAPPDATA%/VaultV2-SyntheticPreflightProbe/" + build_id,
        "registry_root": "HKCU/Software/VaultV2-SyntheticPreflightProbe/" + build_id,
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
