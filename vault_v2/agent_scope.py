"""Owner text permissions for archive Ask; never extends StagingReader or export."""
from __future__ import annotations

import json
from pathlib import Path
import stat
from uuid import uuid4

from .errors import VaultError
from .file_access import visible_directory, visible_file, iter_visible_files
from .ops import _operation_path
from .paths import PANE_NAMES, VaultPaths
from .receipts import ReceiptLog
from .text_queue import text_preparation_queue


class ScopeChanged(VaultError):
    pass


def _unique_settings(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate setting")
        result[key] = value
    return result


class AgentTextScope:
    def __init__(self, paths: VaultPaths, log: ReceiptLog):
        self.paths, self.log = paths, log
        self.queue = text_preparation_queue(paths, log)

    def _settings(self) -> dict:
        try:
            info = _operation_path(self.paths.settings_file, missing=True)
            if info is None:
                return {}
            if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
                raise ValueError("invalid settings file")
            settings = json.loads(self.paths.settings_file.read_text(encoding="utf-8"),
                                  object_pairs_hook=_unique_settings)
            if not isinstance(settings, dict):
                raise ValueError("settings must be an object")
            folders = settings.get("agent_text_folders", [])
            generation = settings.get("agent_text_generation", 0)
            if (not isinstance(folders, list) or type(generation) is not int or generation < 0):
                raise ValueError("invalid text permissions")
            for folder in folders:
                if not isinstance(folder, str):
                    raise ValueError("invalid text folder")
                rel = Path(folder)
                if (rel.is_absolute() or rel.drive or not rel.parts or rel.parts[0] not in PANE_NAMES
                        or any(part in (".", "..") or part.startswith(".") or ":" in part
                               or part.lower().endswith(".trash.json") or part.endswith((".", " "))
                               for part in rel.parts)):
                    raise ValueError("invalid text folder")
            return settings
        except (OSError, ValueError, TypeError) as exc:
            raise VaultError("Text permissions unavailable: vault.json needs manual review.") from exc

    def revision(self) -> tuple[int, tuple[str, ...]]:
        settings = self._settings()
        return settings.get("agent_text_generation", 0), tuple(settings.get("agent_text_folders", []))

    def validate(self, revision) -> None:
        if self.revision() != revision:
            raise ScopeChanged("Text permissions changed — ask again.")

    def grants(self) -> tuple[Path, ...]:
        return tuple(self.paths.root.absolute() / rel for rel in self.revision()[1])

    def covering_folder(self, path: Path) -> Path | None:
        path = Path(path).absolute()
        # Check lexical ancestors before any directory test may follow a link.
        try:
            visible_file(self.paths, path)
        except VaultError:
            visible_directory(self.paths, path)
        if path.is_relative_to(self.paths.staging.absolute()):
            return self.paths.staging.absolute()
        # The broadest active permission governs revocation and the UI label.
        for folder in sorted(self.grants(), key=lambda candidate: len(candidate.parts)):
            if path.is_relative_to(folder):
                visible_directory(self.paths, folder)
                return folder
        return None

    def require_allowed(self, path: Path, revision=None) -> None:
        if revision is not None:
            self.validate(revision)
        if self.covering_folder(path) is None:
            raise ScopeChanged("Text reading is not permitted here — refresh the request.")

    def set_folder(self, folder: Path, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise VaultError("Text permission must be on or off.")
        with self.log.write("change agent text scope"):
            folder = visible_directory(self.paths, Path(folder).absolute())
            if folder.is_relative_to(self.paths.staging.absolute()):
                raise VaultError("Staging is already the agent workspace; its text scope stays enabled.")
            settings = self._settings()
            folders = list(settings.get("agent_text_folders", []))
            relative = folder.relative_to(self.paths.root.absolute())
            rel = Path(relative.parts[0].lower(), *relative.parts[1:]).as_posix()
            if enabled:
                if self.covering_folder(folder) is not None:
                    return
                folders.append(rel)
            else:
                covering = self.covering_folder(folder)
                if covering is not None and covering != folder:
                    raise VaultError("This folder is allowed by a parent folder; stop reading at that parent.")
                # Revoking a marked parent also withdraws nested explicit grants.
                folders = [value for value in folders
                           if not (self.paths.root.absolute() / value).is_relative_to(folder)]
                if folders == settings.get("agent_text_folders", []):
                    return
            settings["agent_text_folders"] = folders
            settings["agent_text_generation"] = settings.get("agent_text_generation", 0) + 1
            op = "agent_scope_granted" if enabled else "agent_scope_revoked"
            temporary = self.paths.root / f".agent-scope-{uuid4().hex}.tmp"
            self.log.effect()
            try:
                with temporary.open("x", encoding="utf-8") as stream:
                    json.dump(settings, stream, ensure_ascii=False, indent=2)
                temporary.replace(self.paths.settings_file)
                self.log.append(op, folder, self.paths.settings_file)
            finally:
                if temporary.exists():
                    temporary.unlink()

    def warm(self, folder: Path) -> tuple[str, ...]:
        """Background folder job: enqueue in the shared serial queue and wait."""
        from .reader import agent_content_kind

        folder = visible_directory(self.paths, Path(folder).absolute())
        self.require_allowed(folder)
        files = [source for source in sorted(iter_visible_files(self.paths, folder))
                 if agent_content_kind(source) in {"TEXT", "EXTRACTABLE"}]
        jobs = []
        try:
            for source in files:
                jobs.append(self.request(source))
        finally:
            # Even an enqueue failure must not report this folder job finished
            # while its already accepted work is still running.
            for job in jobs:
                job.done.wait()
        notes = []
        for job in jobs:
            if isinstance(job.error, ScopeChanged):
                raise job.error
            if job.error is not None:
                notes.append(f"{job.source.name}: {job.error}")
        return tuple(notes)

    def request(self, source: Path):
        """Queue one currently permitted file; duplicate requests share the job."""
        if self.log.read_only:
            raise VaultError("Text preparation is unavailable in a read-only window.")
        return self.queue.request(source, lambda: self.require_allowed(source))

    def is_pending(self, source: Path) -> bool:
        return self.queue.is_pending(source)
