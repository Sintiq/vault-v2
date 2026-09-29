; Compile-only candidate; clean-machine acceptance is OPEN.
#ifndef AppVersion
 #error AppVersion is required
#endif
#ifndef VersionCode
 #error VersionCode is required
#endif
#ifndef PayloadFiles
 #error PayloadFiles is required
#endif
[Setup]
AppId={{F2A4CECE-C95B-44C2-AEF9-8FC574BDBE57}
AppName=Vault V2
AppVersion={#AppVersion}
AppPublisher=Vault V2
DefaultDirName={localappdata}\Programs\VaultV2
UsePreviousAppDir=no
DisableDirPage=yes
DefaultGroupName=Vault V2
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=no
RestartApplications=no
RestartIfNeededByRun=no
AppMutex=Global\VaultV2-InstallAdmission-v1.runtime
UninstallDisplayName=Vault V2
UninstallFilesDir={app}\uninstall
Compression=lzma2/fast
SolidCompression=yes
SetupLogging=yes
WizardStyle=modern
#ifdef RecipientTerms
LicenseFile={#RecipientTerms}
#endif
; No [Run], broad deletion, service, firewall, archive or key operations.
#include PayloadFiles
[Icons]
Name: "{userprograms}\Vault V2"; Filename: "{app}\versions\{#VersionCode}\python\pythonw.exe"; Parameters: "-I -B -m vault_v2.launcher"; WorkingDir: "{app}\versions\{#VersionCode}"
Name: "{userprograms}\Vault V2 Updates"; Filename: "{app}\versions\{#VersionCode}\python\pythonw.exe"; Parameters: "-I -B -m vault_v2.update_dialog"; WorkingDir: "{app}\versions\{#VersionCode}"
[Registry]
Root: HKCU; Subkey: "Software\VaultV2\Installer"; ValueType: dword; ValueName: "VersionCode"; ValueData: "{#VersionCode}"; Flags: uninsdeletevalue
[Code]
const
 Admission = 'Global\VaultV2-InstallAdmission-v1';
 RegistryPath = 'Software\VaultV2\Installer';
 MaxTreeDepth = 64;
 MaxTreeEntries = 100000;
 MaxTreeMilliseconds = 30000;
type
 { WIN32_FIND_DATAW: 11 DWORDs + WCHAR[260] + WCHAR[14] = 592 bytes.
   Inno 7.1.0 WideChar is 2 bytes and script record fields are contiguous.
   These field sizes also match native Windows alignment; no managed strings. }
 TNativeFindData = record
  Attributes: DWORD;
  CreationTime, LastAccessTime, LastWriteTime: TFileTime;
  SizeHigh, SizeLow, Reserved0, Reserved1: DWORD;
  FileName: array[0..259] of WideChar;
  AlternateFileName: array[0..13] of WideChar;
 end;
var InstallerHandle: THandle;
function WinCreateMutex(Security: LONG_PTR; InitialOwner: BOOL; Name: String): THandle;
 external 'CreateMutexW@kernel32.dll stdcall';
function WinOpenMutex(Access: DWORD; Inherit: BOOL; Name: String): THandle;
 external 'OpenMutexW@kernel32.dll stdcall';
function WinWait(Handle: THandle; Milliseconds: DWORD): DWORD;
 external 'WaitForSingleObject@kernel32.dll stdcall';
function WinReleaseMutex(Handle: THandle): BOOL;
 external 'ReleaseMutex@kernel32.dll stdcall';
function WinCloseHandle(Handle: THandle): BOOL;
 external 'CloseHandle@kernel32.dll stdcall';
function WinFileAttributes(Name: String): DWORD;
 external 'GetFileAttributesW@kernel32.dll stdcall';
function WinFindFirst(Name: String; var Item: TNativeFindData): THandle;
 external 'FindFirstFileW@kernel32.dll stdcall';
function WinFindNext(Handle: THandle; var Item: TNativeFindData): BOOL;
 external 'FindNextFileW@kernel32.dll stdcall';
function WinFindClose(Handle: THandle): BOOL;
 external 'FindClose@kernel32.dll stdcall';
function WinTicks: UInt64;
 external 'GetTickCount64@kernel32.dll stdcall';
function AbsentMarker(Name: String): Boolean;
var Handle: THandle;
begin
 Handle := WinOpenMutex($00100000, False, Name);
 if Handle <> 0 then begin
  WinCloseHandle(Handle);
  Result := False;
 end else Result := DLLGetLastError = 2;
end;
function AcquireAdmission: Boolean;
var Gate: THandle; Status: DWORD;
begin
 Result := False;
 Gate := WinCreateMutex(0, False, Admission + '.gate');
 if Gate = 0 then Exit;
 try
  Status := WinWait(Gate, 0);
  if (Status <> 0) and (Status <> $80) then Exit;
  try
   if not AbsentMarker(Admission + '.installer') then Exit;
   if not AbsentMarker(Admission + '.runtime') then Exit;
   InstallerHandle := WinCreateMutex(0, False, Admission + '.installer');
   Result := InstallerHandle <> 0;
  finally WinReleaseMutex(Gate); end;
 finally WinCloseHandle(Gate); end;
end;
procedure ReleaseAdmission;
begin
 if InstallerHandle <> 0 then begin
  WinCloseHandle(InstallerHandle);
  InstallerHandle := 0;
 end;
end;
function PlainAncestors(Path: String): Boolean;
var Attributes, LastError: DWORD; Parent: String;
begin
 Result := False;
 while Path <> '' do begin
  Attributes := WinFileAttributes(Path);
  if Attributes = $FFFFFFFF then begin
   LastError := DLLGetLastError;
   if (LastError <> 2) and (LastError <> 3) then Exit;
  end else if ((Attributes and $400) <> 0) or ((Attributes and $10) = 0) then Exit;
  Parent := ExtractFileDir(Path);
  if Parent = Path then Break;
  Path := Parent;
 end;
 Result := True;
end;
function FixedRoot: String;
begin Result := ExpandConstant('{localappdata}\Programs\VaultV2'); end;
function NativeFindName(var Item: TNativeFindData): String;
var Index: Integer;
begin
 Result := '';
 for Index := 0 to 259 do begin
  if Item.FileName[Index] = #0 then Exit;
  Result := Result + Item.FileName[Index];
 end;
 { An unterminated buffer is uncertainty, not a usable child path. }
 Result := '';
end;
function TreeBudgetAvailable(Started: UInt64): Boolean;
var Now: UInt64;
begin
 Now := WinTicks;
 Result := (Now >= Started) and (Now - Started <= MaxTreeMilliseconds);
end;
function PlainTreeWithin(Path: String; Depth: Integer; var Entries: Integer;
 Started: UInt64): Boolean;
var Item: TNativeFindData; Handle: THandle; LastError, Attributes: DWORD;
 Name: String;
begin
 Result := False;
 if (Depth > MaxTreeDepth) or (Entries >= MaxTreeEntries)
  or (Length(Path) > 32760) or not TreeBudgetAvailable(Started) then Exit;
 if not PlainAncestors(Path) then Exit;
 Attributes := WinFileAttributes(Path);
 if (Attributes = $FFFFFFFF) or ((Attributes and $400) <> 0)
  or ((Attributes and $10) = 0) then Exit;
 Handle := WinFindFirst(AddBackslash(Path) + '*', Item);
 if NativeInt(Handle) = -1 then begin
  LastError := DLLGetLastError;
  Result := (LastError = 2) and TreeBudgetAvailable(Started);
  Exit;
 end;
 try
  repeat
   if (Entries >= MaxTreeEntries) or not TreeBudgetAvailable(Started) then Exit;
   Entries := Entries + 1;
   if (Item.Attributes and $400) <> 0 then Exit;
   Name := NativeFindName(Item);
   if Name = '' then Exit;
   if (Name <> '.') and (Name <> '..') then begin
    if (Pos('\', Name) > 0) or (Pos('/', Name) > 0) or (Pos(':', Name) > 0)
     or (Name[Length(Name)] = '.') or (Name[Length(Name)] = ' ') then Exit;
    if (Item.Attributes and $10) <> 0 then
     if not PlainTreeWithin(AddBackslash(Path) + Name, Depth + 1, Entries, Started) then Exit;
   end;
   if not WinFindNext(Handle, Item) then begin
    { Only external DLL calls update Inno's saved error; built-in FindNext does not. }
    LastError := DLLGetLastError;
    Result := (LastError = 18) and TreeBudgetAvailable(Started);
    Exit;
   end;
  until False;
 finally
  if not WinFindClose(Handle) then Result := False;
 end;
end;
function PlainTree(Path: String): Boolean;
var Entries: Integer; Started: UInt64;
begin
 Result := False;
 Entries := 0;
 Started := WinTicks;
 try
  Result := PlainTreeWithin(Path, 0, Entries, Started);
 except
  Result := False;
 end;
 { The elapsed limit is checked between calls, not a hard kernel-IO deadline.
   No files change here; this is not a hostile concurrent-writer sandbox. }
end;
function ConsistentVersions(Previous: Cardinal): Boolean;
var Item: TNativeFindData; Handle: THandle; LastError: DWORD;
 Name, Path: String; Code, Index, Entries: Integer; FoundCurrent: Boolean;
 Started: UInt64;
begin
 Result := False;
 FoundCurrent := False;
 Entries := 0;
 Started := WinTicks;
 Path := FixedRoot + '\versions';
 if not DirExists(Path) or not PlainAncestors(Path) then Exit;
 Handle := WinFindFirst(AddBackslash(Path) + '*', Item);
 if NativeInt(Handle) = -1 then Exit;
 try
  repeat
   if (Entries >= MaxTreeEntries) or not TreeBudgetAvailable(Started) then Exit;
   Entries := Entries + 1;
   Name := NativeFindName(Item);
   if Name = '' then Exit;
   if (Name <> '.') and (Name <> '..') then begin
    if ((Item.Attributes and $400) <> 0) or ((Item.Attributes and $10) = 0) then Exit;
    if (Length(Name) > 10) or (Name[1] = '0') then Exit;
    for Index := 1 to Length(Name) do
     if (Name[Index] < '0') or (Name[Index] > '9') then Exit;
    Code := StrToIntDef(Name, -1);
    if (Code <= 0) or (Cardinal(Code) > Previous) then Exit;
    if Cardinal(Code) = Previous then FoundCurrent := True;
   end;
   if not WinFindNext(Handle, Item) then begin
    LastError := DLLGetLastError;
    Result := (LastError = 18) and FoundCurrent and TreeBudgetAvailable(Started);
    Exit;
   end;
  until False;
 finally
  if not WinFindClose(Handle) then Result := False;
 end;
end;
function Preflight: String;
var Previous: Cardinal; HasVersion: Boolean;
begin
 Result := 'Installation directory is changed, linked or inaccessible; no files were installed.';
 if CompareText(RemoveBackslashUnlessRoot(ExpandConstant('{app}')), FixedRoot) <> 0 then Exit;
 if not PlainAncestors(FixedRoot + '\versions\{#VersionCode}') then Exit;
 if not PlainAncestors(FixedRoot + '\uninstall') then Exit;
 if DirExists(FixedRoot) then
  if not PlainTree(FixedRoot) then Exit;
 HasVersion := RegValueExists(HKCU, RegistryPath, 'VersionCode');
 if HasVersion then begin
  Result := 'Installed version is unreadable, equal or newer; replacement is refused.';
  if not RegQueryDWordValue(HKCU, RegistryPath, 'VersionCode', Previous) then Exit;
  if (Previous = 0) or (Previous >= {#VersionCode}) then Exit;
  Result := 'Installed version and version directories disagree; manual review is required.';
  if not ConsistentVersions(Previous) then Exit;
 end else if DirExists(FixedRoot) then begin
  Result := 'An unrecognized program directory exists. Do not install over an archive.';
  Exit;
 end;
 Result := 'This version directory already exists; partial or repeated installation needs manual review.';
 if DirExists(FixedRoot + '\versions\{#VersionCode}') then Exit;
 Result := '';
end;
function InitializeSetup: Boolean;
var Attempt: Integer;
begin
 { Handoff releases its marker after Popen. Bounded wait, no process termination. }
 Result := False;
#ifdef RecipientTerms
 { This distribution requires a visible recipient agreement, not silent assent. }
 if WizardSilent then begin
  Log('Silent installation refused: review the Microsoft runtime component terms interactively.');
  Exit;
 end;
#endif
 for Attempt := 1 to 20 do begin
  if AcquireAdmission then begin Result := True; Exit; end;
  Sleep(100);
 end;
 SuppressibleMsgBox('Close all Vault windows and other installers, then retry. Unknown state also refuses installation.', mbError, MB_OK, IDOK);
end;
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
 NeedsRestart := False;
 if InstallerHandle = 0 then Result := 'Installation admission was lost; retry.'
 else Result := Preflight;
end;
procedure DeinitializeSetup;
begin ReleaseAdmission; end;
function InitializeUninstall: Boolean;
begin
 Result := AcquireAdmission;
 if Result then Result := (CompareText(RemoveBackslashUnlessRoot(ExpandConstant('{app}')), FixedRoot) = 0)
  and PlainTree(FixedRoot);
 if not Result then begin
  ReleaseAdmission;
  SuppressibleMsgBox('Close all Vault windows and verify the program directory before uninstalling.', mbError, MB_OK, IDOK);
 end;
end;
procedure DeinitializeUninstall;
begin ReleaseAdmission; end;
