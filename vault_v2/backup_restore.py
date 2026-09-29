"""Recovery core, also copied verbatim into the self-contained recovery script.

No application imports: recovery needs only Python and cryptography. Archive
validation finishes before destination writes; disk failures are not rolled back.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import zipfile
import zlib
from contextlib import contextmanager
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"VAULTBK1"
SALT_LEN = 16
NONCE_LEN = 12
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1


class BackupError(Exception):
    pass


def _derive(passphrase: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P).derive(
        passphrase.encode("utf-8")
    )


def decrypt(blob: bytes, passphrase: str) -> bytes:
    if not blob.startswith(MAGIC):
        raise BackupError("not a vault backup")
    if len(blob) < len(MAGIC) + SALT_LEN + NONCE_LEN + 16:
        raise BackupError("the backup file is truncated")
    head = len(MAGIC)
    salt = blob[head : head + SALT_LEN]
    nonce = blob[head + SALT_LEN : head + SALT_LEN + NONCE_LEN]
    body = blob[head + SALT_LEN + NONCE_LEN :]
    key = _derive(passphrase, salt)
    try:
        return AESGCM(key).decrypt(nonce, body, MAGIC)
    except InvalidTag as exc:
        raise BackupError("wrong passphrase, or the backup file is damaged") from exc


def _path_parts(name: str) -> tuple[str, ...]:
    """Portable relative file names, with Windows namespace aliases refused."""
    if not isinstance(name, str):
        raise BackupError("invalid path in backup")
    parts = tuple(name.split("/"))
    for part in parts:
        stem = part.split(".", 1)[0].rstrip(" .").upper()
        device = (stem in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
                  or len(stem) == 4 and stem[:3] in {"COM", "LPT"}
                  and stem[3] in "123456789\u00b9\u00b2\u00b3")
        if (not part or part in {".", ".."} or part.endswith((".", " "))
                or device or any(c in '\\:<>"|?*' or ord(c) < 32 for c in part)):
            raise BackupError("unsafe path in backup")
        try:
            part.encode("utf-8")
            if len(part.encode("utf-16-le")) > 510:
                raise BackupError("path component exceeds the Windows filename limit")
        except UnicodeError as exc:
            raise BackupError("invalid path in backup") from exc
    return parts


def _check_names(infos: list[zipfile.ZipInfo]) -> None:
    nodes: dict[tuple[str, ...], tuple[tuple[str, ...], bool]] = {}
    for info in infos:
        if info.orig_filename != info.filename:
            raise BackupError("normalized or truncated ZIP path in backup")
        parts = _path_parts(info.filename)
        mode = stat.S_IFMT(info.external_attr >> 16)
        if mode not in {0, stat.S_IFREG} or info.external_attr & 0x10:
            raise BackupError("backup contains a directory, link or special ZIP entry")
        if info.flag_bits & 1:
            raise BackupError("unexpected encrypted ZIP member inside backup")
        for end in range(1, len(parts) + 1):
            prefix = parts[:end]
            key = tuple(p.casefold() for p in prefix)
            is_file = end == len(parts)
            old = nodes.get(key)
            if old is not None and (old != (prefix, is_file) or is_file):
                raise BackupError("duplicate or conflicting paths in backup")
            nodes[key] = (prefix, is_file)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BackupError("duplicate key in backup manifest")
        result[key] = value
    return result


def _preflight(z: zipfile.ZipFile) -> list[str]:
    _check_names(z.infolist())
    manifest = json.loads(z.read("MANIFEST.json"), object_pairs_hook=_unique_object)
    if not isinstance(manifest, dict):
        raise BackupError("backup manifest must be a file-to-digest object")
    for rel, digest in manifest.items():
        _path_parts(rel)
        if (rel == "MANIFEST.json" or not isinstance(digest, str)
                or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)):
            raise BackupError("invalid file digest in backup manifest")
    if set(z.namelist()) != set(manifest) | {"MANIFEST.json"}:
        raise BackupError("backup ZIP does not exactly match its manifest")
    for rel, expected in manifest.items():
        digest = hashlib.sha256()
        with z.open(rel) as stream:
            while block := stream.read(1024**2):
                digest.update(block)
        if digest.hexdigest() != expected:
            raise BackupError(f"damaged inside the backup: {rel}")
    return list(manifest)


@contextmanager
def _checked_zip(plain: bytes):
    z = None
    try:
        # Only parse/read failures are archive refusals. Errors in the caller's
        # write phase are deliberately NOT relabelled as harmless preflight.
        try:
            z = zipfile.ZipFile(io.BytesIO(plain))
            names = _preflight(z)
        except (zipfile.BadZipFile, ValueError, KeyError, EOFError, OSError,
                NotImplementedError, zlib.error, RecursionError) as exc:
            raise BackupError("invalid or damaged ZIP/manifest in backup") from exc
        yield z, names
    finally:
        if z is not None:
            z.close()


def verify_archive(plain: bytes) -> int:
    with _checked_zip(plain) as (_z, names):
        return len(names)


def _destination(into: Path) -> Path:
    """Check lexical ancestors before following anything; never create here."""
    supplied = Path(into)
    for part in supplied.parts[1:] if supplied.anchor else supplied.parts:
        _path_parts(part)
    into = Path(os.path.abspath(supplied))
    present = False
    for ancestor in (*reversed(into.parents), into):
        try:
            meta = ancestor.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(meta.st_mode)
                or getattr(meta, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise BackupError("restore target contains a link or junction")
        if not stat.S_ISDIR(meta.st_mode):
            raise BackupError("restore target or its ancestor is not a folder")
        if ancestor == into:
            present = True
    if present and next(into.iterdir(), None) is not None:
        raise BackupError(f"restore target is not empty: {into}")
    return into


def restore_archive(plain: bytes, into: Path) -> int:
    into = _destination(into)
    with _checked_zip(plain) as (z, names):
        # Recheck after potentially slow validation and before the first write.
        # This is not a filesystem sandbox against concurrent outside mutation.
        _destination(into)
        targets = [(rel, into.joinpath(*_path_parts(rel))) for rel in names]
        for _rel, target in targets:
            if not target.is_relative_to(into):
                raise BackupError("refusing path outside the restore target")
        into.mkdir(parents=True, exist_ok=True)
        for rel, target in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(rel) as source, target.open("xb") as out:
                while block := source.read(1024**2):
                    out.write(block)
        return len(names)
