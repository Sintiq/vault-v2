# Contract — devices, phone PIN, phone export, setup, updates

Slice 0 of the owner's GO of 2026-09-26 («Ждём что скажет кодекс и делаем
проект до конца, потом тестим»). Base: master `af581fe`. This document fixes
the behaviour every later slice is built and reviewed against. It adds no
feature by itself.

What the owner holds at the end of the whole series: one Windows installer;
a setup wizard that ends with a paired phone and no terminal; a Devices
section on the PC; a PIN on the phone app; three ways to take a Staging file
to the phone; signed updates.

Owner's words that bind this contract (verbatim):

- «Отправляет документы человек сам, не агент. Агент только подготовит до двери, а нажимает человек.»
- Export: «Когда пк онлайн. Пусть в приложении горит зелёный или красный кружок»; «Да, из staging»;
  «Пин при входе в приложение. И пин когда пользователь хочет выгрузить (действует 5 мин)».
- Claude's addition, accepted by the owner: the 5-minute export window ends when the app
  goes to the background or the screen locks; green means an authorised Vault answer.
- 9 wrong PINs wipe the phone's Vault data (owner's idea, 2026-09-26).
- Setup over standard Tailscale, not an embedded network stack («Принял твой вариант»).

---

## 1. Devices and sessions

**Device.** A phone or a browser that was paired through the PC. Stored in
`<root>/.api/devices.json` (schema `vault-v2-devices@1`):

```
{ "<device_id>": { "name": str, "kind": "app" | "browser" | "legacy",
                   "token_sha256": hex, "paired_at": iso, "revoked_at": iso | null } }
```

- `device_id`: 12 random url-safe characters. `token`: 32 random bytes, url-safe.
  Only `sha256(token)` is stored on the PC; the token itself exists on the device only.
- Authentication: `Authorization: Bearer <token>` → `sha256` → lookup → must exist
  and not be revoked. A device token never rides in a URL: the browser fetches pictures
  with the header and shows them from a local object URL. `?key=` stays only for the
  legacy key (below).
- `last_seen` is kept in memory by the running window (no disk write per request); the
  Devices list shows «not since the window opened» after a restart.
- Every write to `devices.json` goes through the root write guard and leaves a receipt:
  `device_paired`, `device_renamed`, `device_revoked`, `device_self_wipe_requested` (the
  phone said it is erasing itself; the PC did not see it). Tokens never appear in receipts.
- Every change is written to disk before the running window believes it, so a failed
  write cannot look like a done revoke on retry.

**Legacy key (migration).** The existing `<root>/.api/key` becomes the device
`legacy` ("Key shared by devices paired before per-device keys"). It keeps
working, Bearer and `?key=`, so nothing breaks on the day of the switch. The
Devices section shows it with a warning and a Revoke button. Revoking writes the
permanent mark `.api/legacy_revoked` first, then renames `.api/key` to
`key.revoked-<date>`; with the mark present no start of the window makes a shared
key again. A vault set up after this change never gets one. There is no silent fallback: once `legacy` is revoked, only device
tokens open anything. The owner's current phone and browser are re-paired by
him through the new flow before he revokes `legacy`. Nothing is revoked
automatically.

**Pairing (one QR, confirmed at the PC).**

1. At the PC, Devices → *Add device* creates a ticket: 16 random bytes, valid
   5 minutes, single use, held in memory only (a restart cancels it). The dialog
   shows a QR of `vault-pair://pair?u=<base url>&t=<ticket>` and the same text to copy.
2. The phone (camera → app via the `vault-pair` scheme, or the link pasted on the
   Connect screen) calls `POST /api/pair/claim {ticket, name, kind}` — no auth.
   A valid, unclaimed ticket becomes a *claim* with a 6-digit code; the answer is
   `{claim_id, code}`. A second claim of the same ticket is refused (409).
   Looking at the URL (a browser prefetch) does not consume the ticket; only `claim` does.
3. The PC dialog shows «<name> wants to pair — code 482 913 · Confirm / Reject».
   The phone shows the same code. The owner confirms only if they match.
4. The phone polls `POST /api/pair/status {claim_id}`: `waiting` | `rejected` |
   `expired` | `confirmed {device_id, token}`. The token is handed out **once**, then
   the claim is deleted. The PC dialog says «approved, waiting for the device» until the
   device's first authorised request, and «paired» only then; if that never comes it
   says so and points to Revoke. These unauthenticated calls read at most 4 KiB with a
   valid length, within 5 seconds.
5. Nothing about an existing pairing changes if a new attempt expires, is
   rejected or fails half-way.

**Revoke.** Devices → *Revoke* sets `revoked_at`. From the next request on, the
token gets 401 on every protected route (files, blob, PDF pages, agent, chat,
tasks, health, door pairing, export grants, offline copies). A revoked device
that later reappears in Tailscale is still revoked. If the device was the one
that paired the agent door, the door config is removed too — only that device's: the config names its `device_id`, written in the same guarded step as the pairing. Revoking cannot
recall bytes already on the device or already exported; the dialog says so.
*Revoke all* exists as an emergency button.

**Admin operations are PC-only.** Add, rename, revoke happen in the Qt window.
The HTTP API exposes none of them.

**Session endpoint.** `GET /api/session` (authorised) →
`{vault_id, api_version, device_id, device_name}`. `vault_id` is random, created
once per root, stored in `<root>/.api/vault_id`.

## 2. Connection indicator and version compatibility

- The phone shows a dot plus a word in the app bar: **green «PC online»** after an
  authorised `/api/session` answer from the expected `vault_id` within the last 30 s;
  **red «PC offline»** otherwise; **amber «Update needed»** when versions are incompatible.
  Start, timeout, 401, other `vault_id` or a changed address → not green.
- Green is an observation, never a permission: every export grant is re-checked on the PC.
- `api_version` is an integer; this series introduces `2`. The phone declares the
  range it speaks (`2..2`). Outside it, the phone shows only the amber state and an
  update instruction: no file reading, no export, no new grants. `/api/ping` stays
  unauthenticated diagnostics and proves reachability only.

## 3. Phone PIN

- **One PIN**, set on first launch after pairing, 6–12 digits. Asked on entry and
  again for export. (The owner's sentence names two moments, not two codes; if he
  meant a separate export code, only this line changes.)
- Stored as PBKDF2-HMAC-SHA256, 310 000 iterations, 16-byte salt, in app-private
  storage. The device token and the door key are encrypted with an Android Keystore
  AES-GCM key; the PIN is not the key (no key derivation from 6 digits).
- **Lock.** The app locks when it goes to the background, when the screen turns
  off, and on process start. Unlocking needs the PIN.
- **Wrong PINs.** A completed wrong check counts; a cancelled dialog does not. The
  counter and the next-allowed time survive a restart. Delays before the next try:
  attempts 1–4 none; after the 5th 30 s; 6th 1 min; 7th 5 min; 8th 15 min. Before the
  9th the dialog says: «One more wrong PIN erases Vault on this phone.» A correct PIN
  resets the counter. Delays use `SystemClock.elapsedRealtime` plus a persisted floor,
  so changing the phone's clock does not skip them.
- **9th wrong PIN → wipe.** First the state `wiping` is persisted, then: best-effort
  `POST /api/device/self-wipe` with the token (3 s timeout; the PC revokes the device and
  writes `device_self_wipe_requested`); then delete the device token, Keystore key, PIN hash,
  offline copies, temporary export files, door key and config, cached pages, all prefs;
  finally the Connect screen. The sequence is idempotent: an interrupted wipe resumes on
  next start. If the PC did not answer, the PC does not know; the Devices section shows
  the device as not seen since, and the owner revokes it by hand. Originals on the PC,
  files already saved to Files or shared, Tailscale and other apps are not touched.
- **Recents and backup.** `setRecentsScreenshotEnabled(false)` (Android 13+) hides the
  content in the app switcher. `allowBackup=false` and `dataExtractionRules` excluding
  everything keep tokens, PIN hash and offline copies out of cloud backup and
  device-to-device transfer.
- **Forgotten PIN.** No reset on the phone. Wipe it (or wait for 9 wrong), pair again
  from the PC. The PC is the proof of ownership.
- Not claimed: protection against root, ADB with elevated rights, or a tampered app.

## 4. Taking files to the phone

Only **Staging** files, only while the indicator is **green**. The agent may
prepare Staging; the owner presses every button below.

| Action | What it does | Control after |
|---|---|---|
| **Available offline** | Exact bytes into `filesDir/offline/` (not cache), list «On this phone», readable without the PC under the PIN | Stays in Vault; erased by the wipe or by *Remove from phone* |
| **Save a copy to Files…** | `ACTION_CREATE_DOCUMENT`, the owner picks the place (may be a cloud provider — the UI says «the place you choose», not «this phone») | Leaves Vault |
| **Share…** | Android Sharesheet with a read-only `content://` grant via FileProvider for that one file | Leaves Vault |

**Grant path (all three).** `POST /api/phone/grant {rel, sha256, action}` with
`pane` fixed to Staging on the server. The PC checks: device valid, file in Staging,
current `sha256` equals the one the phone saw, action known. Answer
`{grant_id, name, size, sha256}` valid 2 minutes. `GET /api/phone/blob?grant=` delivers
the bytes exactly once per grant (no Range; a failed download needs a new grant), after
checking the path is still inside Staging and the bytes still match; the phone verifies
`sha256` and never marks a partial file as ready. The issue and outcome receipts carry
the grant id (not a token), so each outcome belongs to one issue. Receipt on the PC at issue:
`phone_offline_copy` / `phone_export_issued` with device, rel, sha, action.

**Outcome report.** The phone reports what it observed:
`POST /api/phone/outcome {grant_id, outcome}` with `saved`, `handed_to <package>` (the
chooser's `IntentSender` callback for this very operation), `cancelled` (a picker closed
without a place), `failed` or `unknown` (a share sheet that named no app — which does not
prove nothing was taken). Receipt `phone_export_outcome`. **No receipt ever says «sent».**
If the report never arrives, the PC shows «outcome not reported».

**Export PIN window.** Save and Share need a fresh PIN check (the same PIN) no
older than 5 minutes, measured with `elapsedRealtime`, bound to this process. It
ends on background, screen off, restart, lock, forget, new pairing or revoke. A
preparation still running when that happens opens nothing and writes nothing. The
one operation whose picker or share sheet is already open may finish, but it never
keeps the rest of Vault unlocked: coming back from it asks for the entry PIN. Before each batch the confirm screen lists the
files and says: «These copies leave Vault. Its PIN and revoking this phone no longer
protect them.»

**From an offline copy.** Exporting a file that is already «On this phone» still
needs green and a grant (the PC re-checks it is in Staging with the same `sha256`);
the verified bytes come through that grant again, so every copy that leaves Vault
has exactly one PC receipt of its own.

**The system picker and the share sheet** take Vault to the background: the operation
they belong to finishes, the export window ends, and the next take-out asks for the PIN
again. A picked app is receipted as `handed_to <package>`; backing out as `cancelled`.

Browser version: offline, Save and Share are app-only in this series.

The module docstring line «there is no export endpoint» and the test
`test_the_page_is_served_and_the_gate_is_not_on_the_phone` change meaning: the
desk Export (any selection, Gatekeeper) stays desk-only; the phone gets exactly
the Staging grant path above and nothing wider.

## 5. Setup wizard over standard Tailscale

One module `vault_v2/setup.py` with a small interface — `check() → [Step]`,
`apply(step_id)`, `undo(step_id)` — and a CLI adapter that tests can replace.
The window only renders its answer. Steps and what proves each:

| Step | Proof |
|---|---|
| Tailscale installed | `tailscale version` answers |
| Signed in | `status --json` → `BackendState == Running`, `Self.Online` |
| HTTPS certificates | `CertDomains` non-empty **and** an HTTPS request to the name answers |
| Vault served | `serve status --json` has exactly our route to the Vault port |
| Phone app installed and paired | a device paired through §1 in this wizard session (not a new Tailscale node) |

- States: `done`, `needs_you` (with the one action and a link/picture), `unknown`
  (could not tell), `not_started`. Never a red «broken» for «waiting for a person».
- **Serve.** Applied only on the owner's press. Read the config first: our exact
  route present → reuse, not ours; a conflicting route on 443 → stop and explain,
  never overwrite. Undo removes only a route the wizard added and only if it is
  unchanged since. Forbidden: `serve reset`, `down`, `logout`, Funnel, preference
  changes. The wizard says the route persists across restarts (`--bg`).
- Without HTTPS: the PC side works; the phone step stays `needs_you` with the admin
  link; no silent switch to HTTP. Undoing the route leaves the vault on this PC only
  (`front: "local"`, bound to localhost), never a plain-HTTP tailnet door.
- The whole HTTPS 443 listener is read (proxy handlers, other handler kinds, the TCP
  entry, Funnel). Unreadable, Funnel on, or anything besides exactly our `/` route →
  not done, not changed. Undo compares a fingerprint of the whole listener.
- The intention is recorded under the vault's write guard before Tailscale is touched;
  the command runs outside the guard; the outcome is recorded after. A crash in between
  shows «pending» with a button to settle it.
- `/api/ping` also names the `vault_id`, so the probe tells this vault from another of
  the same name. The phone step counts only phones paired in this wizard. The wizard says the machine name will appear in
  public Certificate Transparency logs before the owner enables it.
- **APK download.** `GET /app/vault.apk` without a key serves one fixed signed release
  file from the program folder, nothing else; it carries no key or setting.
- The wizard re-checks everything when reopened; closing it midway breaks nothing.

## 6. Root and runtime

- `default_root()`: `VAULT_V2_ROOT` if set; else, when running from an installed
  runtime (a `vault-install.json` next to the package), `%LOCALAPPDATA%\VaultV2\data`;
  else the development `./data`. Program files and the vault root never share a folder;
  uninstall and update never delete the root.
- Child workers (PDF, OCR) start through one function `runtime.worker_python()` that
  returns a real interpreter (`python.exe` beside the embedded runtime), never
  `sys.executable` of a frozen launcher. Isolation flags, deadlines and output limits
  stay as they are.

## 7. Updates and signing

- **APK signing.** One release keystore, created once, held by the owner outside the
  repository and this workspace's files; the build reads its path and passwords from
  the environment. `applicationId` stays `com.vault.phone`; `versionCode` only grows.
  The owner's phone today runs a debug-signed build: moving to the release signature
  needs one uninstall/reinstall and a re-pair, done only with his yes.
- **Release manifest.** `vault-release.json` per `docs/release-format-v1.md` (strict:
  unknown fields refused, sizes bounded, file a bare name); signed with an ECDSA P-256
  release key (SHA256withECDSA, DER, base64 in `vault-release.json.sig`), the owner's.
  P-256 rather than Ed25519 because Android checks Ed25519 natively only from API 33. The verifying public key is compiled into the PC program and the APK —
  never taken from the update itself.
- **PC.** Checks the configured channel, verifies signature, version (no downgrade),
  size and hash, downloads, asks, makes a verified backup of the root before a data
  migration, installs on the owner's press. No automatic rollback over migrated data.
- **Phone.** The same manifest check, rule for rule and in the same order as the PC's
  `verify_release` (whole manifest valid first; then platform, newer, API). One update at a
  time; each download has its own folder; the bytes are checked again as they are handed to
  the system installer. Then the system installer (Android asks for
  confirmation; installing over requires the same signature). The PC also offers the
  current APK at `/app/vault.apk`.
- Tailscale and Ollama update through their own mechanisms. A public release
  channel is a separate decision of the owner; until then updates are tested on a
  local signed channel.

## 8. Slices and acceptance

| # | Slice | Who |
|---|---|---|
| 0 | This contract | Claude |
| 0P | Minimal embedded-Python runtime: Qt window, PDF and OCR workers on synthetic files | Codex |
| 1 | Devices, pairing, revoke, session, indicator, legacy key | Claude |
| 2 | PIN, lock, delays, wipe, recents, backup rules | Claude |
| 3 | Available offline (grant path, «On this phone») | Claude |
| 4 | Save / Share via the same grant path, outcome report | Claude |
| 5 | Setup wizard | Claude |
| 6 | Signed updates (Windows contract Codex, Android Claude) | both |
| 7 | Windows installer | Codex, accepted by Claude |
| 8 | One PC+APK set with hashes, full suite, synthetic end-to-end, owner checklist | Claude |

Every slice: acceptance written first, synthetic data only, focused tests, a narrow
diff review by Codex, the full suite run by Claude before merge. The 9-wrong wipe
and a broken update are exercised on a synthetic profile, never on the owner's phone
data. Clean-machine install is checked separately; if it cannot be, the result is
reported as INSTALL_UNVERIFIED, not «ready for new users».

Out of scope: an embedded network stack, app stores, public publishing, mail
integration for the agent, new sensors, new models.
