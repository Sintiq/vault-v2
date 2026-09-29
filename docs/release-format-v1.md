# Release format v1 — PC / Android shared contract

Clarification of the lead's 2026-09-26 slice 6 message. Public interfaces and
strict cross-platform rules confirmed by the lead in the subsequent
TO-CODEX message for 7231c11..1bc6e1c. No production keys here.

## Public interfaces under test

1. Verify exact manifest bytes + detached signature against the installed key,
   expected platform, installed version_code and available API version. Return
   an immutable release description or a typed refusal; no writes or networking.
2. Stage a matching package from an explicitly selected channel: bounded download,
   expected size and SHA256, only publish a new staging file after verification.
   No execution. Invalid/partial files never become installable.
3. Explicit Windows installation handoff: reverify the staged package, refuse
   an active/unknown Vault runtime, show the exact release and require human action.
   Never kill a window, auto-update, change Tailscale/Ollama, or delete the data root.

All three core interfaces now exist. The separate Windows updater and installed
identity/channel wiring also exist; the lead's main-window entry and actual
installed/disposable-machine acceptance remain integration tasks. See
[updater UI](windows-updater-ui.md).

## Wire data

`vault-release.json`: strict UTF-8 JSON object, at most 16 KiB; no duplicate keys,
BOM, NaN/Infinity, coercion or unknown fields. Exactly:

- `schema`: `vault-v2-release@1`
- `version`: nonempty printable ASCII display label, at most 64 characters.
- `version_code`: JSON integer 1..2147483647 (not bool or fractional number).
- `platform`: `windows` or `android`, must match the requested platform.
- `file`: ASCII basename, `[A-Za-z0-9][A-Za-z0-9._-]{0,119}`, no `..`, no URL,
  slash/backslash/colon, Windows reserved device basename, or trailing dot/space.
  Windows suffix `.exe`, Android `.apk`. It is relative to the configured channel,
  never an arbitrary URL supplied by the manifest.
- `size`: JSON integer >0 and <= 1073741824 bytes for Windows, <=268435456 for APK.
- `sha256`: 64 lowercase hexadecimal characters.
- `min_api_version`: JSON integer 1..2147483647, <= available API version.

`version_code` must be strictly greater than the installed value for an update;
display version is not the ordering key. Signature validity alone does not admit
a downgrade, another platform, incompatible API or an oversized package.

`vault-release.json.sig`: base64 of ASN.1 DER ECDSA signature, P-256 / SHA-256,
over the manifest's **original bytes**. No JSON reserialization before verification.
Allow surrounding ASCII whitespace only; reject embedded whitespace/invalid base64.
Bound the signature sidecar to 1 KiB before decoding. Accept valid DER as understood
by the platform crypto provider; do not mistake raw 64-byte r||s for DER.

Trusted key: base64 SubjectPublicKeyInfo DER, P-256 only. PC file
`vault_v2/release_key.pub`; Android build input `phone/release-key.pub` compiled into
BuildConfig. It is shipped out-of-band with the installed app, never accepted from
the channel or manifest. No/malformed key disables updates, not verification.
Test keys are ephemeral test fixtures only and must never enter a release bundle.
The PC verifier bounds this base64 key input to 1 KiB including surrounding ASCII
whitespace; this defensive size clarification was sent to the Android lead.

## First implemented interface

`vault_v2.releases.verify_release(raw_manifest, detached_signature,
installed_public_key, expected_platform, installed_version_code,
available_api_version)` is pure: no file/network/process operations. Inputs holding
wire data are immutable bytes. It returns frozen `ReleaseDescription` or
`ReleaseRefusal` with a fixed `RefusalCode`; untrusted content/crypto exceptions are
not returned as errors. Missing/malformed installed trust means `updates_disabled`.
The caller supplies its installed version/API; exact integer version 0 denotes no
previous release, while available API must be 1..2147483647. Protocol suffixes
are lowercase `.exe` and `.apk` on both platforms.

Metadata success is not package validation or installation authority. The result
type can be constructed by callers; future stage/install boundaries must reverify
the signed inputs and package bytes, not infer trust from a Python type.

TDD evidence: eight observed RED/GREEN increments; **186 focused tests passed**.
Parent rerun with builder regression tests: **207 passed, 1 symlink-permission skip,
1.08 s**. A separate compatibility smoke ran this source module with the actual
embedded CPython 3.14.7/dependencies (not a new complete bundle): valid manifest
accepted; changed bytes and signed duplicate key refused; absent key disabled.
An initial diagnostic command had a quoting syntax error before execution and
was corrected; the passing invocation exercised the real verifier. All keys were
generated only in memory. No real signing identity was used or persisted.

The pure verifier itself does not download or install. The later staging boundary
now compares actual package size/SHA256; install handoff repeats that verification.
Python results do not qualify Android or release availability. The Android lead's
separate branch is not included in this Windows checkpoint.

## Implemented staging and explicit handoff

`release_stage.stage_release(...)` accepts the original signed bytes and current
installed trust/version/API again, not just a `ReleaseDescription`. Sources are an
explicit `LocalReleaseSource(directory)` or `HttpsReleaseSource(channel_url)`.
The caller supplies an existing, ordinary local `staging_parent`. HTTPS preserves
certificate validation, sends no credentials/proxy/cookies and rejects all
redirects. A bounded child performs transfer, hash and fsync. Only verified bytes
are published into a unique `release-*` attempt, without replacing other attempts.
Its immutable `StagedRelease` is a receipt, not install authority. See
[stage evidence and exact deadline limitations](windows-stage-qualification.md).

`release_handoff.inspect_staged_release(...)` reauthenticates against the caller's
current installed key/version/API and hashes the actual retained file. It returns
an `InstallOffer` with authenticated release fields and an exact approval digest.
Only an explicit user action should pass that displayed digest to
`handoff_release(..., confirmed_digest=...)`, which repeats checks. The file's
Windows handle denies write/delete through process creation. Changed bytes, key,
version/API, path or signature cannot reuse an old displayed approval.

Handoff refuses any active/unknown managed runtime, never kills a window, and
starts the installer without silent flags. `InstallerStarted.state` is always
`started_unconfirmed`, not successful installation. A later installer admission
gate must independently refuse any app that starts after handoff. These are
cooperating-process controls, not a hostile same-user sandbox. The standalone
updater is the GUI caller; no background auto-update, automatic channel discovery
or persistent approval is supplied. Its channel and public key must be explicit
build inputs and are read only from the installed program.

## Channels and installation boundary

No automatic channel discovery. An explicit HTTPS channel or local test directory
is required; don't read tokens, vault documents or private settings for a request.
HTTPS keeps certificate validation and refuses all redirects, including same-origin redirects;
no HTTP fallback. A test directory is a local-only development adapter.

Version 1 Windows packages do not migrate the Vault data schema. An installer must
refuse a running Vault rather than terminate it; program binaries and data root stay
disjoint. Any future data migration requires a separately qualified, verified backup
and migration design before installation. There is no silent rollback of binaries
over migrated data. Installer remains RELEASE HOLD without maintained runtime,
owner's real signing identity and clean-machine acceptance.
