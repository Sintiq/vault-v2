"""Qualify only synthetic Qt/PDF/OCR work inside a built 0P embedded bundle.

No Vault window/server/model is started; no existing vault or phone is accessed.
Python audit hooks observe children and reject Python networking for this run.
These hooks are instrumentation, NOT an OS-level network/sandbox attestation.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time


def checked_worker_command(executable, arguments, python: Path) -> list[str]:
    """Windows audits (None, command-line string); POSIX audits (exe, argv)."""
    if executable is not None and Path(executable).resolve() != python.resolve():
        raise RuntimeError("unexpected child during synthetic qualification")
    for worker in ("pdf_worker.py", "ocr_worker.py"):
        command = [str(python), "-I", "-B", str(python.parent.parent / "vault_v2" / worker)]
        if arguments == command or arguments == subprocess.list2cmdline(command):
            return command
    raise RuntimeError("unexpected child during synthetic qualification")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    from vault_v2.runtime import installed_directory, worker_python
    from vault_v2.paths import default_root

    installation = installed_directory()
    if installation is None:
        parser.error("requires the built embedded runtime, not development Python")
    expected_python = Path(worker_python()).resolve()
    if Path(sys.executable).resolve() != expected_python:
        parser.error("run this qualifier with the bundle's python/python.exe")
    if not (sys.flags.isolated and sys.flags.ignore_environment and sys.flags.no_site):
        parser.error("isolated runtime without site is required")
    for entry in sys.path:
        if not Path(entry).resolve().is_relative_to(installation):
            parser.error("unexpected external Python import path")
    output = args.output_directory.resolve()
    if output.is_relative_to(installation) or installation.is_relative_to(output):
        parser.error("qualification output must be separate from program files")
    output.mkdir(parents=True, exist_ok=False)
    selected_root = default_root()  # Pure path selection, no storage creation.
    launches = []

    def observe(event, values):
        if event in {"socket.connect", "socket.connect_ex", "socket.bind", "socket.getaddrinfo"}:
            raise RuntimeError("Python networking forbidden during synthetic 0P")
        if event != "subprocess.Popen":
            return
        executable, arguments, cwd, environment = values
        argv = checked_worker_command(executable, arguments, expected_python)
        if set(environment) - {"SystemRoot", "WINDIR", "TEMP", "TMP", "OMP_THREAD_LIMIT", "PYTHONDONTWRITEBYTECODE"}:
            raise RuntimeError("unexpected child environment")
        launches.append({"worker": Path(argv[3]).name, "interpreter": str(expected_python),
                         "flags": argv[1:3], "environment_keys": sorted(environment),
                         "temporary_cwd": str(cwd)})

    sys.addaudithook(observe)
    from tools import qualify_pdf_process_preview as preview
    from tools.synthetic_ocr_files import ENGLISH, RUSSIAN, image_bytes
    from vault_v2.ocr_process import recognize_image
    import PIL
    import PySide6
    import pypdf
    import pypdfium2
    import cryptography

    modules = {module.__name__: str(Path(module.__file__).resolve())
               for module in (PIL, PySide6, pypdf, pypdfium2, cryptography)}
    if any(not Path(location).is_relative_to(installation / "vendor")
           for location in modules.values()):
        raise RuntimeError("a dependency escaped the bundled vendor directory")
    original_argv = sys.argv
    try:
        sys.argv = ["qualify_pdf_process_preview", "--output-directory", str(output / "pdf")]
        assert preview.main() == 0
    finally:
        sys.argv = original_argv
    ocr_results = []
    for language, text in (("eng", ENGLISH), ("rus", RUSSIAN)):
        payload = image_bytes(text)
        before = hashlib.sha256(payload).hexdigest()
        started = time.monotonic()
        recognized = recognize_image(payload, timeout_s=20)
        if " ".join(recognized.split()).casefold() != " ".join(text.split()).casefold():
            raise RuntimeError(f"synthetic {language} OCR mismatch: {recognized!r}")
        assert hashlib.sha256(payload).hexdigest() == before
        ocr_results.append({"language": language, "recognized": recognized,
                            "seconds": time.monotonic() - started, "input_sha256": before})
    assert sum(row["worker"] == "ocr_worker.py" for row in launches) == 2
    assert any(row["worker"] == "pdf_worker.py" for row in launches)
    for launch in launches:
        assert not Path(launch["temporary_cwd"]).exists(), "owned child directory remained"
        del launch["temporary_cwd"]
        launch["temporary_cwd_removed"] = True
    result = {
        "scope": "synthetic embedded Qt + real bounded PDF/OCR children, not a release",
        "python": sys.version, "executable": str(expected_python),
        "isolated": True, "ignore_environment": True, "no_site": True,
        "sys_path": sys.path, "dependency_origins": modules,
        "selected_default_root": str(selected_root),
        "pdf": json.loads((output / "pdf" / "result.json").read_text(encoding="utf-8")),
        "ocr": ocr_results, "child_launches": launches,
        "clean_machine_qualified": False, "security_freshness_qualified": False,
        "os_network_isolation_attested": False, "owner_data_used": False,
        "full_application_qualified": False,
    }
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
