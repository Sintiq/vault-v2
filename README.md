# Vault V2

Keep your documents together on your Windows PC. Organize them, find what you
need, and take selected files with you on a paired Android phone.

Free download · English interface · Optional local AI · First release: **0.1.0**

## Download and get started

**[Download for Windows](https://github.com/Sintiq/vault-v2/releases/download/v0.1.0/vault-v2-0.1.0-windows.exe)**

[Step-by-step setup](START-HERE.md) · [All downloads and release notes](https://github.com/Sintiq/vault-v2/releases/tag/v0.1.0) · [Project website](https://sintiq.github.io/vault-v2/)

1. Download and run the Windows installer. Review its component terms before
   accepting. Use your own Windows account.
2. Open **Vault V2** from the Start menu. Try **Upload…** with a non-sensitive
   sample file first.
3. Use the archive on your PC. Add local AI or phone access later, if you want.

**No Python, Visual Studio, or source-code setup is needed.** The installer
includes the app's runtime, PDF/OCR components and signed Android companion.
It does **not** include Ollama, an AI model or Tailscale.

This Windows download has no paid publisher certificate. Windows may show an
unknown-publisher or SmartScreen warning. Do not disable protection; if you do
not trust the file, do not run it. This is an initial release: keep backups of
important documents.

## What you can do

- **Bring files together.** Import documents into Staging, then copy or move
  them between Staging, Documents and Personal.
- **Organize and find.** Use folders, shelves and filters. Optional local AI
  suggests categories and helps with document questions; you review its work.
- **Read without leaving Vault.** Preview supported photos, PDFs and text files.
  Local OCR can extract text from scans; some PDF forms have limitations.
- **Keep useful records.** Use document cards, tasks and health records alongside
  the archive. Health information is not a diagnosis or medical advice.
- **Take selected files to your phone.** View files; for offline copies,
  **Save a copy to Files…** or **Share…**, put the files in Staging first.
  You choose each action yourself while the PC is online.
- **Review changes.** Receipts record app operations; Trash allows restoration.
  An encrypted backup tool is available, with a size limit explained in the guide.

The agent helps prepare documents. **A person chooses whether to save or share
them. It does not send them to a doctor, accountant or anyone else for you.**

## Choose the setup that suits you

### Just the archive

Install Vault and start using files, previews, folders, Trash and backup.
There is no Vault account or subscription. You can leave both AI and phone
access unconfigured.

### Add local AI — optional

Install [Ollama for Windows](https://ollama.com/download/windows) separately,
then download the default model, `llama3.1:8b`.
[Follow the short instructions](START-HERE.md#add-local-ai-optional).
Model downloads need internet, disk space and enough memory; performance depends
on your PC. Vault does not silently switch to a different model or to cloud AI.

### Add your Android phone — optional

Install the official Tailscale app on PC and phone and sign in to the same
personal network. In Vault, open **Setup…**, follow its checks, install the
companion from the download QR, then pair using the pairing QR and matching code.
[See the phone walkthrough](START-HERE.md#connect-your-android-phone-optional).

The companion requires Android 10 or newer. Set its PIN after pairing. Your PC
and Vault must be running for live access; only copies previously made available
offline can be read without the PC. This is an Android companion, not an iPhone app.

## Privacy, control and updates

- **Local-first is not “nothing ever leaves the PC.”** Local document AI uses
  Ollama. Paired-phone access, configured reminders and your exports transfer
  selected data. Tailscale is a separate networking service with its own account
  and terms.
- **Cloud chat is a separate choice.** The explicitly selected Claude Code chat
  option uses an external service. It is not local-only and never opens
  automatically; document Sort/Ask are not routed through it.
- **Phone access is yours to manage.** Open **Phone…** on the PC to add, rename
  or revoke paired devices. Revoking cannot recall files already saved or shared.
- **PIN locking is not full-disk encryption.** Protect your Windows account and
  disk. Read the [PIN and backup notes](START-HERE.md#pin-backup-and-safe-sharing)
  before storing important data.
- **Updates are your choice.** Use **Vault → Check for updates…** on Windows.
  Signed update manifests are checked; nothing installs automatically. Release
  signatures are separate from Windows publisher certification.

## Need help?

Start with [common problems and their next steps](START-HERE.md#if-something-does-not-work).
Still stuck? [Report a problem](https://github.com/Sintiq/vault-v2/issues/new?template=bug_report.md)
or [suggest an improvement](https://github.com/Sintiq/vault-v2/issues/new?template=feature_request.md).

GitHub issues are public. **Do not attach personal documents, pairing QR codes,
PINs, keys or unredacted logs.** Describe the problem with a harmless sample.
Community reports are welcome; there is no guaranteed support response time.

## Source, licenses and technical details

Want to inspect, modify or build Vault? See the [developer reference](DEVELOPMENT.md)
and [technical documents](docs/). Original Vault code and documentation are
[MIT-licensed](LICENSE); bundled third-party components retain their own licenses.

The `v0.1.0` tag and release source ZIP preserve the initial, history-free source
snapshot. [SOURCE-INVENTORY.json](SOURCE-INVENTORY.json) describes that frozen
export, not later documentation updates on `main`. Release assets also include
dependency notices and recipient materials; historical “draft” filenames and
source-completeness limitations are retained, not hidden.
