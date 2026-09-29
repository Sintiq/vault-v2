"""Local derived-text cache; file names are source-byte SHA256 values."""
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
from uuid import uuid4

from .cards import TEXT_SUFFIXES, content_kind
from .errors import VaultError
from .file_access import visible_file
from .paths import VaultPaths
from .receipts import ReceiptLog, sha256_file

MAX_SOURCE_BYTES = 512 * 1024**2
MAX_TEXT_BYTES = 32 * 1024**2
MAX_METADATA_BYTES = 65536
PLAIN_TEXT_PROFILE = "vault-text-utf8-replace-v1"


@dataclass(frozen=True)
class TextSnapshot:
    text: str
    source_sha256: str
    source_size: int
    extraction_metadata: dict


def _plain_path(path: Path, *, directory: bool = False, missing: bool = False):
    """Inspect every lexical ancestor without following links or reparses."""
    path = Path(path).absolute()
    if ".." in path.parts:
        raise VaultError("Text cache path needs manual review.")
    current = Path(path.anchor)
    for index, part in enumerate(path.parts[1:], 1):
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if missing and index == len(path.parts) - 1:
                return None
            raise
        if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            raise VaultError("Linked text cache paths need manual review.")
        is_directory = index < len(path.parts) - 1 or directory
        if not (stat.S_ISDIR(info.st_mode) if is_directory else stat.S_ISREG(info.st_mode)):
            raise VaultError("Text cache path needs manual review.")
    return info


class DocumentTextCache:
    """Extract immutable bytes outside the guard; publish only a current source.

    A cache hit is provisional, not read authority. StagingReader must recheck
    the visible source and its digest while writing the final agent receipt.
    """

    def __init__(self, paths: VaultPaths, log: ReceiptLog):
        self.paths, self.log = paths, log
        self.directory = paths.root / ".text"

    def read_cached_snapshot(self, source: Path, *,
                             authorize: Callable[[], None] | None = None) -> TextSnapshot | None:
        """Read only a valid existing cache for the current visible source.

        A miss, stale pair or suspicious path is unavailable, never a request
        to extract text or repair the cache. Recheck after validating the pair
        because the source may have changed while the cache was read.
        Authorization refusals propagate; they are never ordinary cache misses.
        """
        from .text_extract import EXTRACTABLE_SUFFIXES, EXTRACTION_PROFILE

        if authorize is not None:
            authorize()
        try:
            source = visible_file(self.paths, source)
            suffix = source.suffix.lower()
            plain_text = content_kind(source) == "TEXT"
            if not plain_text and suffix not in EXTRACTABLE_SUFFIXES:
                return None
            profile = PLAIN_TEXT_PROFILE if plain_text else EXTRACTION_PROFILE
            size = source.stat().st_size
            if size > MAX_SOURCE_BYTES:
                return None
            digest = sha256_file(source)
        except (OSError, VaultError):
            return None
        if authorize is not None:
            authorize()
        try:
            snapshot = self._read_pair(digest, size, suffix, profile)
        except (OSError, VaultError):
            return None
        if authorize is not None:
            authorize()
        if snapshot is None:
            return None
        try:
            current = visible_file(self.paths, source)
            if current.stat().st_size != size or sha256_file(current) != digest:
                return None
        except (OSError, VaultError):
            return None
        if authorize is not None:
            authorize()
        return snapshot

    def read_snapshot(self, source: Path, *,
                      authorize: Callable[[], None] | None = None) -> TextSnapshot:
        """Prepare text outside the guard; reauthorize guarded publication.

        The callback checks current source-path authority before content reads
        and handoff. It must remain safe under the reentrant root write guard.
        """
        from .text_extract import EXTRACTABLE_SUFFIXES, EXTRACTION_PROFILE, ExtractedText, extract_text

        if authorize is not None:
            authorize()
        source = visible_file(self.paths, source)
        suffix = source.suffix.lower()
        plain_text = content_kind(source) == "TEXT"
        if not plain_text and suffix not in EXTRACTABLE_SUFFIXES:
            raise VaultError("This document type has no local text extraction.")
        profile = PLAIN_TEXT_PROFILE if plain_text else EXTRACTION_PROFILE
        if source.stat().st_size > MAX_SOURCE_BYTES:
            raise VaultError("Document exceeds the 512 MiB local text limit.")
        with source.open("rb") as stream:
            data = stream.read(MAX_SOURCE_BYTES + 1)
        size = len(data)
        if size > MAX_SOURCE_BYTES:
            raise VaultError("Document exceeds the 512 MiB local text limit.")
        digest = hashlib.sha256(data).hexdigest()
        if authorize is not None:
            authorize()
        cached = self._read_pair(digest, size, suffix, profile)
        if authorize is not None:
            authorize()
        if cached is not None:
            return cached
        extracted = (ExtractedText(data.decode("utf-8", errors="replace"),
                                   "utf8-replacement", "1", (), 0, 0)
                     if plain_text else extract_text(data, suffix))
        encoded = extracted.text.encode("utf-8")
        if len(encoded) > MAX_TEXT_BYTES:
            raise VaultError("Extracted text exceeds the local cache limit.")
        metadata = {
            "schema": 1, "source_sha256": digest, "source_size": size,
            "source_suffix": suffix, "profile": profile,
            "text_sha256": hashlib.sha256(encoded).hexdigest(), "text_size": len(encoded),
            "engine": extracted.engine, "version": extracted.version,
            "languages": list(extracted.languages), "pages": extracted.pages,
            "total_pages": extracted.total_pages, "warnings": list(extracted.warnings),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        metadata_bytes = json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode("utf-8")
        if len(metadata_bytes) > MAX_METADATA_BYTES:
            raise VaultError("Text extraction metadata exceeds the local cache limit.")
        with self.log.write("publish extracted text"):
            if authorize is not None:
                authorize()
            current = visible_file(self.paths, source)
            if current.stat().st_size != size or sha256_file(current) != digest:
                raise VaultError("Document changed during text extraction; read it again.")
            if authorize is not None:
                authorize()
            cached = self._read_pair(digest, size, suffix, profile)
            if authorize is not None:
                authorize()
            if cached is not None:
                return cached
            _plain_path(self.directory, directory=True, missing=True)
            txt, meta = (self.directory / f"{digest}{ext}" for ext in (".txt", ".json"))
            with self.log.intent("text_extract", {"src": source, "text": txt, "metadata": meta},
                                 {"source_sha256": digest}):
                self.log.effect()
                self.directory.mkdir(exist_ok=True)
                self._replace_bytes(txt, encoded)
                # The manifest is the publication point. A partial pair is a miss.
                self._replace_bytes(meta, metadata_bytes)
                self.log.append("text_extracted", source, txt, sha256=digest, size=size,
                                extra={"pages": extracted.pages, "engine": extracted.engine})
        if authorize is not None:
            authorize()
        return TextSnapshot(extracted.text, digest, size, metadata)

    def _replace_bytes(self, target: Path, data: bytes):
        _plain_path(target, missing=True)
        temporary = self.directory / f".{uuid4().hex}.tmp"
        with temporary.open("xb") as stream:
            stream.write(data)
        os.replace(temporary, target)

    def _read_pair(self, digest: str, size: int, suffix: str, profile: str) -> TextSnapshot | None:
        from .text_extract import EXTRACTION_ENGINES

        if _plain_path(self.directory, directory=True, missing=True) is None:
            return None
        txt, meta = (self.directory / f"{digest}{ext}" for ext in (".txt", ".json"))
        txt_info, meta_info = _plain_path(txt, missing=True), _plain_path(meta, missing=True)
        if txt_info is None or meta_info is None:
            return None
        if txt_info.st_size > MAX_TEXT_BYTES or meta_info.st_size > MAX_METADATA_BYTES:
            return None
        try:
            with meta.open("rb") as stream:
                metadata_bytes = stream.read(MAX_METADATA_BYTES + 1)
            with txt.open("rb") as stream:
                encoded = stream.read(MAX_TEXT_BYTES + 1)
            metadata = json.loads(metadata_bytes)
            text = encoded.decode("utf-8")
            required = {"schema", "source_sha256", "source_size", "source_suffix", "profile",
                        "text_sha256", "text_size", "engine", "version", "languages", "pages",
                        "total_pages", "warnings", "timestamp"}
            if (not isinstance(metadata, dict) or set(metadata) != required
                    or type(metadata["schema"]) is not int or metadata["schema"] != 1
                    or metadata["source_sha256"] != digest
                    # All supported plain-text suffixes use the same decoder;
                    # byte-identical .txt/.md files share one digest cache.
                    # Binary extractor profiles still require the exact suffix.
                    or (metadata["source_suffix"] != suffix and not (
                        profile == PLAIN_TEXT_PROFILE and suffix in TEXT_SUFFIXES
                        and metadata["source_suffix"] in TEXT_SUFFIXES))
                    or metadata["profile"] != profile
                    or type(metadata["source_size"]) is not int or metadata["source_size"] != size
                    or type(metadata["text_size"]) is not int or metadata["text_size"] != len(encoded)
                    or len(encoded) > MAX_TEXT_BYTES or len(metadata_bytes) > MAX_METADATA_BYTES
                    or metadata["text_sha256"] != hashlib.sha256(encoded).hexdigest()
                    or not all(isinstance(metadata[key], str) and 0 < len(metadata[key]) <= 512
                               for key in ("engine", "version", "timestamp"))
                    or type(metadata["pages"]) is not int or not 0 <= metadata["pages"] <= 50
                    or type(metadata["total_pages"]) is not int
                    or metadata["total_pages"] < metadata["pages"]
                    or not isinstance(metadata["languages"], list)
                    or not all(isinstance(lang, str) and lang in ("eng", "rus")
                               for lang in metadata["languages"])
                    or len(metadata["languages"]) != len(set(metadata["languages"]))
                    or not isinstance(metadata["warnings"], list)
                    or not all(isinstance(warning, str) and len(warning) <= 2000
                               for warning in metadata["warnings"])):
                return None
            timestamp = datetime.fromisoformat(metadata["timestamp"])
            if (timestamp.tzinfo is None or (metadata["version"], tuple(metadata["languages"]))
                    not in EXTRACTION_ENGINES.get(metadata["engine"], ())):
                return None
        except (ValueError, TypeError, RecursionError, OSError):
            return None
        return TextSnapshot(text, digest, size, metadata)


def remove_cache_digests(paths: VaultPaths, log: ReceiptLog, digests: set[str]) -> bool:
    """Remove only exact cache pairs while the caller holds the root guard.

    Return True when a suspicious cache path was retained for owner review.
    This is nested inside the purge intention, so partial failure stays visible.
    """
    directory = paths.root / ".text"
    try:
        if _plain_path(directory, directory=True, missing=True) is None:
            return False
    except (VaultError, OSError):
        return True
    deferred = False
    for digest in sorted(digests):
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise VaultError("Invalid source digest for cache cleanup.")
        pair = [directory / f"{digest}{suffix}" for suffix in (".txt", ".json")]
        try:
            existing = [path for path in pair if _plain_path(path, missing=True) is not None]
        except (VaultError, OSError):
            deferred = True
            continue
        for path in existing:
            log.effect()
            path.unlink()
        if existing:
            log.append("text_cache_purged", "", directory, sha256=digest,
                       note="last source copy was purged")
    return deferred
