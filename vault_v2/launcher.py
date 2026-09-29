"""Installed entry point: bundled Python, a retained install marker, then Qt.

No network/dependency installation, process termination or data migration here.
The runner seam also allows a synthetic non-GUI process to qualify admission.
"""
from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from .installation import InstallationBusy, InstallationSession, NAMESPACE
from .runtime import program_directory


def prepare_arguments(argv: list[str]) -> list[str]:
    """Apply the same program/data separation to explicit installed roots."""
    args = list(argv)
    program = program_directory()
    if program is None:
        return args
    if not args or len(args) > 2:
        raise RuntimeError("Expected Vault and at most one separate data directory.")
    if len(args) == 2:
        root = Path(args[1])
        if not root.is_absolute():
            raise RuntimeError("Vault root must be an absolute path outside program files.")
        root = root.resolve()
        if root.is_relative_to(program) or program.is_relative_to(root):
            raise RuntimeError("Vault root and program files must be separate directories.")
    else:
        from .paths import default_root
        root = default_root()
    return [args[0], str(root)]


def launch_application(run: Callable[[list[str]], int], argv: list[str], *, namespace: str = NAMESPACE) -> int:
    with InstallationSession("runtime", namespace=namespace):
        return run(prepare_arguments(argv))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    def application(args):
        # Import the actual Qt application only after install admission succeeds.
        from .main import main as run
        return run(args)
    try:
        return launch_application(application, argv)
    except (InstallationBusy, RuntimeError) as error:
        message = ("Vault cannot start while installation is active or its state is unknown. Finish setup, then try again."
                   if isinstance(error, InstallationBusy) else
                   "Vault cannot start with this installation or data-directory configuration. Repair the installation or select a separate data directory.")
        if sys.stderr is not None:
            print(message, file=sys.stderr)
        else:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, "Vault V2", 0x10)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
