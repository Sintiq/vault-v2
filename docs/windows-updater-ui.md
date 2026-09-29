# Separate Windows updater

Entry: bundled `pythonw.exe -I -B -m vault_v2.update_dialog`, via **Vault V2
Updates** Start Menu shortcut. No command-line root/key/channel overrides,
Vault root, per-root lease, normal-runtime marker, watcher or scheduled updater.
The main window exposes **Vault > Check for updates…** without changing the
toolbar order. A default-No confirmation precedes normal close/drain of that
window, then the fixed bundled interpreter starts this module. Refused close
means no launch; other windows are not closed. Source checkouts stay open with
an explanation, and missing runtime files refuse before closing. Launch failure
after close gives a fixed message and never resurrects a writer whose lease was
released. The updater never closes/kills Vault.

Opening reads only the installed program's version/code/API, public key and
channel. Missing key or channel visibly disables updates. The builder accepts
`--public-key` and `--update-channel` explicitly; it never silently copies those
files from the checkout. Channel file is strict JSON:

```json
{"schema":"vault-v2-update-channel@1","url":"https://releases.example.invalid/windows"}
```

The example is not a configured/working release host. Key is P-256 SPKI as in
the shared release format. Marker API must match the source's literal API_VERSION;
the builder checks this without importing the application.

1. **Check and download update** starts one background request. Metadata fetch
   has its own30second child deadline; verified package stage its own60second
   deadline. Local filesystem/hash calls are not an end-to-end hard timeout.
2. The window shows exact authenticated version/code/platform/file/size/SHA256/
   minimum API as plain text. A release-manifest signature is not Authenticode.
3. **Install this release…** opens a second confirmation, also plain text with
   default **No**. Only Yes passes the displayed offer's exact digest to the
   service. Current installed configuration and actual package are checked again.
4. Active/unknown Vault windows refuse installation. The only successful handoff
   label is **process started; installation NOT confirmed**. The UI then offers
   close, not a second install. The installer itself requires independent admission.

Busy state disables actions and refuses window close/Escape until the bounded
job returns. The Qt event loop is not blocked by the download/hash/handoff job;
worker errors restore controls with fixed messages, not raw error contents.
Repeating a check removes the old actionable offer. Every install attempt consumes
the issued ready object, success or refusal, and requires a new check to retry.
Complete stage attempts are retained; no automatic package cleanup is claimed.
See [service evidence and exact limitations](windows-update-service.md).

## Observed qualification

Eleven focused Qt tests: no implicit check/install on open; worker thread separate
from GUI; exact display; No default and Yes digest binding; repeated check/busy
close; missing configuration; exception scrubbing; failed handoff; literal HTML-
looking version in details **and** confirmation; thread-start refusal; synthetic
offscreen rendering. The initial static QMessageBox confirmation was identified
by independent read as rich-text-capable and corrected with observed RED/GREEN.

Synthetic screenshot was inspected: fields, full SHA256 and buttons readable.
First offscreen render lacked native font discovery and showed boxes; the test
harness now loads the existing Windows Segoe font explicitly. This is not a
product font replacement. Two attempts to read pytest-owned temporary screenshots
hit ACL denial; the final synthetic PNG was saved directly to the authorized
artifact folder, without changing ACLs or reading user screenshots/documents.

No generated installer ran. Fake service/launch callbacks cover GUI decisions;
the separate service suite uses real tiny HTTPS loopback metadata/package transport.
Seven synthetic main-window tests cover cancel, source/missing runtime, safe
close, busy refusal, launch failure and an untouched read-only peer. Three
separate-process runtime tests cover fixed installed GUI interpreter selection.
The focused aggregate with toolbar, updater and launcher tests is **55 passed,
2 skipped**. Main-window tests substitute only the OS launch boundary; native
installed handoff and disposable-Windows lifecycle still need acceptance. The
full combined suite after this integration completed: **2,301 passed, 24 skipped,
2 existing Qt warnings**, exit 0. Python/Qt tests do not qualify SmartScreen or
Inno runtime behavior.
