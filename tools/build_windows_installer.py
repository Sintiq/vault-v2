"""Compile an inventoried Windows payload with the pinned portable Inno toolchain.

Compilation only. This module never starts the generated installer.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from tools.build_runtime_0p import BuildError, _file_digest, _plain_path

COMPILER_HASHES = {
    "ISCC.exe": "d06ebd38f38e3cee60a3c50cc45bd449d77e0bc6a5cabc607ea9886808e4de1a",
    "ISCmplr.dll": "a7a58961ca61bfb2570e66a29b40021b887c9a41d3cbf3ae79611538257864ee",
    "ISPP.dll": "f875ddf920f17dceaaad05280dafd6d5376a1a4111cbd5fe97bfc47c286b5a41",
    "ISSigTool.exe": "731574e95da866789fe83f26040a4c541a060ca7f7d867f2618e201d378c3e6f",
}


@dataclass(frozen=True)
class Payload:
    root: Path
    version: str
    version_code: int
    api_version: int
    files: tuple[str, ...]
    inventory_sha256: str
    recipient_terms: bytes | None = None


def _json(path: Path, maximum: int):
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise BuildError("Build metadata is too large")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise BuildError("Duplicate metadata key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique), hashlib.sha256(raw).hexdigest()


def validate_payload(root: Path) -> Payload:
    root = Path(root).absolute()
    _plain_path(root)
    if any(c in str(root) for c in '\r\n"{}') or ".." in root.parts:
        raise BuildError("Unsafe compiler path")
    try:
        for path in root.rglob("*"):
            _plain_path(path)
        inventory, digest = _json(root / "windows-payload-files.json", 4 * 1024 * 1024)
        if not isinstance(inventory, dict) or inventory.get("schema") != "vault-v2-payload-files@1":
            raise BuildError("Invalid payload inventory")
        names, folded = [], set()
        for item in inventory["files"]:
            name = item["path"]
            if (not isinstance(name, str) or not name or any(c in name for c in '\\:\r\n"{}')
                    or any(p in ("", ".", "..") or p.rstrip(" .") != p for p in name.split("/"))
                    or name.casefold() in folded or name == "windows-payload-files.json"
                    or type(item["size"]) is not int or item["size"] < 0
                    or not isinstance(item["sha256"], str) or re.fullmatch("[0-9a-f]{64}", item["sha256"]) is None):
                raise BuildError("Invalid inventory entry")
            path = root / name
            if not path.is_file() or _file_digest(path) != (item["sha256"], item["size"]):
                raise BuildError("Payload file changed or is missing")
            names.append(name)
            folded.add(name.casefold())
        names.append("windows-payload-files.json")
        actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
        if actual != set(names):
            raise BuildError("Payload contains unlisted files")
        if "runtime-redist.json" in names and "MSVC-RECIPIENT-TERMS.txt" not in names:
            raise BuildError("Microsoft runtime recipient terms are required")
        recipient_terms = None
        if "MSVC-RECIPIENT-TERMS.txt" in names:
            with (root / "MSVC-RECIPIENT-TERMS.txt").open("rb") as stream:
                recipient_terms = stream.read(65537)
            entry = next(row for row in inventory["files"] if row["path"] == "MSVC-RECIPIENT-TERMS.txt")
            if (len(recipient_terms) != entry["size"]
                    or hashlib.sha256(recipient_terms).hexdigest() != entry["sha256"]):
                raise BuildError("Microsoft recipient terms changed after inventory check")
            try:
                text = recipient_terms.decode("utf-8-sig")
            except UnicodeError:
                raise BuildError("Invalid recipient terms encoding") from None
            if not text.strip() or len(recipient_terms) > 65536 or any(
                    (ord(char) < 32 and char not in "\t\r\n") or 127 <= ord(char) <= 159
                    for char in text):
                raise BuildError("Invalid recipient terms text/size")
        marker, _ = _json(root / "vault-install.json", 4096)
        if (marker.get("schema") != "vault-v2-install@1" or marker.get("program_root") != "../.."
                or marker.get("worker_python") != "python/python.exe"
                or not isinstance(marker.get("version"), str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}", marker["version"]) is None
                or any(type(marker.get(k)) is not int or not 1 <= marker[k] <= 2147483647
                       for k in ("version_code", "api_version"))
                or not {"python/python.exe", "python/pythonw.exe", "vault_v2/launcher.py", "vault_v2/installation.py"} <= set(names)):
            raise BuildError("Invalid installed payload identity")
        return Payload(root, marker["version"], marker["version_code"], marker["api_version"], tuple(sorted(names)), digest, recipient_terms)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise BuildError("Invalid or unreadable payload") from None


def compile_installer(payload: Path, compiler: Path, output: Path) -> Path:
    checked = validate_payload(payload)
    compiler, output = Path(compiler).absolute(), Path(output).absolute()
    for path in (compiler, output):
        _plain_path(path)
        if ".." in path.parts or any(c in str(path) for c in '\r\n"{}'):
            raise BuildError("Unsafe compiler/output path")
    if output.exists() or output.suffix.lower() != ".exe" or output.is_relative_to(checked.root):
        raise BuildError("Choose a new executable output outside the payload")
    if compiler.name != "ISCC.exe":
        raise BuildError("Pinned ISCC.exe is required")
    for name, expected in COMPILER_HASHES.items():
        component = compiler.parent / name
        _plain_path(component)
        if not component.is_file() or _file_digest(component)[0] != expected:
            raise BuildError("Portable compiler component pin mismatch")
    output.parent.mkdir(parents=True, exist_ok=True)
    recipe = Path(__file__).resolve().parent / "windows/vault-v2.iss"
    _plain_path(recipe)
    with tempfile.TemporaryDirectory(prefix="vault-compile-", dir=output.parent) as temporary:
        scratch = Path(temporary)
        include = scratch / "payload-files.iss"
        lines = ["[Files]"]
        for name in checked.files:
            destination = str(Path(name).parent).replace("/", "\\")
            suffix = "" if destination == "." else "\\" + destination
            lines.append(f'Source: "{checked.root / name}"; DestDir: "{{app}}\\versions\\{checked.version_code}{suffix}"; Flags: ignoreversion')
        include.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
        command = [str(compiler), "--no-ide-signtools", "/Q",
                   f"/DAppVersion={checked.version}", f"/DVersionCode={checked.version_code}",
                   f"/DPayloadFiles={include}", f"/O{scratch}", "/Fvault-setup", str(recipe)]
        if checked.recipient_terms is not None:
            terms = scratch / "MSVC-RECIPIENT-TERMS.txt"
            terms.write_bytes(checked.recipient_terms)
            command.insert(-1, f"/DRecipientTerms={terms}")
        try:
            result = subprocess.run(command, cwd=scratch, capture_output=True, timeout=300,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired):
            raise BuildError("Compiler failed or exceeded its deadline") from None
        if result.returncode != 0:
            raise BuildError("Compiler refused recipe: " + result.stdout[-4000:].decode(errors="replace")
                             + result.stderr[-4000:].decode(errors="replace"))
        built = scratch / "vault-setup.exe"
        if not built.is_file() or not 2 <= built.stat().st_size <= 1024 ** 3:
            raise BuildError("Compiler output is missing or too large")
        with built.open("rb") as stream:
            if stream.read(2) != b"MZ":
                raise BuildError("Compiler output is not an executable")
        if validate_payload(checked.root) != checked:
            raise BuildError("Payload changed during compilation")
        _plain_path(output)
        if output.exists():
            raise BuildError("Output appeared during compilation")
        # Windows rename does not overwrite an existing destination.
        if os.name != "nt":
            raise BuildError("Windows compiler publication is Windows-only")
        built.rename(output)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", required=True, type=Path)
    parser.add_argument("--compiler", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        print(compile_installer(args.payload, args.compiler, args.output))
    except BuildError as error:
        parser.exit(2, str(error) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
