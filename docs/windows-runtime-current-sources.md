# Windows release runtime: current upstream sources

Checked **2026-09-26** against official Python release metadata, version-specific
PyPI JSON, and the upstream-recommended Windows OCR wheel publisher's live
GitHub API. Implementation-support research only: no binary download, install,
execution, signature verification, clean-machine qualification, or security audit
was performed for this note.

## Decision

Use **standard, GIL-enabled CPython 3.14.7 Windows x64 embedded** as the release
candidate, with **tesserocr 2.11.0 / Tesseract 5.5.3**. Preserve the current 0P
dependency versions except tesserocr; replace the Python ZIP and the three
ABI-specific wheels: Pillow, cffi, and tesserocr. The resulting 15 pinned inputs
total **118,865,759 bytes**. This is metadata compatibility, not a passing runtime
qualification. The bundled cryptography verifier must still be executed with the
candidate interpreter.

The smallest interpreter ABI step is **cp313 / Python 3.13.15**, also available
with Windows x64 OCR wheels. However, Python's published schedule expects its
last regular binary release on **2026-10-06**; 3.14's corresponding date is
**2027-10-05**. Prefer 3.14 to avoid another imminent binary-maintenance migration.
Those future releases are schedules, not available artifacts.
[3.13.15 release](https://www.python.org/downloads/release/python-31315/),
[3.14.7 release](https://www.python.org/downloads/release/python-3147/),
[3.13 schedule](https://peps.python.org/pep-0719/),
[3.14 schedule](https://peps.python.org/pep-0745/).

Both actual releases above are dated 2026-08-05. The official 3.14.7 release
includes security fixes for HTML/XML parsing complexity, archive extraction,
HTTP response bounds, and bundled Expat. Its published SBOM identifies Expat
2.8.2 and OpenSSL 3.5.7. This closes the particular **3.12.10 binary versus later
3.12 security releases** gap. It does not establish that all shipped native
libraries have no known vulnerabilities or that unpublished fixes do not exist.
[Tagged 3.14.7 security changes](https://raw.githubusercontent.com/python/cpython/v3.14.7/Misc/NEWS.d/3.14.7.rst),
[3.14.7 SBOM](https://www.python.org/ftp/python/3.14.7/python-3.14.7-embed-amd64.zip.spdx.json),
[3.12.14 source-only policy](https://www.python.org/downloads/release/python-31214/).

## Four replacement pins for cp314

These are publisher metadata values. Artifact bytes must be checked against
both SHA-256 and size at build time. Python SHA-256 comes from the exact ZIP's
SPDX `SPDXRef-PACKAGE-cpython`; size comes from HTTPS HEAD Content-Length.
Wheel values come from the source URLs included below. No signature was checked.

```json
[
  {
    "kind": "embedded",
    "filename": "python-3.14.7-embed-amd64.zip",
    "url": "https://www.python.org/ftp/python/3.14.7/python-3.14.7-embed-amd64.zip",
    "sha256": "d297e5ff019966817ad8502465176139f2d3d840fa4ed84b13bed399a6ab1f15",
    "size": 12673227,
    "metadata_source": "https://www.python.org/ftp/python/3.14.7/python-3.14.7-embed-amd64.zip.spdx.json"
  },
  {
    "kind": "wheel",
    "filename": "pillow-12.3.0-cp314-cp314-win_amd64.whl",
    "url": "https://files.pythonhosted.org/packages/f1/e0/492879f69d94f91f60fc8cd05ba03650e9520afebb2fb7aa12777d7c7f38/pillow-12.3.0-cp314-cp314-win_amd64.whl",
    "sha256": "fdafc9cce40277e0f7a0feabce0ee50dd2fa1800f3b38015e51296b5e814048d",
    "size": 7237707,
    "metadata_source": "https://pypi.org/pypi/Pillow/12.3.0/json"
  },
  {
    "kind": "wheel",
    "filename": "cffi-2.1.1-cp314-cp314-win_amd64.whl",
    "url": "https://files.pythonhosted.org/packages/a7/06/1c3e01e3ba14c39f6d10bfbac52753b7e22259e38088e5cfe1d704918690/cffi-2.1.1-cp314-cp314-win_amd64.whl",
    "sha256": "3222ba5d678f80a030e6afbcc33dc1ae5cb45facabb61cee2c7016b8432fde48",
    "size": 187949,
    "metadata_source": "https://pypi.org/pypi/cffi/2.1.1/json"
  },
  {
    "kind": "wheel",
    "filename": "tesserocr-2.11.0-cp314-cp314-win_amd64.whl",
    "url": "https://github.com/simonflueckiger/tesserocr-windows_build/releases/download/tesserocr-v2.11.0-tesseract-5.5.3/tesserocr-2.11.0-cp314-cp314-win_amd64.whl",
    "sha256": "823215ecc09a7bd3f62e8c374fcad3cc2e22ec4dbda076a6c73037392b1fa3ed",
    "size": 4365116,
    "metadata_source": "https://api.github.com/repos/simonflueckiger/tesserocr-windows_build/releases/tags/tesserocr-v2.11.0-tesseract-5.5.3"
  }
]
```

The Python release also publishes the exact ZIP's
[Sigstore bundle](https://www.python.org/ftp/python/3.14.7/python-3.14.7-embed-amd64.zip.sigstore).
An upstream API digest pins content; it is not an independently verified signer.

## Retained pins and dependency compatibility

The remaining exact filenames/URLs/hashes/sizes already reside in
[`tools/runtime-0p-assets.json`](../tools/runtime-0p-assets.json). Retain these
eleven artifacts byte-for-byte. PyPI release and dependency metadata were
refreshed live for this note; model hashes/sizes are inherited from the accepted
0P lock and were not re-downloaded here.

| Artifact | Bytes | SHA-256 | Version-specific source |
| --- | ---: | --- | --- |
| `pyside6_essentials-6.11.2-cp310-abi3-win_amd64.whl` | 76913043 | `c8a29def77032773a30879f7f24415b5395ad08592d147c170824ef4c735dfc1` | [PyPI JSON](https://pypi.org/pypi/PySide6_Essentials/6.11.2/json) |
| `shiboken6-6.11.2-cp310-abi3-win_amd64.whl` | 1226578 | `6ab0eba1c904455df621f9a6df3ca2bb896bab8670572d2bc4e37804ae91f19a` | [PyPI JSON](https://pypi.org/pypi/shiboken6/6.11.2/json) |
| `pypdfium2-5.13.0-py3-none-win_amd64.whl` | 3885553 | `47dcca2a8d507b5fd24f94c3c9d48fb379430f097bc20f01beff6c963ffbcedb` | [PyPI JSON](https://pypi.org/pypi/pypdfium2/5.13.0/json) |
| `pypdf-6.19.0-py3-none-any.whl` | 395480 | `7e5d6e730e7dae87d560a2cee218b852f6498c8be61966f3cd02ead971e48d14` | [PyPI JSON](https://pypi.org/pypi/pypdf/6.19.0/json) |
| `cryptography-50.0.1-cp39-abi3-win_amd64.whl` | 3875429 | `55d16b1ef3ee0958d893a977b19777887e546c9954ea81b200c3301a864013f2` | [PyPI JSON](https://pypi.org/pypi/cryptography/50.0.1/json) |
| `pycparser-3.0-py3-none-any.whl` | 48172 | `b727414169a36b7d524c1c3e31839a521725078d7b2ff038656844266160a992` | [PyPI JSON](https://pypi.org/pypi/pycparser/3.0/json) |
| `qrcode-8.2-py3-none-any.whl` | 45986 | `16e64e0716c14960108e85d853062c9e8bba5ca8252c0b4d0231b9df4060ff4f` | [PyPI JSON](https://pypi.org/pypi/qrcode/8.2/json) |
| `colorama-0.4.6-py2.py3-none-any.whl` | 25335 | `4f1d9991f5acc0ca119f9d443620b77f9d6b33703e51011c16baf57afb285fc6` | [PyPI JSON](https://pypi.org/pypi/colorama/0.4.6/json) |
| `eng.traineddata` | 4113088 | `7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2` | [Pinned upstream asset](https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/87416418657359cb625c412a48b6e1d6d41c29bd/eng.traineddata) |
| `rus.traineddata` | 3861738 | `e16e5e036cce1d9ec2b00063cf8b54472625b9e14d893a169e2b0dedeb4df225` | [Pinned upstream asset](https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/87416418657359cb625c412a48b6e1d6d41c29bd/rus.traineddata) |
| Model `LICENSE` | 11358 | `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30` | [Pinned upstream license](https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/87416418657359cb625c412a48b6e1d6d41c29bd/LICENSE) |

PySide6 Essentials and shiboken6 both declare `>=3.10,<3.15`, and Essentials
requires exactly matching shiboken6. The `cp310-abi3` wheels cover standard
CPython 3.14; they are not free-threaded wheels. pypdfium2's Windows wheel uses
ctypes/native PDFium without a CPython-specific extension ABI. Cryptography's
existing `cp39-abi3` wheel can remain; its dependency is `cffi>=2.0.0` on CPython,
and cffi requires pycparser. No typing-extensions dependency is selected on 3.14.
qrcode requires colorama on Windows. These facts follow the JSON sources above;
actual imports, DLL resolution, PDF AES behavior and signature verification
remain candidate execution checks.

## OCR upstream distinction

PyPI tesserocr 2.11.0 has no Windows wheel in its release JSON. Its own
[Windows instructions](https://pypi.org/project/tesserocr/2.11.0/) direct users to
the [simonflueckiger Windows release](https://github.com/simonflueckiger/tesserocr-windows_build/releases/tag/tesserocr-v2.11.0-tesseract-5.5.3).
That release was published **2026-09-12T21:36:12Z** and is a third-party binary
build recommended by upstream, not a PSF/Tesseract-team binary.

Its x64 release notes identify Tesseract 5.5.3, Leptonica 1.87.0, libgif 6.1.3,
libjpeg 10, libpng 1.6.58, libtiff 4.7.1, zlib 1.3.2, libwebp 1.6.0 and
libopenjp2 2.5.4. Tesseract 5.5.3 itself was released 2026-07-24 and includes
fixes to traineddata memory safety and LSTM integer overflow.
[Tesseract release](https://github.com/tesseract-ocr/tesseract/releases/tag/5.5.3).

The Windows publisher bundles **cysignals 1.12.6** under `tesserocr/cysignals`,
patches the import to be relative, and removes the external `install_requires`
entry. Therefore do not add a separate PyPI cysignals wheel just because generic
tesserocr PyPI metadata lists it. Preserve the supplier's complete package tree;
verify its bundled cysignals import in the embedded candidate.
[Tagged build recipe](https://raw.githubusercontent.com/simonflueckiger/tesserocr-windows_build/tesserocr-v2.11.0-tesseract-5.5.3/appveyor.yml),
[Tagged setup patch](https://raw.githubusercontent.com/simonflueckiger/tesserocr-windows_build/tesserocr-v2.11.0-tesseract-5.5.3/res/patches/tesserocr/setup.py.patch).

The English/Russian `tessdata_fast` models stay commit-pinned. Upstream describes
these as LSTM-only models suitable for Tesseract 4/5; use the existing LSTM mode
and retain both language assets. The new Tesseract/Python combination still
requires the same synthetic English/Russian worker qualification.
[Pinned model README](https://github.com/tesseract-ocr/tessdata_fast/blob/87416418657359cb625c412a48b6e1d6d41c29bd/README.md).

The web-indexed aggregate GitHub release list initially omitted the newest
5.5.3 build. The direct live `releases/latest` and exact-tag API responses above
resolved this; do not choose a 2.11.0 wheel from the separately published
Tesseract 4.1.3 or 3.05.02 release families.

## License inventory to preserve

This is an upstream license inventory, not clearance for public redistribution.
Preserve extracted notices and license files; review obligations against the
actual delivered binaries and their bundled libraries.

| Component | Upstream license evidence |
| --- | --- |
| CPython | PSF-2.0 for CPython in the exact SBOM; retain `python/LICENSE.txt` and included third-party notices. [Python license](https://docs.python.org/3.14/license.html) |
| PySide6 Essentials / shiboken6 | PyPI declares `LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only`; Qt also has component-specific third-party terms. Keep shared-library layout, licenses and the corresponding-source/replacement path required by the chosen license. [Qt for Python licenses](https://doc.qt.io/qtforpython-6/licenses.html) |
| pypdfium2 / PDFium | Apache-2.0 or BSD-3-Clause for pypdfium2; PDFium and its dependencies have separate notices which upstream requires with binary distributions. [Upstream licensing](https://pypi.org/project/pypdfium2/5.13.0/#licensing) |
| tesserocr | MIT. [v2.11.0 license](https://github.com/sirfz/tesserocr/blob/v2.11.0/LICENSE) |
| Bundled cysignals 1.12.6 | LGPLv3 or later, not covered solely by tesserocr's MIT notice. Preserve its license/source/replacement obligations. [Version metadata](https://pypi.org/pypi/cysignals/1.12.6/json) |
| Tesseract / models | Apache-2.0. [Engine license](https://raw.githubusercontent.com/tesseract-ocr/tesseract/5.5.3/LICENSE), [model license](https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/87416418657359cb625c412a48b6e1d6d41c29bd/LICENSE) |
| Leptonica | BSD-style two-condition license requiring binary-distribution notices. Codec licenses remain additional. [1.87.0 license](https://raw.githubusercontent.com/DanBloomberg/leptonica/1.87.0/leptonica-license.txt) |
| Pillow / cryptography / cffi / pypdf / pycparser | PyPI declares MIT-CMU / Apache-2.0 OR BSD-3-Clause / MIT-0 / BSD-3-Clause / BSD-3-Clause respectively; bundled native-library notices remain additional. See version-specific metadata above. |
| qrcode / colorama | BSD three-clause notices; qrcode also retains MIT attribution for its original QR implementation. [qrcode 8.2 license](https://raw.githubusercontent.com/lincolnloop/python-qrcode/v8.2/LICENSE), [colorama 0.4.6 license](https://raw.githubusercontent.com/tartley/colorama/0.4.6/LICENSE.txt) |

## Builder implications and remaining checks

The approved implementation keeps the existing `build_bundle` seam and upgrades
its default lock to the four cp314 replacement pins above. The previous 3.12.10
mechanical result remains historical in `runtime-0p-qualification.md`; it does
not qualify the new candidate. Use `python314._pth` with `python314.zip`, `.`,
`../vendor`, `..` and no `import site`, preserving absolute bundled child
invocation with `-I -B`. The builder now requires exactly one root standard
library ZIP and one matching `._pth` before configuring it. Its notices point
to the locked metadata and retain the production/clean-machine qualification
hold. Preserve unpacked wheel package
layout, native DLLs, Qt platform plugins and notices. Embed the web/static
assets required by the complete product separately from 0P's narrow allowlist.
[Embedded distribution](https://docs.python.org/3.14/using/windows.html#the-embeddable-package),
[path initialization](https://docs.python.org/3.14/library/sys_path_init.html#pth-files).

Before a release claim, verify actual hashes/lengths, bundled imports including
cysignals and cryptography, signature verification positive/negative cases,
synthetic PDF and eng/rus OCR children, and DLL prerequisites on clean Windows.
This note did none of those executions. It neither changes live Vault nor
authorizes server, phone, key, publication, or data operations.

Bounded builder verification on 2026-09-26 used synthetic archives only. The
new `build_bundle` cp314 fixture first failed because the old builder left
`python314._pth` unconfigured, then the focused builder suite passed:
**18 passed, 1 skipped, 0.74 s**. The skipped existing test requires permission
to create a real filesystem symlink. Command: build-host `.venv` Python with
`-B -m pytest tests/test_runtime_bundle.py -q -p no:cacheprovider`. There was no
runtime artifact download, installation or candidate execution.

## Inno Setup metadata only

`ISCC.exe` was not found on PATH or the standard Inno Setup 6 Program Files
paths. This is a bounded search, not proof that no compiler exists anywhere.
The official downloads page currently lists **Inno Setup 7.1.0 x64**, released
2026-08-12. The official page/latest GitHub release exposes installers, signatures
and release notes; **no portable compiler ZIP was found in that searched set**.
Do not substitute a third-party portable package.
[Official downloads](https://jrsoftware.org/isdl.php),
[official release](https://github.com/jrsoftware/issrc/releases/tag/is-7_1_0).

- Asset: [innosetup-7.1.0-x64.exe](https://github.com/jrsoftware/issrc/releases/download/is-7_1_0/innosetup-7.1.0-x64.exe).
- Size: `14304168`; SHA-256: `0362a383ed217d4c4239b5933866dd96d3eb2102737da92f80f6057a4b40df2f`.
- Metadata: [exact release API](https://api.github.com/repos/jrsoftware/issrc/releases/tags/is-7_1_0).
- The official page names Pyrsys B.V. as Authenticode publisher. This note did not verify a local signature or download the executable.
- The [version-tagged license](https://raw.githubusercontent.com/jrsoftware/issrc/is-7_1_0/license.txt) permits use including commercial applications under its stated notice/origin conditions; the website requests commercial users purchase a license. No purchase or installation was performed.

## Minimal cp313 fallback metadata

Use only if cp314 qualification exposes a concrete compatibility failure.
Keep the same general dependency versions, but use these exact alternatives:

| Asset | Bytes | SHA-256 | Source |
| --- | ---: | --- | --- |
| `python-3.13.15-embed-amd64.zip` | 11009825 | `d1f04d990aee1253d8569e8e5104e30fa9f5fa830899f14843448872d936a2cf` | [ZIP](https://www.python.org/ftp/python/3.13.15/python-3.13.15-embed-amd64.zip), [SBOM](https://www.python.org/ftp/python/3.13.15/python-3.13.15-embed-amd64.zip.spdx.json) |
| `pillow-12.3.0-cp313-cp313-win_amd64.whl` | 7239691 | `1cca606cd25738df4ed873d5ad46bbdb3d83b5cbca291f6b4ff13a4df6b0bbe8` | [Wheel](https://files.pythonhosted.org/packages/a6/9b/7a58e61d62be561da3a356fe2384d4059a6345fc130e23ef1c36a5b81d24/pillow-12.3.0-cp313-cp313-win_amd64.whl), Pillow JSON above |
| `cffi-2.1.1-cp313-cp313-win_amd64.whl` | 185688 | `1aa5645c30469b09530c4ebca77ebf8f17618293c58f8549cb1a543a50236e7d` | [Wheel](https://files.pythonhosted.org/packages/60/a6/8b149b2c3f2e11aaa1618ef64500b45f50f22c57a977a4dff1aff1f91042/cffi-2.1.1-cp313-cp313-win_amd64.whl), cffi JSON above |
| `tesserocr-2.11.0-cp313-cp313-win_amd64.whl` | 4222406 | `41958c5b8a030189e5733dc16317705a0fd7910f247fbca09d4c9a5bd152969b` | [Wheel](https://github.com/simonflueckiger/tesserocr-windows_build/releases/download/tesserocr-v2.11.0-tesseract-5.5.3/tesserocr-2.11.0-cp313-cp313-win_amd64.whl), exact release API above |
