"""Backup of the whole vault to a removable drive — encrypted, verified, receipted.

Why this shape:
- one file per backup, `vault-YYYYMMDD-HHMMSS[-N].vault`: a zip of the vault
  root, encrypted with AES-256-GCM under a key derived from a passphrase
  with scrypt. A flash drive is the thing most easily lost; what is on it
  must be useless without the passphrase;
- the passphrase is the owner's and lives nowhere — not on the drive, not on
  the PC. A key file on the PC would die with the PC, which is the failure a
  backup exists for;
- every backup is read back and decrypted before it is called done, and the
  receipt carries the digest of what actually landed on the drive;
- a RESTORE.md and a standalone restore script go next to the archives, so
  getting the documents back never depends on this app still existing.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import secrets
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import backup_restore
from .backup_restore import (
    BackupError, MAGIC, SALT_LEN, NONCE_LEN, _derive, decrypt,
    restore_archive, verify_archive,
)
from .receipts import ReceiptLog

MIN_PASSPHRASE = 8
# Temporary policy while ZIP, encryption and readback hold multiple buffers.
# This bounds included source bytes, NOT the process's peak RAM usage.
MAX_BACKUP_INPUT_BYTES = 512 * 1024**2
BACKUP_SIZE_ERROR = (
    "Backup exceeds the 512 MiB limit for uncompressed included file bytes. "
    "Reduce the Vault size or wait for streaming backup support."
)

# Not copied: exports are already outside the vault by design, and the
# incoming folder is a transient landing spot for phone uploads.
SKIP_PATHS = {Path(".exports"), Path(".api") / "incoming"}


@dataclass(frozen=True)
class BackupResult:
    path: Path
    size: int
    sha256: str
    files: int
    verified: bool


def _included_files(root: Path) -> Iterator[Path]:
    """One inclusion rule shared by the size preflight and the archive."""
    def refuse_unreadable(error: OSError) -> None:
        raise error

    for dirpath, dirnames, filenames in os.walk(root, onerror=refuse_unreadable):
        dirnames[:] = sorted(
            d for d in dirnames
            if (Path(dirpath) / d).relative_to(root) not in SKIP_PATHS
        )
        for name in sorted(filenames):
            path = Path(dirpath) / name
            # Runtime ownership belongs to the current process, not a restore.
            if path.relative_to(root) != Path(".vault.lock"):
                yield path


def _check_input_size(root: Path) -> None:
    total = 0
    for path in _included_files(root):
        total += path.stat().st_size
        if total > MAX_BACKUP_INPUT_BYTES:
            raise BackupError(BACKUP_SIZE_ERROR)


def _zip_vault(root: Path, log: ReceiptLog | None = None) -> tuple[bytes, int]:
    """The vault root as a zip in memory, with a manifest of digests."""
    log = log or ReceiptLog(root / ".receipts")
    with log.write("backup snapshot"):
        _check_input_size(root)
        return _zip_contents(root)


def _zip_contents(root: Path) -> tuple[bytes, int]:
    buf = io.BytesIO()
    manifest: dict[str, str] = {}
    count = 0
    input_bytes = 0
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for p in _included_files(root):
            rel = p.relative_to(root).as_posix()
            if rel.split("/", 1)[0].casefold() == "manifest.json":
                raise BackupError("MANIFEST.json at the vault root is reserved for backup metadata")
            # A source may grow after preflight. Bound even that read; do not
            # allocate an unbounded file before discovering the limit breach.
            with p.open("rb") as stream, io.BytesIO() as file_buffer:
                while block := stream.read(min(1024**2, MAX_BACKUP_INPUT_BYTES - input_bytes + 1)):
                    input_bytes += len(block)
                    if input_bytes > MAX_BACKUP_INPUT_BYTES:
                        raise BackupError(BACKUP_SIZE_ERROR)
                    file_buffer.write(block)
                data = file_buffer.getvalue()
            manifest[rel] = hashlib.sha256(data).hexdigest()
            z.writestr(rel, data)
            count += 1
        z.writestr("MANIFEST.json", json.dumps(manifest, indent=1, sort_keys=True))
    return buf.getvalue(), count


def encrypt(plain: bytes, passphrase: str) -> bytes:
    salt = secrets.token_bytes(SALT_LEN)
    nonce = secrets.token_bytes(NONCE_LEN)
    key = _derive(passphrase, salt)
    return MAGIC + salt + nonce + AESGCM(key).encrypt(nonce, plain, MAGIC)


def make_backup(root: Path, dest_dir: Path, passphrase: str, log: ReceiptLog | None = None) -> BackupResult:
    if len(passphrase) < MIN_PASSPHRASE:
        raise BackupError(f"the passphrase needs at least {MIN_PASSPHRASE} characters")
    root, dest_dir = Path(root), Path(dest_dir)
    if not root.is_dir():
        raise BackupError(f"no vault at {root}")
    guard_log = log or ReceiptLog(root / ".receipts")
    plain, files = _zip_vault(root, guard_log)
    # A freshly made archive must satisfy the same recovery contract as an old
    # one before any encrypted output is published on the backup drive.
    verify_archive(plain)
    blob = encrypt(plain, passphrase)
    # Encryption does not block writers. Recheck before writing the external
    # result, and keep those effects together with the completion receipt.
    with guard_log.write("backup output"):
        guard_log.effect()
        return _write_backup(root, dest_dir, passphrase, plain, blob, files, log)


def _write_backup(root: Path, dest_dir: Path, passphrase: str, plain: bytes,
                  blob: bytes, files: int, log: ReceiptLog | None) -> BackupResult:
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    for attempt in range(1000):
        suffix = f"-{attempt}" if attempt else ""
        out = dest_dir / f"vault-{stamp}{suffix}.vault"
        try:
            stream = out.open("xb")
        except FileExistsError:
            continue
        with stream:
            stream.write(blob)
        break
    else:
        raise BackupError("cannot create a unique backup name; choose another backup folder")

    # Read it back from the drive and decrypt it: "written" is not "saved".
    landed = out.read_bytes()
    digest = hashlib.sha256(landed).hexdigest()
    verified = decrypt(landed, passphrase) == plain
    if not verified:
        out.unlink(missing_ok=True)
        raise BackupError("the copy on the drive did not read back correctly; nothing kept")

    _write_restore_notes(dest_dir)
    if log is not None:
        log.append("backup", root, out, sha256=digest, size=len(landed),
                   note=f"{files} files, encrypted, read back and verified",
                   extra={"files": files, "verified": True})
    return BackupResult(out, len(landed), digest, files, verified)


def verify_backup(path: Path, passphrase: str) -> int:
    """Check the exact archive contents, paths and digests. Returns file count."""
    plain = decrypt(Path(path).read_bytes(), passphrase)
    return verify_archive(plain)


def restore_backup(path: Path, passphrase: str, into: Path) -> int:
    """Unpack into an empty folder. Never over the live vault by accident."""
    # A single immutable snapshot serves both preflight and extraction.
    plain = decrypt(Path(path).read_bytes(), passphrase)
    return restore_archive(plain, into)


RESTORE_NOTES = """# Restoring a vault backup

Each `vault-*.vault` file here is the whole vault — documents, cards, tasks,
health timeline, receipts, trash — zipped and then encrypted with AES-256-GCM
under a key derived from the passphrase (scrypt, N=32768, r=8, p=1).

The encrypted archive includes access keys (`.api/key` and `.door/config.json`, when present); restoring them lets the Vault accept the same paired phone, so protect the archive and its passphrase.

Without the passphrase the file is noise. Nothing on this drive or on the PC
holds it.

## With the vault app

The Backup window creates and verifies archives. To restore to an empty folder,
use the standalone script below (the application's Python restore procedure
uses the same checks; there is no Restore button in this window).

## Without the app

`restore_backup.py` next to this file needs only Python 3 and the
`cryptography` package (`pip install cryptography`):

    python restore_backup.py vault-YYYYMMDD-HHMMSS.vault  C:\\path\\to\\empty\\folder

It asks for the passphrase, then checks the whole archive before creating the
destination or writing files: exactly one manifest, exact file membership, no
duplicate names/JSON keys, safe relative Windows-compatible paths, and every
file's CRC and SHA256. Directory/link/special ZIP entries and a link/junction
in the destination path are refused. Only an absent or empty destination is
allowed. Hidden and service files are retained; the unpacked folder is a vault
root. `MANIFEST.json` at the vault root is reserved for backup metadata.

Backup names gain a numeric suffix when occupied; existing .vault and .part
files are never replaced. An interrupted write may leave an unverified .vault:
do not treat presence or a filename as proof that a backup completed.

Preflight prevents partial recovery caused by an invalid archive. It does not
roll back disk/write failures and is not protection against another process
changing destination directories concurrently. Use a folder you control.
The recovery script must be kept with the archive; it needs Python 3.12 or
newer and cryptography, but not Vault or its source files.
"""

# The app ships as Python source. Include the SAME recovery implementation in
# the exported script, not a second validator that can drift. Once exported it
# needs neither this source file nor the app, only Python + cryptography.
RESTORE_SCRIPT = Path(backup_restore.__file__).read_text(encoding="utf-8") + r'''

if __name__ == "__main__":
    import getpass
    import sys

    if len(sys.argv) != 3:
        sys.exit("usage: restore_backup.py BACKUP.vault EMPTY_FOLDER")
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    try:
        plain = decrypt(src.read_bytes(), getpass.getpass("passphrase: "))
        count = restore_archive(plain, dst)
    except (BackupError, OSError) as exc:
        sys.exit(f"restore refused or interrupted: {exc}")
    print(f"restored {count} files into {dst}")
'''


def _write_restore_notes(dest_dir: Path) -> None:
    (dest_dir / "RESTORE.md").write_text(RESTORE_NOTES, encoding="utf-8")
    (dest_dir / "restore_backup.py").write_text(RESTORE_SCRIPT, encoding="utf-8")


def list_backups(dest_dir: Path) -> list[Path]:
    dest_dir = Path(dest_dir)
    if not dest_dir.is_dir():
        return []
    return sorted(dest_dir.glob("vault-*.vault"))
