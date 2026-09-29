"""Gatekeeper — the only way anything leaves the vault.

Rules (from capsule-product-block, kept whole):
- deny by default: an export needs an explicit owner approval, one per request;
- the approval is bound to a snapshot: the exact files and their hashes as
  shown in the preview. If a file changes between preview and write, the
  export refuses instead of silently exporting something else;
- an approval is used once and expires;
- only Staging can be exported. Documents and Personal never leave through
  this gate;
- every written file is hashed again after writing; the receipt records what
  actually landed on disk, and a mismatch is reported, never hidden.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .file_access import iter_visible_files, visible_directory, visible_file
from .ops import VaultError, VaultOps
from .receipts import sha256_file

APPROVAL_TTL_S = 180


@dataclass(frozen=True)
class ExportItem:
    path: Path
    rel: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ExportRequest:
    id: str
    items: tuple[ExportItem, ...]
    destination: Path
    mode: str  # "folder" | "zip"
    created_at: float
    digest: str
    hidden_count: int = 0

    @property
    def total_size(self) -> int:
        return sum(i.size for i in self.items)


@dataclass
class Approval:
    request_id: str
    digest: str
    granted_at: float
    used: bool = False

    def is_valid_for(self, req: ExportRequest, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return (
            not self.used
            and self.request_id == req.id
            and self.digest == req.digest
            and now - self.granted_at <= APPROVAL_TTL_S
        )


@dataclass(frozen=True)
class ExportResult:
    request_id: str
    destination: Path
    mode: str
    written: tuple[tuple[str, str], ...]  # (rel, sha256 as written)
    verified: bool
    problems: tuple[str, ...] = field(default_factory=tuple)


class GateError(VaultError):
    pass


def _request_digest(items: list[ExportItem], destination: Path, mode: str, hidden_count: int = 0) -> str:
    body = {
        "items": [{"rel": i.rel, "size": i.size, "sha256": i.sha256} for i in items],
        "destination": str(destination),
        "mode": mode,
        "hidden_count": hidden_count,
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


class Gatekeeper:
    def __init__(self, ops: VaultOps):
        self.ops = ops
        self._approvals: dict[str, Approval] = {}

    def _visible_staging_source(self, source: Path, *, directory: bool = False) -> Path:
        source = Path(source).absolute()
        try:
            source.relative_to(self.ops.paths.staging.absolute())
        except ValueError as exc:
            raise GateError(f"only Staging can be exported: {source}") from exc
        try:
            boundary = visible_directory if directory else visible_file
            return boundary(self.ops.paths, source)
        except VaultError as exc:
            raise GateError(str(exc)) from exc

    # -- 1. preview: snapshot exactly what would leave -----------------------

    def prepare(self, sources: list[Path], destination: Path, mode: str = "folder") -> ExportRequest:
        if mode not in ("folder", "zip"):
            raise GateError(f"unknown export mode: {mode}")
        staging = self.ops.paths.staging.absolute()
        items: list[ExportItem] = []
        hidden_items: set[Path] = set()
        for src in sources:
            src = Path(src)
            src = self._visible_staging_source(src, directory=src.is_dir())
            if src.is_dir():
                files = sorted(iter_visible_files(self.ops.paths, src, hidden_items=hidden_items))
            elif src.is_file():
                files = [src]
            else:
                raise GateError(f"not found: {src}")
            for f in files:
                f = self._visible_staging_source(f)
                rel = f.relative_to(staging).as_posix()
                items.append(ExportItem(f, rel, f.stat().st_size, sha256_file(f)))
        if not items:
            raise GateError("nothing to export")
        # Reject an impossible export in the preview, before requesting consent.
        if any(it.rel.split("/", 1)[0].casefold() == "manifest.txt" for it in items):
            raise GateError("reserved export path: manifest.txt")
        destination = Path(destination)
        if self.ops.paths.pane_of(destination) is not None:
            raise GateError("destination must be outside the vault")
        req = ExportRequest(
            id=uuid.uuid4().hex,
            items=tuple(items),
            destination=destination,
            mode=mode,
            created_at=time.time(),
            digest=_request_digest(items, destination, mode, len(hidden_items)),
            hidden_count=len(hidden_items),
        )
        return req

    # -- 2. approval: explicit, one-use, bound to the snapshot ---------------

    def approve(self, req: ExportRequest) -> Approval:
        ap = Approval(request_id=req.id, digest=req.digest, granted_at=time.time())
        self._approvals[req.id] = ap
        return ap

    # -- 3. execute: re-verify, write, verify again, receipt -----------------

    def execute(self, req: ExportRequest) -> ExportResult:
        # One root guard covers preview recheck, effects and all receipts.
        # Any proposal/store locks used inside must follow this outer guard.
        with self.ops.log.write("export"):
            return self._execute(req)

    def _execute(self, req: ExportRequest) -> ExportResult:
        ap = self._approvals.get(req.id)
        if ap is None or not ap.is_valid_for(req):
            raise GateError("no valid approval for this export (missing, used, or expired)")
        ap.used = True

        # The snapshot must still be true: same bytes as the owner saw.
        for it in req.items:
            source = self._visible_staging_source(it.path)
            if sha256_file(source) != it.sha256:
                raise GateError(f"changed since preview (content): {it.rel}")

        # Reserve the root manifest for both export formats, including a
        # directory of that name and case-insensitive extraction targets.
        # Refuse before destination creation, copying or appending receipts.
        if any(it.rel.split("/", 1)[0].casefold() == "manifest.txt" for it in req.items):
            raise GateError("reserved export path: manifest.txt")
        if _request_digest(list(req.items), req.destination, req.mode, req.hidden_count) != req.digest:
            raise GateError("changed since preview (request)")

        if req.mode == "zip":
            return self._write_zip(req)
        return self._write_folder(req)

    def _manifest(self, req: ExportRequest) -> str:
        lines = [
            "VAULT EXPORT",
            f"request: {req.id}",
            f"snapshot: {req.digest}",
            f"exported_at: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
            f"files: {len(req.items)}",
            f"{req.hidden_count} hidden items not exported",
            "A hidden folder counts as one item; its contents are not inspected.",
            "",
        ]
        for it in req.items:
            lines.append(f"{it.sha256}  {it.size:>10}  {it.rel}")
        lines.append("")
        lines.append("Each line: sha256, size in bytes, path inside this export.")
        return "\n".join(lines) + "\n"

    def _write_folder(self, req: ExportRequest) -> ExportResult:
        dest = req.destination
        if dest.exists() and any(dest.iterdir()):
            raise GateError(f"destination is not empty: {dest}")
        self.ops.log.effect()
        dest.mkdir(parents=True, exist_ok=True)
        written: list[tuple[str, str]] = []
        problems: list[str] = []
        for it in req.items:
            target = dest / it.rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(it.path, target)
            got = sha256_file(target)
            written.append((it.rel, got))
            if got != it.sha256:
                problems.append(f"hash mismatch after write: {it.rel}")
            self.ops.log.append(
                "export", it.path, target, sha256=got, size=target.stat().st_size,
                extra={"request": req.id, "mode": "folder", "expected": it.sha256},
            )
        (dest / "manifest.txt").write_text(self._manifest(req), encoding="utf-8")
        self.ops.log.append(
            "export_done", self.ops.paths.staging, dest, note=f"{len(req.items)} files",
            extra={"request": req.id, "snapshot": req.digest, "mode": "folder", "verified": not problems},
        )
        return ExportResult(req.id, dest, "folder", tuple(written), not problems, tuple(problems))

    def _write_zip(self, req: ExportRequest) -> ExportResult:
        dest = req.destination if req.destination.suffix.lower() == ".zip" else req.destination.with_suffix(".zip")
        if dest.exists():
            raise GateError(f"destination already exists: {dest}")
        self.ops.log.effect()
        dest.parent.mkdir(parents=True, exist_ok=True)
        written: list[tuple[str, str]] = []
        problems: list[str] = []
        with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for it in req.items:
                zf.write(it.path, arcname=it.rel)
            zf.writestr("manifest.txt", self._manifest(req))
        # verify by reading back from the archive
        with zipfile.ZipFile(dest, "r") as zf:
            for it in req.items:
                h = hashlib.sha256()
                with zf.open(it.rel) as f:
                    for block in iter(lambda: f.read(1 << 20), b""):
                        h.update(block)
                got = h.hexdigest()
                written.append((it.rel, got))
                if got != it.sha256:
                    problems.append(f"hash mismatch inside zip: {it.rel}")
                self.ops.log.append(
                    "export", it.path, f"{dest}!{it.rel}", sha256=got, size=it.size,
                    extra={"request": req.id, "mode": "zip", "expected": it.sha256},
                )
        self.ops.log.append(
            "export_done", self.ops.paths.staging, dest, sha256=sha256_file(dest), size=dest.stat().st_size,
            note=f"{len(req.items)} files",
            extra={"request": req.id, "snapshot": req.digest, "mode": "zip", "verified": not problems},
        )
        return ExportResult(req.id, dest, "zip", tuple(written), not problems, tuple(problems))
