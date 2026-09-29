"""Filesystem layout of a vault instance.

One vault root holds three user-visible panes plus service folders:

    <root>/staging     temporary — exports and the Staging document jobs happen here
    <root>/documents   permanent main store
    <root>/personal    permanent private store
    <root>/.trash      soft-deleted files with a restore manifest
    <root>/.receipts   append-only hash-chained log of every operation
    <root>/.exports    folders/zips produced by approved exports
    <root>/vault.json  optional settings: pane titles, agent model

This is a single-owner vault. No pane is shared with other people.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .runtime import program_directory

PANE_NAMES = ("staging", "documents", "personal")

DEFAULT_TITLES = {
    "staging": "Staging",
    "documents": "Documents",
    "personal": "Personal",
}


@dataclass(frozen=True)
class VaultPaths:
    root: Path

    @property
    def staging(self) -> Path:
        return self.root / "staging"

    @property
    def documents(self) -> Path:
        return self.root / "documents"

    @property
    def personal(self) -> Path:
        return self.root / "personal"

    @property
    def trash(self) -> Path:
        return self.root / ".trash"

    @property
    def receipts(self) -> Path:
        return self.root / ".receipts"

    @property
    def exports(self) -> Path:
        return self.root / ".exports"

    @property
    def settings_file(self) -> Path:
        return self.root / "vault.json"

    def pane(self, name: str) -> Path:
        if name not in PANE_NAMES:
            raise ValueError(f"unknown pane: {name}")
        return self.root / name

    def pane_of(self, path: Path) -> str | None:
        """Return which pane a path belongs to, or None if outside the vault."""
        try:
            rel = Path(path).resolve().relative_to(self.root.resolve())
        except ValueError:
            return None
        head = rel.parts[0] if rel.parts else ""
        return head if head in PANE_NAMES else None

    def ensure(self) -> "VaultPaths":
        for p in (self.staging, self.documents, self.personal, self.trash, self.receipts, self.exports):
            p.mkdir(parents=True, exist_ok=True)
        return self

    # -- settings -------------------------------------------------------------

    def settings(self) -> dict:
        if not self.settings_file.exists():
            return {}
        try:
            return json.loads(self.settings_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def titles(self) -> dict[str, str]:
        out = dict(DEFAULT_TITLES)
        custom = self.settings().get("titles", {})
        for k, v in custom.items():
            if k in out and isinstance(v, str) and v.strip():
                out[k] = v.strip()
        return out


def default_root() -> Path:
    """Installed user data is separate from program files; dev layout is unchanged."""
    env = os.environ.get("VAULT_V2_ROOT")
    installation = program_directory()
    if installation is None:
        return Path(env) if env else Path(__file__).resolve().parent.parent / "data"
    if env:
        root = Path(env)
    else:
        local = os.environ.get("LOCALAPPDATA")
        if not local or not Path(local).is_absolute():
            raise RuntimeError("LOCALAPPDATA is unavailable; select a separate Vault root.")
        root = Path(local) / "VaultV2" / "data"
    if not root.is_absolute():
        raise RuntimeError("Vault root must be an absolute path outside the installation.")
    root = root.resolve()
    if root.is_relative_to(installation) or installation.is_relative_to(root):
        raise RuntimeError("Vault root and program files must be separate directories.")
    return root
