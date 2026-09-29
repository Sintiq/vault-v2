# Vault V2 — Android companion

Files, Chat, Tasks, Health and Watch connect to the owner's desktop Vault.
This is a qualification build, not a claim that the release has completed
clean-device, dependency-license or privacy acceptance.

## Pair your own phone

Use the desktop setup wizard with standard Tailscale on both devices. The
normal setup path requires Tailscale HTTPS; it does not silently downgrade to
HTTP. The Android client still understands the explicitly configured legacy
HTTP/shared-key path for migration. Do not copy development addresses or keys.

On the PC choose **Phone… → Add phone…**. Scan the pairing QR with the phone's
camera, or paste the complete `vault-pair://` link into the connection screen.
Compare the six digits on both devices and confirm on the PC. The QR contains
a short-lived, single-use ticket, not the long-lived device token. The desktop
does not call the phone paired merely because it appears in Tailscale.

The connection indicator says **PC online** only after a recent authenticated
answer from the expected Vault. **PC offline** and **Update needed** are not
permission to read/export; each request still has its own checks.

Manage and revoke devices on the PC. Revocation refuses future protected
requests, but cannot recall files already saved, shared or copied offline.

## PIN and local copies

After pairing, choose a 6–12 digit PIN. Vault asks for it when entering the app
and again for Save/Share. Backgrounding, screen-off and restart lock the app.
Wrong attempts trigger delays; the ninth wrong PIN erases this phone's Vault
state. It does not erase desktop originals, other apps or previously exported
files. Forgetting the PIN requires clearing the phone's Vault state and pairing
again; there is no phone-side PIN reset that bypasses ownership at the PC.

The app stores more than just a connection address: its protected local state
includes device credentials, PIN verification data, user-requested offline
copies, temporary exports and agent-door state/logs. Device credentials use
Android Keystore protection. The PIN is an app gate, not a claim of protection
against a rooted phone, a modified app or privileged debugging.

## Take a file from Staging

The person chooses the action; the agent does not send documents. While the
desktop is online and authorizes the request, Staging offers:

- **Available offline**: a verified copy inside Vault's app-private storage.
- **Save a copy to Files…**: the Android system picker chooses the destination.
  It may be a cloud document provider; this is not necessarily local-only.
- **Share…**: Android's chooser hands a read-only copy to the chosen app.

Save and Share require a fresh PIN check and a visible confirmation that the
copy leaves Vault. The export PIN window expires after at most five minutes
and on background/lock. An already opened system picker may finish its one
operation; returning to Vault still requires entry unlock.

Offline copies can be read without the PC, but exporting them still needs a
fresh PC grant. The PC checks the current Staging file and digest; a partial
download is never marked ready. Receipts distinguish saved, handed to an app,
cancelled, failed, unknown and unreported outcomes. A handoff is not proof that
an email was sent or delivered. Revocation and Vault's PIN no longer protect
copies outside Vault.

## Watch and Health

With the owner's Health Connect permission, Watch reads data made available by
compatible phone/watch apps. It can show today's readings and add selected
days to the desktop Health timeline on the owner's action. Missing permission,
provider, source data or a failed read is reported, not replaced with invented
measurements. These are recorded readings, not diagnosis or emergency monitoring.

## Optional agent door

The separate phone door starts closed. When explicitly opened, it binds to
the phone's Tailscale address; it does not fall back to a public/LAN listener.
Its capability gates, time-limited input session and receipts are separate from
document pairing/export. Location and screenshots require their own approvals;
Android's screen-capture permission is not bypassed. Typing, reading other
apps' accessibility contents and disabling security are not offered.

The PC-side adapter is `tools/phone_mcp/server.py`. Configure it only with the
address and key shown by your own phone, under your Vault's `.door/config.json`.
Keep that file out of source control and never publish a real key. A local
`.gitignore` is a safeguard, not proof that a secret was never committed.

## Build and test

Developer build requires a compatible JDK (17 or 21), Android SDK platform 34
and Build Tools 36.0.0 as pinned in `app/build.gradle.kts`. Gradle/Android/Kotlin
versions come from the checked-in wrapper and version catalog. Set `JAVA_HOME`
to your own JDK and `ANDROID_HOME` to your own SDK directory; no owner's paths
or network addresses are needed. From this `phone` directory:

```powershell
.\gradlew.bat :app:testDebugUnitTest :app:assembleDebug
adb devices
adb -s <your-device-serial> install -r app\build\outputs\apk\debug\app-debug.apk
```

Authorize USB debugging yourself on the intended test device. Explicitly select
its serial if multiple devices/emulators are present. Do not enable wireless ADB
just to use Vault: normal app traffic uses the paired Tailscale connection.
Run acceptance on synthetic data and photograph only this app, never the
notification shade, other applications or private documents.

Release signing uses explicit environment inputs documented in the Gradle file;
never put a private APK keystore/password in the repository. The release-update
public key is a separate trust input. A missing key disables update checks; it
must not disable signature verification. Updates require user confirmation and
Android's installer; the app does not silently install them.

## Licenses in the Android package

Original Vault code is MIT-licensed. The APK also contains third-party license
texts and its runtime component index under `assets/licenses/`; source copies
are in [`app/src/main/assets/licenses`](app/src/main/assets/licenses).
These dependency terms remain separate from Vault's MIT license. Preserve the
complete notice directory and OkHttp's Public Suffix List notice when packaging
or redistributing. Qualification artifacts are not automatically cleared for
public distribution merely because they build successfully.

The desktop/phone behavioural contract is in
[`docs/CONTRACT-devices-pin-export-setup.md`](../docs/CONTRACT-devices-pin-export-setup.md),
and the shared signed-release format is in
[`docs/release-format-v1.md`](../docs/release-format-v1.md).
