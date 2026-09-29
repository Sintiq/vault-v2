"""Read-only boundary for owner-visible files in the three vault panes."""
from pathlib import Path
import os
import stat
from collections.abc import Iterator

from .errors import VaultError
from .paths import PANE_NAMES, VaultPaths


def _hidden_name(name: str) -> bool:
    return name.startswith(".") or name.lower().endswith(".trash.json")


def visible_file(paths: VaultPaths, path: Path) -> Path:
    """Validate lexical containment and every component before any file read.

    This protects normal owner navigation, not against a hostile process
    replacing filesystem entries after validation.
    """
    return _visible_path(paths, path, directory=False)


def visible_directory(paths: VaultPaths, path: Path) -> Path:
    """Validate a pane root or visible directory without resolving aliases."""
    return _visible_path(paths, path, directory=True)


def _visible_path(paths: VaultPaths, path: Path, *, directory: bool | None) -> Path:
    path, root = Path(path), Path(paths.root).absolute()
    if not path.is_absolute() or ".." in path.parts or ".." in root.parts:
        raise VaultError("File must be inside a visible vault pane.")
    if os.name == "nt" and any(part.endswith((".", " ")) for part in path.parts[1:]):
        raise VaultError("Ambiguous Windows path aliases cannot be opened.")
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise VaultError("File must be inside a visible vault pane.") from exc
    pane = relative.parts[0] if relative.parts else ""
    if os.name == "nt":
        pane = pane.casefold()
    if (len(relative.parts) < (2 if directory is False else 1) or pane not in PANE_NAMES
            or any(_hidden_name(part) or ":" in part
                   for part in relative.parts)):
        raise VaultError("Hidden or service files cannot be opened.")
    current = Path(path.anchor)
    try:
        for part in path.parts[1:]:
            current /= part
            info = current.lstat()
            if (stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
                raise VaultError("Linked files or folders cannot be opened.")
        if directory is True and not stat.S_ISDIR(info.st_mode):
            raise VaultError("Only visible directories can be listed.")
        if directory is False and not stat.S_ISREG(info.st_mode):
            raise VaultError("Only regular files can be opened.")
        if directory is None and not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise VaultError("Only regular files and directories can be listed.")
    except OSError as exc:
        raise VaultError("File is missing or cannot be read.") from exc
    return path


def visible_children(paths: VaultPaths, directory: Path, *,
                     hidden_items: set[Path] | None = None) -> Iterator[Path]:
    """Prune before I/O; optionally record hidden boundaries, not descendants.

    Export uses the set for an honest, deduplicated omission count. A hidden
    directory counts once and is never traversed; links do not count as hidden.
    """
    parent = visible_directory(paths, directory)
    for candidate in parent.iterdir():
        try:
            entry = _visible_path(paths, candidate, directory=None)
        except VaultError:
            if hidden_items is not None and _hidden_name(candidate.name):
                try:
                    info = candidate.lstat()
                    if (not stat.S_ISLNK(info.st_mode)
                            and not getattr(info, "st_file_attributes", 0)
                            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                            and (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))):
                        hidden_items.add(candidate)
                except OSError:
                    pass
            continue
        yield entry


def iter_visible_files(paths: VaultPaths, directory: Path, *,
                       hidden_items: set[Path] | None = None) -> Iterator[Path]:
    """Walk only validated directories; never follow a link then filter it."""
    pending = [visible_directory(paths, directory)]
    while pending:
        for entry in visible_children(paths, pending.pop(), hidden_items=hidden_items):
            if entry.is_dir():
                pending.append(entry)
            else:
                yield entry
