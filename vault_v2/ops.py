"""File operations of the vault — pure functions over VaultPaths, every one
of them leaves a receipt. The GUI calls these; nothing in the GUI touches
files directly.

Rules baked in:
- copy is the default between panes; move only when asked explicitly;
- nothing is ever deleted: delete = move to .trash with a restore manifest;
- an operation that would overwrite refuses (fail closed) unless told
  `overwrite=True`;
- every written file is hashed after writing and the receipt carries the
  hash of what actually landed on disk.
"""

from __future__ import annotations

import json
import hashlib
import shutil
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .paths import PANE_NAMES, VaultPaths
from .receipts import ReceiptLog, sha256_file
from .errors import VaultError
from .write_guard import guarded


@dataclass(frozen=True)
class OpResult:
    op: str
    src: Path
    dst: Path
    sha256: str
    size: int


@dataclass(frozen=True)
class PurgeResult:
    removed: int
    skipped: int
    cache_deferred: bool = False


def _unique_target(dst_dir: Path, name: str, *, reserved: set[Path] | None = None) -> Path:
    target = dst_dir / name
    reserved = reserved or set()
    if target not in reserved and not target.exists():
        return target
    stem, suffix = Path(name).stem, Path(name).suffix
    for i in range(1, 1000):
        candidate = dst_dir / f"{stem} ({i}){suffix}"
        if candidate not in reserved and not candidate.exists():
            return candidate
    raise VaultError(f"too many copies of {name} in {dst_dir}")


def _inside(paths: VaultPaths, p: Path) -> None:
    if paths.pane_of(p) is None:
        raise VaultError(f"path is outside the vault panes: {p}")


LINK_REFUSAL = "this folder contains a link or junction; move it outside Vault first"


def _operation_path(path: Path, *, missing: bool = False, directory: bool = False):
    """Inspect lexical ancestors, including those above the vault root."""
    path = Path(path).absolute()
    if ".." in path.parts:
        raise VaultError("Parent path components are not allowed for file operations.")
    current = Path(path.anchor)
    info = current.lstat()
    for index, part in enumerate(path.parts[1:], 1):
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if missing:
                return None
            raise
        if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            raise VaultError(LINK_REFUSAL)
        if index < len(path.parts) - 1 or directory:
            if not stat.S_ISDIR(info.st_mode):
                raise VaultError("Operation requires a plain directory.")
        elif not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise VaultError("Operation requires a regular file or directory.")
    return info


def _preflight_tree(source: Path) -> None:
    """Validate the entire tree before hashing any bytes; hidden entries stay."""
    source = Path(source)
    info = _operation_path(source)
    if stat.S_ISREG(info.st_mode):
        return
    pending = [source]
    while pending:
        for child in pending.pop().iterdir():
            info = child.lstat()
            if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
                raise VaultError(LINK_REFUSAL)
            if stat.S_ISDIR(info.st_mode):
                pending.append(child)
            elif not stat.S_ISREG(info.st_mode):
                raise VaultError("Operation requires regular files and directories.")


def _plain_trash_entry(path: Path):
    """Read the entry itself, never a symlink/junction/reparse target."""
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise VaultError("Linked trash entry is not safe to restore or purge.")
    return info


class VaultOps:
    def __init__(self, paths: VaultPaths, *, read_only: bool = False):
        self.paths = paths
        self.log = ReceiptLog(paths.receipts, read_only=read_only)
        if not read_only:
            with self.log.write("initialize vault"):
                paths.ensure()

    # -- import from anywhere (uploads land in staging by default) ------------

    @guarded
    def import_file(self, src: Path, pane: str = "staging", note: str = "") -> OpResult:
        src = Path(src)
        if not src.is_file():
            raise VaultError(f"not a file: {src}")
        dst = _unique_target(self.paths.pane(pane), src.name)
        expected = sha256_file(src)
        with self.log.intent("import", {"src": src, "dst": dst}, {"src": expected}):
            self.log.effect()
            shutil.copy2(src, dst)
            digest = sha256_file(dst)
            self.log.append("import", src, dst, sha256=digest, size=dst.stat().st_size, note=note)
        return OpResult("import", src, dst, digest, dst.stat().st_size)

    # -- pane to pane ---------------------------------------------------------

    @guarded
    def copy(self, src: Path, dst_dir: Path, note: str = "") -> OpResult:
        src, dst_dir = Path(src), Path(dst_dir)
        _preflight_tree(src)
        _operation_path(dst_dir, directory=True)
        _inside(self.paths, dst_dir)
        if not dst_dir.is_dir():
            raise VaultError(f"destination is not a folder: {dst_dir}")
        if src.is_dir():
            dst = _unique_target(dst_dir, src.name)
            _operation_path(dst, missing=True)
            with self.log.intent("copy", {"src": src, "dst": dst}, self._expected(src)):
                self.log.effect()
                shutil.copytree(src, dst)
                self.log.append("copy_dir", src, dst, note=note)
            return OpResult("copy_dir", src, dst, "", 0)
        dst = _unique_target(dst_dir, src.name)
        _operation_path(dst, missing=True)
        with self.log.intent("copy", {"src": src, "dst": dst}, self._expected(src)):
            self.log.effect()
            shutil.copy2(src, dst)
            digest = sha256_file(dst)
            self.log.append("copy", src, dst, sha256=digest, size=dst.stat().st_size, note=note)
        return OpResult("copy", src, dst, digest, dst.stat().st_size)

    @guarded
    def move(self, src: Path, dst_dir: Path, note: str = "") -> OpResult:
        src, dst_dir = Path(src), Path(dst_dir)
        _preflight_tree(src)
        _operation_path(dst_dir, directory=True)
        _inside(self.paths, src)
        _inside(self.paths, dst_dir)
        dst = _unique_target(dst_dir, src.name)
        _operation_path(dst, missing=True)
        digest = sha256_file(src) if src.is_file() else ""
        size = src.stat().st_size if src.is_file() else 0
        with self.log.intent("move", {"src": src, "dst": dst}, self._expected(src)):
            self.log.effect()
            shutil.move(str(src), str(dst))
            self.log.append("move", src, dst, sha256=digest, size=size, note=note)
        return OpResult("move", src, dst, digest, size)

    # -- trash ----------------------------------------------------------------

    @guarded
    def trash(self, src: Path, note: str = "") -> OpResult:
        src = Path(src)
        return self._apply_trash(src, self._prepare_trash(src), note)

    def _prepare_trash(self, src: Path, reserved: set[Path] | None = None) -> tuple[str, Path, str]:
        """Choose and validate exact payload/sidecar names before any intent."""
        _preflight_tree(src)
        _operation_path(self.paths.trash, directory=True)
        _inside(self.paths, src)
        try:
            origin = src.resolve().relative_to(self.paths.root.resolve()).as_posix()
        except ValueError as exc:
            raise VaultError("Cannot resolve trash origin inside this vault; no files were moved.") from exc
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        occupied = set() if reserved is None else reserved
        while True:
            slot = _unique_target(self.paths.trash, f"{stamp}-{src.name}", reserved=occupied)
            manifest = slot.with_name(slot.name + ".trash.json")
            if manifest not in occupied:
                break
            occupied.add(slot)
        _operation_path(slot, missing=True)
        _operation_path(manifest, missing=True)
        occupied.update((slot, manifest))
        return stamp, slot, origin

    def _apply_trash(self, src: Path, plan: tuple[str, Path, str], note: str) -> OpResult:
        """Execute one already-preflighted plan while the same guard is held."""
        stamp, slot, origin = plan
        digest = sha256_file(src) if src.is_file() else ""
        size = src.stat().st_size if src.is_file() else 0
        with self.log.intent("trash", {"src": src, "dst": slot}, self._expected(src)):
            self.log.effect()
            slot.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(slot))
            manifest = slot.with_name(slot.name + ".trash.json")
            manifest.write_text(
                json.dumps({"origin": origin,
                            "trashed_at": stamp, "sha256": digest}, ensure_ascii=False),
                encoding="utf-8",
            )
            self.log.append("trash", src, slot, sha256=digest, size=size, note=note)
        return OpResult("trash", src, slot, digest, size)

    def list_trash(self) -> list[dict]:
        if not stat.S_ISDIR(_plain_trash_entry(self.paths.trash).st_mode):
            raise VaultError("Trash root is not a plain directory.")
        items = []
        entries = set(self.paths.trash.iterdir())
        seen: set[Path] = set()
        # Pair a payload with its own sidecar, even when its name already ends
        # in .trash.json. Overlapping roles are ambiguous and retained.
        for slot in sorted(entries, key=lambda p: (len(p.name), p.name)):
            if slot in seen:
                continue
            chain = [slot]
            while (next_path := chain[-1].with_name(chain[-1].name + ".trash.json")) in entries:
                chain.append(next_path)
            seen.update(chain)
            if len(chain) == 1:
                items.append({"slot": str(slot), "error": "no manifest"})
                continue
            if len(chain) != 2:
                items.extend({"slot": str(path), "error": "manifest damaged: ambiguous file roles"} for path in chain)
                continue
            m = chain[1]
            try:
                payload = _plain_trash_entry(slot)
                metadata = _plain_trash_entry(m)
                if (not (stat.S_ISREG(payload.st_mode) or stat.S_ISDIR(payload.st_mode))
                        or not stat.S_ISREG(metadata.st_mode)):
                    raise VaultError("Invalid trash entry type.")
                data = self._read_trash_manifest(m)
            except (VaultError, OSError):
                items.append({"slot": str(slot), "error": "manifest damaged or unreadable"})
                continue
            # Display does not authorize a restore. Keep legacy paths visible
            # after relocation; restore validates the destination separately.
            origin = Path(data["origin"])
            if not origin.is_absolute():
                origin = self.paths.root.resolve() / origin
            data["origin"] = str(origin)
            data["slot"] = str(slot)
            items.append(data)
        return sorted(items, key=lambda item: Path(item["slot"]).name)

    def _trash_origin(self, origin: str) -> Path:
        if not isinstance(origin, str) or not origin or "\x00" in origin:
            raise VaultError("Invalid trash origin; no files were restored.")
        path = Path(origin)
        if ".." in path.parts or (not path.is_absolute() and (path.drive or path.root)):
            raise VaultError("Invalid trash origin; no files were restored.")
        root = self.paths.root.resolve()
        candidate = path if path.is_absolute() else root / path
        try:
            # Old absolute manifests work only in their original, current root.
            # Never guess a replacement root from a pane name in an old path.
            candidate.relative_to(root)
        except ValueError as exc:
            raise VaultError("Trash origin is outside the current vault; legacy relocation needs review.") from exc
        _operation_path(candidate, missing=True)
        try:
            candidate = candidate.resolve()
            relative = candidate.relative_to(root)
        except (OSError, ValueError, RuntimeError) as exc:
            raise VaultError("Trash origin is outside the current vault or invalid.") from exc
        if len(relative.parts) < 2 or self.paths.pane_of(candidate) is None:
            raise VaultError("Trash origin must name an item inside a vault pane.")
        return candidate

    def _read_trash_manifest(self, manifest: Path) -> dict:
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError) as exc:
            raise VaultError("Invalid trash manifest; no files were restored.") from exc
        if not isinstance(data, dict) or "origin" not in data:
            raise VaultError("Invalid trash manifest; no files were restored.")
        origin = data["origin"]
        if not isinstance(origin, str) or not origin or any(ord(c) < 32 for c in origin):
            raise VaultError("Invalid trash origin; no files were restored.")
        path = Path(origin)
        if ".." in path.parts or (not path.is_absolute() and (path.drive or path.root)):
            raise VaultError("Invalid trash origin; no files were restored.")
        components = path.parts[1:] if path.anchor else path.parts
        if any(any(c in '<>:"|?*' for c in part) or part.rstrip(" .") != part for part in components):
            raise VaultError("Invalid trash origin; no files were restored.")
        if not path.is_absolute() and (len(path.parts) < 2 or path.parts[0] not in PANE_NAMES):
            raise VaultError("Invalid trash origin; no files were restored.")
        # Older manifests may omit display time/digest. Keep that absence
        # explicit; malformed present values cannot reach UI or receipts.
        when, digest = data.get("trashed_at", "unknown"), data.get("sha256", "")
        if not isinstance(when, str) or not when or any(ord(c) < 32 for c in when):
            raise VaultError("Invalid trash timestamp; no files were restored.")
        if (not isinstance(digest, str) or (digest and
                (len(digest) != 64 or any(c not in "0123456789abcdefABCDEF" for c in digest)))):
            raise VaultError("Invalid trash digest; no files were restored.")
        return {"origin": origin, "trashed_at": when, "sha256": digest}

    @guarded
    def restore(self, slot: Path, note: str = "") -> OpResult:
        slot = Path(slot)
        if slot.parent.absolute() != self.paths.trash.absolute():
            raise VaultError("Restore slot is outside this vault's trash.")
        try:
            _preflight_tree(slot)
        except FileNotFoundError as exc:
            raise VaultError("Cannot restore trash: no manifest or payload") from exc
        data = next((row for row in self.list_trash() if Path(row["slot"]) == slot), None)
        if data is None or "error" in data:
            reason = data["error"] if data is not None else "no manifest or payload"
            raise VaultError(f"Cannot restore trash: {reason}")
        manifest = slot.with_name(slot.name + ".trash.json")
        origin = self._trash_origin(data["origin"])
        dst = _unique_target(origin.parent, origin.name)
        _operation_path(dst, missing=True)
        with self.log.intent("restore", {"src": slot, "dst": dst}, self._expected(slot)):
            self.log.effect()
            origin.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(slot), str(dst))
            manifest.unlink()
            digest = sha256_file(dst) if dst.is_file() else ""
            size = dst.stat().st_size if dst.is_file() else 0
            self.log.append("restore", slot, dst, sha256=digest, size=size, note=note)
        return OpResult("restore", slot, dst, digest, size)

    @guarded
    def purge_trash(self, note: str = "") -> PurgeResult:
        """Empty the trash for good. The one operation here that really deletes.

        Trash rides along in every backup, so junk left there is junk kept
        forever. Each purged item gets its own receipt with the digest it had,
        so the log still says what was there even though the bytes are gone.
        """
        try:
            self.log.verify()
        except (ValueError, TypeError, AttributeError) as exc:
            raise VaultError("Receipt journal is invalid; trash was not purged.") from exc
        items = self.list_trash()
        manifest = {row["slot"]: row.get("sha256", "") for row in items if "error" not in row}
        with self.log.intent("purge_trash", {"trash": self.paths.trash},
                             {"manifest_sha256": hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()}):
            return self._purge_items(items, note)

    def _purge_items(self, items, note):
        n = skipped = 0
        try:
            (self.paths.root / ".text").lstat()
            cache_present = True
        except FileNotFoundError:
            cache_present = False
        except OSError:
            # Uncertainty is retention, not a reason to hide healthy trash.
            cache_present = True
        purged_hashes: set[str] = set()
        uncertain = False
        for item in items:
            if "error" in item:
                skipped += 1
                uncertain = True
                continue
            slot = Path(item["slot"])
            manifest = slot.with_name(slot.name + ".trash.json")
            digest = item.get("sha256", "")
            payload = _plain_trash_entry(slot)
            _plain_trash_entry(manifest)
            hashes, unreadable = self._payload_hashes(slot) if cache_present else (set(), False)
            if unreadable:
                skipped += 1
                uncertain = True
                continue
            self.log.effect()
            if stat.S_ISDIR(payload.st_mode):
                shutil.rmtree(slot)
            else:
                slot.unlink()
            manifest.unlink(missing_ok=True)
            self.log.append("purge", item.get("origin", ""), slot, sha256=digest, note=note)
            purged_hashes.update(hashes)
            n += 1
        if not cache_present:
            return PurgeResult(n, skipped)
        remaining_hashes: set[str] = set()
        for pane in PANE_NAMES:
            hashes, unreadable = self._payload_hashes(self.paths.pane(pane))
            remaining_hashes.update(hashes)
            uncertain |= unreadable
        for item in self.list_trash():
            if "error" in item:
                uncertain = True
            else:
                hashes, unreadable = self._payload_hashes(Path(item["slot"]))
                remaining_hashes.update(hashes)
                uncertain |= unreadable
        from .text_cache import remove_cache_digests
        deferred = uncertain
        if not uncertain:
            deferred = remove_cache_digests(self.paths, self.log, purged_hashes - remaining_hashes)
        if deferred:
            self.log.append("text_cache_cleanup_deferred", self.paths.trash, "",
                            note="text cache kept: source copies or cache paths need manual review")
        return PurgeResult(n, skipped, deferred)

    def _payload_hashes(self, path: Path) -> tuple[set[str], bool]:
        """Actual byte hashes; never descend through a linked or unreadable item."""
        hashes: set[str] = set()
        try:
            absolute = path.absolute()
            if ".." in absolute.parts:
                return hashes, True
            current = Path(absolute.anchor)
            for part in absolute.parts[1:]:
                current /= part
                info = _plain_trash_entry(current)
            if stat.S_ISREG(info.st_mode):
                hashes.add(sha256_file(path))
                return hashes, False
            if not stat.S_ISDIR(info.st_mode):
                return hashes, True
            uncertain = False
            for child in path.iterdir():
                child_hashes, unreadable = self._payload_hashes(child)
                hashes.update(child_hashes)
                uncertain |= unreadable
            return hashes, uncertain
        except (OSError, VaultError):
            return hashes, True

    # -- folders --------------------------------------------------------------

    @guarded
    def mkdir(self, parent: Path, name: str) -> Path:
        parent = Path(parent)
        _inside(self.paths, parent)
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            raise VaultError(f"bad folder name: {name!r}")
        target = parent / name
        if target.exists():
            raise VaultError(f"already exists: {target}")
        with self.log.intent("mkdir", {"parent": parent, "dst": target}):
            self.log.effect()
            target.mkdir()
            self.log.append("mkdir", parent, target)
        return target

    # -- staging --------------------------------------------------------------

    @guarded
    def clear_staging(self) -> int:
        """Move everything in staging to trash. Originals elsewhere are untouched."""
        _preflight_tree(self.paths.staging)
        _operation_path(self.paths.trash, directory=True)
        items = list(self.paths.staging.iterdir())
        reserved: set[Path] = set()
        plans = [(item, self._prepare_trash(item, reserved)) for item in items]
        with self.log.intent("clear_staging", {"src": self.paths.staging, "dst": self.paths.trash},
                             self._expected(self.paths.staging)):
            n = 0
            for item, plan in plans:
                self._apply_trash(item, plan, note="clear_staging")
                n += 1
            return n

    def _expected(self, source: Path) -> dict[str, str]:
        """File digest or bounded tree digest of sorted relative paths/file hashes."""
        if source.is_file():
            return {"src": sha256_file(source)}
        digest = hashlib.sha256()
        for path in sorted(source.rglob("*")):
            if path.is_file():
                record = [path.relative_to(source).as_posix(), sha256_file(path)]
                digest.update(json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
        return {"tree_sha256": digest.hexdigest()}
