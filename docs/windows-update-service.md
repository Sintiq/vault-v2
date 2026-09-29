# Installed Windows update service

2026-09-26. This is controller/transport evidence, not installer execution,
clean-machine acceptance, a working public release channel, or production trust.

## Public boundary

Production constructs `UpdateService()` with no arguments. Construction performs
no filesystem/network IO. It imports no Qt and acquires no runtime marker.

- `configuration() -> UpdateConfiguration | UpdateError`: read-only installed
  identity, trust and channel. Frozen configuration fields are `version`,
  `version_code`, `api_version`, `channel`, `key`, `staging_parent`,
  `installed_directory`.
- `check() -> ReadyUpdate | UpdateError`: fetch fixed metadata, verify, stage,
  inspect. Frozen `ReadyUpdate` contains `staged` and `offer`.
- `install(ready, confirmed_digest) -> InstallerStarted | UpdateError`: consume
  the displayed ready result and explicit confirmation, reread configuration,
  reauthenticate/recheck through the core handoff. The only success state is
  `started_unconfirmed`, never installation success.

Calls must be serialized by the GUI busy-job gate; check/install run off the UI
thread. A fresh check invalidates old ready results. Install consumes approval
even on refusal. After a successful launch the service refuses further actions.
A caller-constructed ReadyUpdate is not accepted. Retaining an issued object is
also insufficient: handoff repeats signature, version/API, size/hash, path,
confirmation and cooperating-process admission checks. No runtime is killed.

The service remembers the full configuration that produced the displayed offer.
Changes to key, channel, version/API, directory or staging parent invalidate it.
Configuration is reread after network fetching and after package staging too.
UpdateError contains only a fixed code and fixed user-safe message; no server
body, URL credential, filesystem exception or crypto exception is displayed.

## Installed inputs and paths

`vault-install.json` is strict bounded UTF-8 JSON (4 KiB), with exactly schema,
worker_python, program_root, version, version_code and api_version. The supported
installed layout is `.../versions/<version_code>`, `program_root = ../..`, and
`worker_python = python/python.exe`. Version is printable ASCII, 1–64 characters;
version/API are exact positive signed-32-bit-range integers, not bool/float.
The builder separately checks marker API identity against bundled source.

`vault_v2/release_key.pub` is at most 1 KiB, base64 DER SPKI for P-256.
Missing/malformed key disables updates. Missing channel also disables updates.
`vault_v2/update-channel.json` is strict UTF-8 JSON (4 KiB), exactly:

```json
{"schema":"vault-v2-update-channel@1","url":"https://example.invalid/releases"}
```

No automatic channel discovery, local source picker, credentials, vault settings,
documents or tokens are used. Channels reject userinfo, queries/fragments,
percent escapes, backslashes, whitespace and parent traversal. Configuration
paths reject symlink/reparse ancestors. Default staging is
`%LOCALAPPDATA%/VaultV2/updates`, separate from default `VaultV2/data` and all
program versions; configuration does not create it. Check creates staging only
after manifest verification. Completed attempts are preserved, not overwritten
or recursively removed by this controller. A verified stage superseded by a new
check, or followed by a later configuration/handoff refusal, can remain in this
dedicated updates directory; there is no automatic deletion policy here.

## Bounded HTTPS metadata transport

Metadata names are fixed `vault-release.json` (16 KiB) and
`vault-release.json.sig` (1 KiB). Original response bytes go to the verifier.
An isolated child uses validated TLS, no redirects/proxies/auth/cookies, status
200 only and identity content encoding. It rejects ambiguous framing, oversized
advertised lengths, truncated advertised bodies and streamed cap-plus-one data.
Both metadata requests share one parent wall deadline of 30 seconds including
process startup, DNS, TLS and reads. Timeout kills/reaps the worker; socket idle
timeout is not presented as the total deadline. Child output is bounded by the
two fixed body caps, with parent framing/size checks before decoding it.

The child inherits only SystemRoot/WINDIR/TEMP/TMP and standard SSL_CERT_FILE /
SSL_CERT_DIR trust configuration, not proxy/token/vault environment settings.
The latter standard CA overrides also support the test-only loopback certificate;
production certificate validation is never disabled.

Package staging then uses the existing separately bounded 60-second stage seam.
Process reaping may add up to 2 seconds. Parent filesystem calls, hashing during
offer inspection/handoff, kernel calls and process creation are not claimed to
have a hard-real-time deadline. This is cooperating local-process safety, not a
sandbox against hostile same-user filesystem mutation. No automatic cleanup of
old completed attempts, GUI cancellation, or public-channel availability claim.

## Synthetic verification

Five observed RED/GREEN increments: missing configuration seam; strict installed
configuration; absent check seam; absent install/test-launch seam; shortened
metadata deadline/configuration-change transport seam. Additional negative cases
were regression checks, not all separately observed RED.

Tests exercise the public service against real verifier, stage and handoff.
Tiny HTTPS loopback responses cover metadata/package success, TLS refusal,
redirects, bounds/framing, total slow-response deadline, signed-byte tampering,
API/version refusal and package corruption. The TLS fixture is shared with stage
tests; its synthetic TLS key is briefly written to load SSL then removed. Release
signing keys remain in memory, and only synthetic public keys enter test fixtures.
Ephemeral test trust does not establish owner/production signing trust.

External process launch is replaced by a callback and runtime admission uses a
unique test namespace. One configuration-change case substitutes only the
external metadata transport boundary. No real installer is executed. Explicit
constructor directory overrides and `_test_launch`, `_test_namespace`,
`_test_metadata_timeout_s` are synthetic-test seams, never UI/channel options.

Focused command:

```text
python -B -m pytest tests/test_update_service.py -q -p no:cacheprovider --tb=short
```

Initial focused result: **57 passed in 11.97 seconds**, using the main development
virtual environment. Owned files had no trailing whitespace. No commits or
changes to stage, handoff, runtime, GUI or builder modules were made in this slice.

Bundled CPython 3.14.7 subsequently flagged a return inside process cleanup's
finally block. Two new external-process failure cases were observed RED: failed
kill and timed-out reaping returned refusal but left owned pipes open. Cleanup
now closes pipes, then raises a private fixed failure on uncertain termination;
the outer transport boundary maps it to `worker_failed`. It never reports an
uncertain live child as successful and contains no return in finally.

After this narrow correction, **59 focused tests passed in 12.27 seconds**.
The actual 26092604 bundled CPython 3.14.7 compiled every current `vault_v2/*.py`
source via `compile(source, filename, 'exec')`, with SyntaxWarning treated as an
error: **68 files, zero failures**. Before the fix, only update_service.py failed
this same check. Source code was not executed by the compile probe and no pycache
was written; no warning suppression or unrelated source changes were used.

GUI integration, installed entry point, real release channel/signing identity,
clean Windows and actual install/upgrade/uninstall acceptance remain separate.
