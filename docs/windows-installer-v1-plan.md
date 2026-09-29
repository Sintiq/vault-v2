# Windows installer v1 — implementation boundary

Started 2026-09-26 from 7231c11. A full synthetic payload and unsigned Inno
candidate now compile; this is not a qualified installer available to users.
Windows work remains independent of the functional fixes held by Claude.

## Selected shape

- Per-user Windows x64 Inno Setup package, `PrivilegesRequired=lowest`; fixed
  program path `%LOCALAPPDATA%/Programs/VaultV2`, not the Vault storage folder.
- Existing installed default data root remains `%LOCALAPPDATA%/VaultV2/data`;
  explicit external `VAULT_V2_ROOT` remains the application's choice. Installer
  never copies, migrates or recursively deletes an archive.
- Versioned application payload below the program directory; only source/static
  files on a build allowlist and pinned verified dependencies. No live data, venv,
  caches, history, settings, phone tokens or private signing material.
- Start Menu shortcut targets bundled `pythonw.exe -I -B -m vault_v2.launcher`.
  No dependency on developer Python, PATH, current working directory or pip.
- App startup holds a process-lifetime named Windows mutex used by Inno AppMutex.
  It is installation admission only, not the per-root RuntimeLease and not a
  restriction on multiple read-only windows. Setup asks the user to close running
  instances; it never kills them (`CloseApplications=no`, `RestartApplications=no`).
  A shared short admission gate also serializes app/installer entry; Setup retains
  an installer marker until exit so a new app cannot start mid-install. This is
  separate from RuntimeLease, includes read-only windows, and fails closed on
  unknown OS state. Installer and uninstaller use the same named protocol.
- No automatic program launch/service start on install. An optional unchecked
  final launch action can be added after qualification; do not start the live Vault
  during a build test. No Tailscale/Ollama/firewall changes from the installer.
- Uninstall removes only tracked program files/shortcuts. No broad
  `[UninstallDelete]` or `[InstallDelete]`, no data-root cleanup, no settings reset,
  no uninstall of shared third-party applications.
- Application and installer version_code must agree; installer should refuse
  downgrade/equal-version destructive replacement. No data migration in this slice.

## Build gates

1. Runtime pins: supported patched CPython, ABI-compatible native PDF/OCR/Qt,
   licenses/notices and SHA256/size verification. Upgrade cp312 proof to cp314.
2. Real synthetic Qt/PDF/OCR children from the new bundle, Unicode path, isolated
   launch from another cwd, no host venv/PATH dependence. Main archive untouched.
3. Complete payload: explicit web assets and launcher, declared optional release
   public key and signed APK inputs. Missing real key means updates disabled;
   missing APK is visible, never silently replaced by a debug or test artifact.
4. Compile with an official, publisher-verified Inno compiler. Compiler acquisition
   is build-time only. No helper installer run on the owner's PC without a clear
   scoped decision about that host change; prefer an isolated build environment.
5. Synthetic installation/upgrade/uninstall and active-window refusal in a disposable
   environment; prove old synthetic archive hashes survive. Independently test a
   clean Windows environment without developer runtimes. Until then INSTALL_UNVERIFIED.
6. Real release identity/signatures come from the owner out of band. Test signatures
   are ephemeral, never promoted into production. Public publication is not implied.

## Primary references checked

- [Inno AppMutex](https://jrsoftware.org/ishelp/topic_setup_appmutex.htm): the
  application must create the named mutex; Setup/Uninstall check for its existence.
- [Inno CloseApplications](https://jrsoftware.org/ishelp/topic_setup_closeapplications.htm):
  default is yes, so explicitly opt out of automatic closure/restart.
- [Official Inno downloads](https://jrsoftware.org/isdl.php): 7.1.0 x64 installer
  published 2026-08-12, publisher Pyrsys B.V.; compiler not found locally during
  initial read-only inventory. It was subsequently acquired into separate portable
  build-tools and used for compilation, not installed system-wide. Exact provenance
  and limited before/after checks: [Inno evidence](windows-inno-build-tools.md).

The release verifier/stager/explicit-install core interfaces in
release-format-v1.md are implemented. Payload building uses an explicit source
allowlist and pinned dependencies; compilation revalidates the entire payload
inventory and invokes the pinned portable compiler with ambient IDE signing tools
disabled. No generated Vault installer has been run on the owner's host.

The installed launcher protects the entire program tree, including sibling
versions, from use as a Vault data root. Explicit roots and default roots use the
same separation rule. Missing markers in embedded CPython refuse startup rather
than silently choosing the development default. Installer preflight rejects
equal/downgrade/unknown installations, an existing target version directory and
linked program-tree entries. Removal is only Inno's tracked program files; no
recursive archive cleanup. The Pascal traversal and upgrade/uninstall behavior
still require disposable-Windows execution, not just compilation or Python tests.

The separate [updater UI](windows-updater-ui.md) now has its own Start Menu entry,
installed key/channel/identity binding, explicit check/download and confirmation,
and background bounded workers. It neither owns nor opens a Vault root. The lead
will connect the main-window entry after safe normal window closure.

Open gates: clean/disposable-machine install/upgrade/uninstall and archive hash
preservation; managed-window refusal in the actual installer; full app launch;
real channel/signing integration and installed update acceptance;
redistribution-license review; final merge/retest with the
lead's branch. The lead now confirms owner-controlled P-256/Android keys exist
and public files are in0577341. This synthetic checkpoint does not copy/use them.
The owner subsequently reopened the Authenticode choice: official certificate
options/account/identity prerequisites are being researched in parallel, with
no purchase/payment or identity submission before confirmation. Current EXEs
remain unsigned, so Windows warning behavior is still an acceptance limitation.
The lead will sign the first real release manifest with the separate P-256 key.
WindowsSandbox.exe and VM CLIs were not found in the searched host locations/PATH.
No hypervisor feature was enabled or VM created. Status stays INSTALL_UNVERIFIED.
This document neither lifts earlier functional review HOLDs nor claims a package
is ready for new users.
