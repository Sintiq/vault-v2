# Vault V2 0.1.0

Free, voluntary Windows and Android download. Original Vault source is MIT;
third-party components keep their own licenses. There is no account or paid
subscription for Vault itself. Tailscale and any optional external service have
their own accounts and terms.

## Install

1. Download **vault-v2-0.1.0-windows.exe** from the [0.1.0 release](https://github.com/Sintiq/vault-v2/releases/tag/v0.1.0).
2. Run it in your Windows account. Review the displayed Microsoft runtime
   component terms; decline if you do not accept them. Installation is per-user.
3. Open Vault. Follow its phone setup wizard if you want phone access: install
   and sign in to the official Tailscale app on PC and phone, then pair using QR.
   The Windows package already carries the signed Android companion.
4. Set a phone PIN. PIN locking is not a claim that the complete PC archive is
   encrypted at rest. Keep your Windows account and disk protected and make backups.

Windows has no paid Authenticode certificate, so an unknown-publisher or
SmartScreen warning may appear. Do not disable system protection. If you do not
trust the download, do not run it. The release includes exact hash/signature
sidecars, and the built-in update path verifies its pinned release key and bytes.

The optional standalone Android asset is **vault-26092821.apk** (Android10+).
Android requires your explicit installation approval; do not uninstall an
existing same-signer Vault merely to update, because uninstalling can remove data.
Only accept downloads from the official release and matching signing identity.

## Updates and privacy

Updates are voluntary: use Check for update and review the offered version before
installation. Windows checks the signed [public channel](https://sintiq.github.io/vault-v2/releases/windows/vault-release.json);
Android receives signed updates from its paired PC. No update is forced.

Local document work uses local Ollama, which is not bundled. Its absence is
displayed; optional model downloads need separate installation and resources.
The explicit Claude fallback uses a cloud service and is not the local-only mode.
Do not send sensitive content to an external service unless you choose to.
The agent prepares documents; a person decides whether to save or share them.

## Source and dependency material

This repository is a clean reviewed source snapshot, not workstation Git history.
SOURCE-INVENTORY.json identifies the frozen original source. The release assets
also include the exact source ZIP and recipient material ZIP, including dependency
notices and qualified Qt/cysignals build/replacement routes. Their historical
"draft" names and source-completeness limitations are intentionally retained;
they are not a universal legal or complete-third-party-source certification.

Installed Windows and physical-phone update paths were exercised on synthetic
data; Windows inventoried files and the installed Android APK matched their
references. This is an initial0.1.0 release, not proof against every hostile race,
power-loss scenario, or data-loss case. Back up before installing or updating.
