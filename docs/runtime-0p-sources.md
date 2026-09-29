# Runtime 0P: CPython Windows embedding sources

Checked 2026-09-26. Primary-source implementation research only; no runtime qualification, artifact download, signature verification, installation, or execution was performed for this note.

## Official CPython 3.12 x64 artifact

The newest official Windows embeddable binary in the 3.12 series is **3.12.10**, released **2025-04-08**. Its release page lists the 64-bit embeddable ZIP; later 3.12 releases are source-only. [3.12.10 release](https://www.python.org/downloads/release/python-31210/), [3.12.14 release and binary policy](https://www.python.org/downloads/release/python-31214/).

- Artifact: [python-3.12.10-embed-amd64.zip](https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip).
- Publisher metadata: [adjacent SPDX SBOM](https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip.spdx.json), package `SPDXRef-PACKAGE-cpython`, identifies that exact ZIP and Python Software Foundation as supplier/originator.
- SHA-256 published in that package's `checksums`: `4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3`.
- The release page also links [Sigstore bundle](https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip.sigstore) and [OpenPGP signature](https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip.asc). Its displayed checksum is MD5, so use the SPDX SHA-256 above for artifact pinning. Reading publisher metadata does not establish that a local download matches it or that a signature was verified. [Release files](https://www.python.org/downloads/release/python-31210/), [official verification guidance](https://www.python.org/downloads/metadata/sigstore/).

## Security freshness limitation

**3.12.10 is not the current 3.12 security release.** The official site currently identifies **3.12.14**, released **2026-08-12**, with subsequent security fixes, including Windows ZIP extraction, Expat, decompression, and parser fixes. The 3.12 branch receives irregular security releases in source form through October 2028; branch support does not update the old 3.12.10 binary. [3.12.14 release](https://www.python.org/downloads/release/python-31214/).

Implementation consequence: treat 3.12.10 as a compatibility candidate for a bounded synthetic 0P exercise, not a qualified current or production runtime. A successful OCR import or extraction cannot close this patch gap. A supported release decision needs an updated 3.12 build with its own provenance/validation, or a maintained newer interpreter with compatible dependencies. This is a packaging inference, not a claim that the present workload exercises every listed vulnerability.

## Vendored dependencies and isolated child invocation

CPython documents the embedded distribution as application-local. It excludes pip; normal pip-managed dependency installation is unsupported. The application installer should supply and version third-party packages alongside it. The host must also supply the Microsoft C Runtime prerequisite. [Windows embedding guidance](https://docs.python.org/3.12/using/windows.html#the-embeddable-package).

The builder places Python under `python/` and both `vendor/` and `vault_v2/` beside that directory. Its explicit `python/python312._pth` contains:

```text
python312.zip
.
../vendor
..
```

`._pth` paths resolve relative to that file, not the launcher's working directory; `.` above means `python/`, and `..` means the bundle root. The DLL-named file takes precedence over an executable-named file. This configuration overrides `sys.path`, ignores registry/environment path injection, and skips `site` unless `import site` is explicitly enabled. Therefore `../vendor` must be listed; setting `PYTHONPATH` is ineffective. The builder omits `import site`. These layout choices are an implementation inference from [path initialization rules](https://docs.python.org/3.12/library/sys_path_init.html#pth-files).

Use the absolute bundled executable with `-I -B` in each child invocation. `-I` ignores `PYTHON*` variables and suppresses implicit script/current-directory and user-site additions; `-B` suppresses `.pyc` writes. These flags do not forbid application file writes or provide an OS sandbox. [Command-line semantics](https://docs.python.org/3.12/using/cmdline.html#cmdoption-I), [bytecode flag](https://docs.python.org/3.12/using/cmdline.html#cmdoption-B).

Native wheels must be staged as filesystem packages: ZIP import cannot load `.pyd`/`.so` extensions. Wheel installation can require moving `.data` subtrees to their designated destinations; merely adding a wheel archive to `._pth` does not implement installation. Preserve required native DLL/package-data layout and qualify imports in the actual embedded child. [zipimport restrictions](https://docs.python.org/3.12/library/zipimport.html), [wheel installation specification](https://packaging.python.org/en/latest/specifications/binary-distribution-format/#installing-a-wheel-distribution-1-0-py32-none-any-whl).

## Bounded builder implementation

Run `tools/build_runtime_0p.py --output <new-directory> --cache <asset-cache>` explicitly with the build host Python. Both directories must be outside the source checkout and separate from each other. The builder refuses existing output, downloads sequentially with standard certificate verification, verifies every cached/downloaded asset against both SHA-256 and exact length, then publishes a completed staging directory. Corrupt cache entries cause refusal; they are not silently replaced. The builder never launches bundled executables or a dependency installer.

`tools/runtime-0p-assets.json` pins 15 artifacts totaling **117,161,541 bytes**. PyPI wheel filenames, hashes, sizes and dependency metadata were checked using the version-specific official JSON API URLs retained in that manifest. Pins: PySide6 Essentials/shiboken6 6.11.2, pypdfium2 5.13.0, pypdf 6.19.0, Pillow 12.3.0, cryptography 50.0.1, cffi 2.1.1, pycparser 3.0, qrcode 8.2 and its Windows dependency colorama 0.4.6. The existing private OCR pins supply the third-party tesserocr 2.10.0 Windows wheel and commit-pinned English/Russian models.

The public [model license](https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/87416418657359cb625c412a48b6e1d6d41c29bd/LICENSE) was independently read: SHA-256 `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30`, 11,358 bytes. The builder copies those verified LF bytes, rather than the checkout's CRLF variant. Python and wheel license/notice files remain in their extracted distributions; a generated notice points to them. This preserves supplied notices without claiming a completed redistribution-license audit.

Source inclusion is deliberately narrow: top-level `vault_v2/*.py`, only the four named synthetic/qualification tools, and verified OCR assets. Existing environments, arbitrary repository data, cached models, and `vault_v2/web` static assets are excluded. Thus this is not a complete installer or qualification of the phone/web interface or whole desktop application. Wheels are unpacked, with `.data/purelib` and `.data/platlib` relocated to the vendor root; other `.data` content is retained without installing command wrappers. This is sufficient only if the actual packaged imports/qualification confirm the selected dependencies.

ZIP validation rejects traversal/absolute/drive paths, backslash ambiguities, reserved device names, links/special files, duplicate case-insensitive members and excessive expanded sizes. Existing source/cache/output ancestors are checked for filesystem links/reparse points. These checks are not protection against an adversarial same-user process racing filesystem changes. The generated `runtime-bundle-files.json` inventories shipped files; it is build evidence, not a publisher signature.

Builder boundary verification uses synthetic assets without GUI/OCR execution: **17 passed, 1 skipped**. The skipped case requires creating a filesystem symlink, which this host denied; archive-symlink refusal was exercised successfully. Actual binary execution, clean-machine prerequisites, runtime DLL resolution, and the CPython security gap remain separate qualification work.
