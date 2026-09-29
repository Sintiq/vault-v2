# Start here — Vault V2

This guide is for using the ready-made **0.1.0** release, not for building code.
The interface is in English. Start with a harmless sample, then add your own
documents once you are comfortable.

[Back to the project](README.md) · [Download page](https://github.com/Sintiq/vault-v2/releases/tag/v0.1.0)

## 1. Install on your Windows PC

You need a 64-bit Windows PC. This first release was tested on Windows 11.
You do not need Python, Visual Studio or a GitHub account to download it.

1. **[Download the Windows installer](https://github.com/Sintiq/vault-v2/releases/download/v0.1.0/vault-v2-0.1.0-windows.exe)**
   (`vault-v2-0.1.0-windows.exe`). You do not need the source or recipient ZIPs
   just to use the app.
2. Run it under the Windows account that will use Vault. Read the displayed
   Microsoft runtime component terms. Accept only if you agree; declining exits
   the installer. Installation is per-user.
3. Open the Start menu, type **Vault V2**, and open the app.

**About Windows warnings:** this release has no paid Authenticode publisher
certificate, so Windows may show “Unknown publisher” or a SmartScreen warning.
Use only the official release above. Do not disable antivirus or system
protection. If you are unsure about a download, stop rather than running it.
The release has hash/signature sidecars; these are not a Windows publisher
certificate.

The installer includes the runtime, PDF/OCR components and signed phone app.
Ollama, its model and Tailscale are separate optional installations.

## 2. Try the archive first

1. Click **Upload…** and pick a non-sensitive sample file. “Upload” here means
   copying into this PC's Vault, not uploading to a public website.
2. Find it in **Staging**. Double-click a supported file to preview it inside
   Vault. Unsupported types show file information instead.
3. Use **F5 Copy** or **F6 Move** to put it in another pane. Right-click a file
   for **Shelf**; use **Shelf → Manage shelves…** to manage categories.
4. Try **F8 Trash**, then **Trash…** to restore the sample. Do not use permanent
   deletion on anything important while learning.

The panes are **Staging** (incoming and prepared files), **Documents** and
**Personal**. You choose their organization. Local **Sort…** offers proposals;
review them before confirming. AI can make mistakes.

## Add local AI (optional)

The archive itself does not require an AI model. To enable the local assistant:

1. Install and start [Ollama for Windows](https://ollama.com/download/windows).
2. Open **PowerShell** from the Start menu and run this one command:

   ```powershell
   ollama pull llama3.1:8b
   ```

3. Wait for the download to finish. Leave Ollama running and reopen Vault if its
   local-model status has not refreshed. The default selected model is exactly
   **`llama3.1:8b`**; installing another model does not replace it automatically.

The [model](https://ollama.com/library/llama3.1:8b) needs several GB of storage
and additional memory to run. Download size is not its total RAM/VRAM use.
There is no promise of fast responses on every PC. The initial model download
uses internet; Vault's default document-model connection is to local Ollama.

**Ask across the archive:** Ask can use saved document cards across the panes.
Staging permits local text reading. For text in other folders, right-click the
folder and choose **Allow agent to read text here**. Text preparation runs in the
background; “still being read” means you can try Ask again after it finishes.
Other folders stay cards-only for Ask.

The explicit Claude Code **chat-only** option is separate and uses a cloud
service. It is not an automatic fallback and does not receive document Sort/Ask
requests. Keep it closed if you want local-only model use. Do not paste personal
data into cloud chat unless you choose to send it there.

## Connect your Android phone (optional)

You need Android 10 or newer, internet for initial setup, and your PC running.
Tailscale is a separate service: its login is not a Vault login.

1. Install the official [Tailscale for Windows](https://tailscale.com/download/windows)
   and [Tailscale for Android](https://play.google.com/store/apps/details?id=com.tailscale.ipn).
   Sign in to the same personal Tailscale network on both. Approve Android's
   VPN connection when Tailscale asks.
2. In the PC Vault toolbar, open **Setup…**. Some toolbar buttons show only an
   icon: hover over them to see their name. Follow the wizard's checks and the
   links it shows. It may ask you to enable HTTPS certificates in Tailscale's
   admin page. Before doing that, read its warning: the machine's certificate
   name will appear in public Certificate Transparency logs, not your documents.
3. When the wizard offers the **download QR**, scan it on your phone to download
   the Vault companion. Approve installing this specific app when Android asks.
   Alternatively, use [the APK in the official release](https://github.com/Sintiq/vault-v2/releases/download/v0.1.0/vault-26092821.apk).
4. On the PC, click **Add phone…** for a **new pairing QR**. Scan it with the
   phone's camera, or paste its link into the Vault app's Connect screen. Compare
   the code shown on phone and PC, then confirm on the PC only if they match.
5. Set a **6–12 digit PIN** in the phone app. Keep it somewhere safe. Wait for
   the green **PC online** indicator before trying live access.

The first QR downloads the app; the second pairs it. They do not replace
Tailscale login. Pairing tickets expire, so reopen **Add phone…** if needed.
Do not post a pairing QR online or send it to anyone else.

Keep the PC awake, Vault open and Tailscale running on both devices for live
access. Previously prepared **Available offline** copies can be read without
the PC. Saving or sharing even an offline copy still requires PC online.

**Taking a file to the phone starts in Staging.** Available offline, Save and
Share are offered for Staging files while **PC online** is green. For a file in
Documents or Personal, first copy it to Staging on the PC.

To add another phone or revoke a lost one, open **Phone…** on the PC (shortcut
**Ctrl+P**). The **Phone and other devices** window lists paired devices and
offers **Add phone…**, **Rename…** and **Revoke**. Revoking blocks future Vault
access; it does not erase copies already saved or shared elsewhere.

## PIN, backup and safe sharing

- The phone asks for its PIN on entry and again for saving/sharing. **Nine wrong
  PIN attempts erase Vault's data on that phone.** They do not erase PC originals
  or files already saved/shared outside the app. If you forget the PIN, see the
  problem guide below rather than repeatedly guessing.
- Phone PIN locking is **not** encryption of the whole PC archive. Protect your
  Windows account and disk separately. Health records are not medical advice.
- Keep originals or another verified backup while you learn. **Backup…** creates
  an encrypted archive, currently limited to **512 MiB of included file data**.
  Larger vaults need a separate checked backup solution. Keep the backup password
  safely: a forgotten password has no recovery shortcut.
- Phone **Available offline** keeps a copy inside the PIN-protected Vault app.
  **Save a copy to Files…** and **Share…** take copies outside Vault. The chosen
  place or app may be a cloud service; Vault's PIN and device revocation no longer
  protect those copies. You choose the destination and press the final button.
- Local-first does not mean no network use. Paired-phone access, configured
  reminders, chosen exports and an explicitly opened cloud-chat option can
  transfer data. No agent sends your documents to a recipient on your behalf.

## Updates

On Windows, use **Vault → Check for updates…**. This closes the archive normally
and opens the separate updater. Review the offered version; an equal or older
version is not offered as a new update. Nothing installs automatically.

Android updates come from the paired PC and also require your approval. Do not
uninstall Vault merely to update: that can remove phone data. Only use the
official release with the matching app signing identity. Back up important data
before installing or updating.

## If something does not work

| What you see | What to do next |
|---|---|
| Local agent unavailable | Start Ollama. If Vault names a missing model, run the exact `ollama pull` command it shows. You can still use the archive without AI. |
| Red “PC offline” | Check the PC is awake, Vault is open and Tailscale is running on both devices in the same network. It is not a sign that your documents were deleted. |
| QR or pairing expired | Open **Add phone…** again and use the new pairing QR. Confirm the matching code on the PC. |
| “Vault is busy” | Let the current operation finish, then retry. Do not launch repeated copies of the operation. |
| Read-only second window | Close the first Vault window normally, then reopen the second if needed. Do not delete its lock file to force writes. |
| PDF has no preview or a form is incomplete | Some formats, including XFA forms, are unsupported. Keep the original. Do not treat an incomplete preview as proof that the file is empty. |
| Forgotten phone PIN | Re-pairing requires resetting the phone app's Vault data and revoking its old pairing on the PC. This discards app-local copies; check your PC originals first. There is no PIN recovery shortcut. |
| No update available | You may already have the current version. Updates are voluntary, and this first release has no promise of a regular schedule. |

Still stuck? [Report a problem](https://github.com/Sintiq/vault-v2/issues/new?template=bug_report.md).
Give the app version, Windows/Android version, what you tried and the exact
error wording. **Issues are public:** do not upload real documents, PINs, tokens,
pairing QR codes, private paths or unredacted logs. Use a synthetic sample.

**Resetting a forgotten phone PIN:** first check your originals on the PC and
revoke the old phone pairing in **Phone…**. On Android, open **Settings → Apps →
Vault → Storage** and choose **Clear data / Clear storage** (wording varies by
phone). This deletes Vault's app-local copies and settings. Reopen the app,
pair again from the PC and set a new PIN. Clearing only the cache does not
reset the PIN.

## Source and release notes

[Release 0.1.0](https://github.com/Sintiq/vault-v2/releases/tag/v0.1.0) includes
the Windows installer, optional standalone APK, signed manifest sidecars,
source ZIP and dependency/recipient material ZIP. Historical “draft” ZIP names
are retained. Original Vault code is [MIT-licensed](LICENSE); third-party
components keep their own terms. Vault itself has no account or paid subscription;
external services have their own accounts and terms.

The tag and source ZIP preserve the initial snapshot without workstation Git
history. `SOURCE-INVENTORY.json` describes that frozen export, not later edits
to this guide. Installed Windows and physical-phone update paths were exercised
on synthetic data, but this initial release does not prove safety against every
crash, hostile race or data-loss scenario. See the [developer reference](DEVELOPMENT.md)
if you want to inspect or build the code.
