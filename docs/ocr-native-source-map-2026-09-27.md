# OCR native source and notice map

Date: 2026-09-27. Scope: the Windows CPython 3.14 tesserocr wheel named in `tools/runtime-0p-assets.json`. This is a bounded engineering research note, not legal clearance or release approval. Only public primary-source pages/source text were read. No archives were downloaded, code run, publishers contacted, keys read, or product files changed.

## Outcome

The important addition is **cysignals: it is LGPL-3.0-or-later, not covered by tesserocr's MIT notice**. The exact upstream 1.12.6 project metadata says so, and the tagged license contains the LGPLv3 terms. Its source/replacement assessment belongs alongside the existing Qt gate, not just in a list of permissive codec notices. [cysignals 1.12.6 metadata](https://raw.githubusercontent.com/sagemath/cysignals/1.12.6/pyproject.toml), [license](https://raw.githubusercontent.com/sagemath/cysignals/1.12.6/LICENSE)

The native build recipe is available, but source correspondence is not yet closed: cysignals and the Software Network codec resolution are not fully pinned by that recipe. The remaining work is small enough to name concretely below; it is not a reason to relabel the current private qualification installer as publicly redistributable. [publisher build recipe](https://raw.githubusercontent.com/simonflueckiger/tesserocr-windows_build/tesserocr-v2.11.0-tesseract-5.5.3/appveyor.yml)

## Identity and provenance

Local reference, not re-hashed in this research: the repository manifest names `tesserocr-2.11.0-cp314-cp314-win_amd64.whl`, 4,365,116 bytes, SHA-256 `823215ecc09a7bd3f62e8c374fcad3cc2e22ec4dbda076a6c73037392b1fa3ed`. The preceding [local inventory report](redistribution-payload-audit-2026-09-26.md) and [source review](redistribution-primary-sources-2026-09-26.md) remain the evidence for its contents. This note does not independently attest those bytes.

The exact publisher release links the CPython 3.14 x64 asset and identifies Tesseract 5.5.3, Leptonica 1.87.0, and cysignals **1.12.6 for Python >=3.12**. The publisher tag points to commit `cb737e3`. It lists gif 6.1.3, jpeg 10, png 1.6.58, tiff 4.7.1, zlib 1.3.2, webp 1.6.0 and openjp2 2.5.4; zstd/lzma are not in that printed list. [exact publisher release](https://github.com/simonflueckiger/tesserocr-windows_build/releases/tag/tesserocr-v2.11.0-tesseract-5.5.3)

The tagged build recipe:

- Selects Leptonica `13275a2` and Tesseract `db0ec62` for its Tesseract 5 branch; builds shared libraries with Visual Studio 2022, disabling Tesseract training tools and OpenMP.
- Downloads a moving `sw-master` client, rather than a pinned client digest.
- Initializes the tesserocr submodule and applies three named patches, including the bundled cysignals import and wheel-copy changes.
- Installs cysignals **from source without a version/hash constraint**, then copies its Python package directory into `tesserocr/cysignals`. This is not a copy of the original distribution metadata directory.
- Collects native dependencies from its build artifact and bundles them.

These are inspected recipe properties, not proof of the exact CI run/toolchain or a reproducible local rebuild. [tagged recipe, especially lines 48–59, 90–119, 261–291 and 379–386](https://raw.githubusercontent.com/simonflueckiger/tesserocr-windows_build/tesserocr-v2.11.0-tesseract-5.5.3/appveyor.yml)

The upstream cysignals 1.12.6 release points to `117f187`. Its maintainer's PyPI page supplies a candidate source archive, `cysignals-1.12.6.tar.gz`, SHA-256 `3ef3a37bdb244821b85475a08e2762ca1019570b369e321504995fa9a54675ce`. **Neither is established here as the exact input used in the OCR wheel.** Preserve the distinction between publisher-declared version, available upstream source, and verified binary/source correspondence. [upstream release](https://github.com/sagemath/cysignals/releases/tag/1.12.6), [maintainer PyPI source record](https://pypi.org/project/cysignals/1.12.6/)

Leptonica's selected source names Software Network gif/jpeg/openjpeg/png/tiff/webp dependencies without exact versions in `sw.cpp`. Therefore a tag for Leptonica alone is not the complete codec build lock. [selected Leptonica recipe](https://raw.githubusercontent.com/DanBloomberg/leptonica/13275a2/sw.cpp)

## Exact-version notice retrieval map

“Readable” means the linked tagged upstream text was read in this pass. It does not establish that every upstream file, optional component, or third-party header is covered, or that a particular DLL was built from that tag. Preserve full texts in a future notice bundle; this table is not a substitute.

| Component | Primary notice/source | Finding and bounded next action |
| --- | --- | --- |
| tesserocr 2.11.0 | [tagged LICENSE](https://raw.githubusercontent.com/sirfz/tesserocr/v2.11.0/LICENSE) | Readable MIT notice. Preserve it, and associate publisher patches rather than describing this wheel as unmodified upstream. |
| Tesseract 5.5.3 | [tagged LICENSE](https://raw.githubusercontent.com/tesseract-ocr/tesseract/5.5.3/LICENSE) | Readable Apache-2.0. Supply the license; evaluate applicable source notices and any NOTICE file in the actual source distribution. This pass does not assert that NOTICE is absent. |
| Leptonica 1.87.0 | [tagged leptonica-license.txt](https://raw.githubusercontent.com/DanBloomberg/leptonica/1.87.0/leptonica-license.txt) | Readable BSD-style two-condition notice, including binary-distribution attribution. Preserve full notice. |
| cysignals 1.12.6 | [tagged LICENSE](https://raw.githubusercontent.com/sagemath/cysignals/1.12.6/LICENSE), [version/license metadata](https://raw.githubusercontent.com/sagemath/cysignals/1.12.6/pyproject.toml) | Readable LGPLv3 text; metadata specifies LGPLv3-or-later. Need library attribution, GPLv3 plus LGPLv3 texts, source-delivery and compatible replacement/relink assessment for the actual nested native module. |
| libpng 1.6.58 | [tagged LICENSE](https://raw.githubusercontent.com/glennrp/libpng/v1.6.58/LICENSE) | Readable PNG Reference Library License v2 and historical notices. Preserve whole text; it also warns that some contributed/generated files have other licensing. |
| libtiff 4.7.1 | [tagged LICENSE.md](https://gitlab.com/libtiff/libtiff/-/raw/v4.7.1/LICENSE.md) | Readable main LibTIFF license **plus a separate LZW/Berkeley notice**. Do not copy only the first section. |
| zlib 1.3.2 | [tagged LICENSE](https://raw.githubusercontent.com/madler/zlib/v1.3.2/LICENSE) | Readable zlib terms and copyright. Preserve the complete text. |
| libwebp 1.6.0 | [tagged COPYING](https://raw.githubusercontent.com/webmproject/libwebp/v1.6.0/COPYING), [tagged PATENTS](https://raw.githubusercontent.com/webmproject/libwebp/v1.6.0/PATENTS) | Readable BSD-style notice and separate patent grant. Carry both as upstream materials; this is not patent clearance. |
| OpenJPEG 2.5.4 | [tagged LICENSE](https://raw.githubusercontent.com/uclouvain/openjpeg/v2.5.4/LICENSE) | Readable BSD-2-Clause text with multiple contributor copyrights. Preserve whole file. |
| Zstandard 1.5.7 | [tagged LICENSE](https://raw.githubusercontent.com/facebook/zstd/v1.5.7/LICENSE) | Readable BSD license path. This map does not choose an alternative license or prove which upstream files entered the observed DLL. |
| liblzma / XZ 5.8.3 | [tagged COPYING](https://raw.githubusercontent.com/tukaani-project/xz/v5.8.3/COPYING) | Readable scope summary: liblzma is 0BSD; some other XZ tools/scripts use other licenses. Fetch the referenced full license texts after exact compiled-file scope is fixed; do not apply a blanket GPL label to liblzma or blanket 0BSD to all XZ utilities. |
| IJG JPEG 10 | [official release directory](https://www.ijg.org/files/), [official directory README](https://www.ijg.org/files/README) | Release `jpegsr10.zip` / `jpegsrc.v10.tar.gz` exists. **Exact release-10 embedded README/license not read**: no archive downloads permitted in this task. The directory README requests acknowledgement and directs readers to the distribution for terms; it is not the complete notice. |
| giflib 6.1.3 | [publisher release directory](https://sourceforge.net/projects/giflib/files/giflib-6.x/) | Exact source archive is listed. **Exact COPYING not verified**: project site timed out and the raw SourceForge code URL was inaccessible to the browser tool. Do not fill this row with a different-version mirror's text. |

The publisher's actual patch files are available separately and should be retained with any matched source/build record. One alters package/DLL handling; another makes the cysignals import relative. [setup.py patch](https://raw.githubusercontent.com/simonflueckiger/tesserocr-windows_build/tesserocr-v2.11.0-tesseract-5.5.3/res/patches/tesserocr/setup.py.patch), [tesserocr.pyx patch](https://raw.githubusercontent.com/simonflueckiger/tesserocr-windows_build/tesserocr-v2.11.0-tesseract-5.5.3/res/patches/tesserocr/tesserocr.pyx.patch)

## Actionable closeout, without expanding this task

1. **Local byte correspondence:** retain the existing wheel hash evidence; inspect bundled cysignals source/version markers against 1.12.6 and seek the build input/CI record. The release assertion is useful, not a source hash. No contact is authorized by this note.
2. **Notice payload:** stage a hashed notice manifest from the readable sources above, including cysignals GPL/LGPL materials and TIFF's second notice. Separately retrieve exact GIF/JPEG archive notices and XZ's referenced license texts before marking the set complete.
3. **Source/replacement gate:** retain the exact publisher tag/submodule/patch set and resolve cysignals plus codec build inputs. Assess replacement of the nested cysignals module, not just top-level Qt DLLs. This is an engineering check derived from the [LGPL section 4 conditions](https://raw.githubusercontent.com/sagemath/cysignals/1.12.6/LICENSE), not a legal opinion on the entire application.
4. **Do not overstate:** this source map neither copies licenses into the installer nor closes corresponding-source obligations. Models, CPython, Pillow, PDFium, Qt and Microsoft runtime DLLs remain separate inventory/notice tracks.

More than ten primary pages were necessary because the requested native component list itself exceeds ten independently licensed entries. The investigation stopped at exact-file mapping and the named gaps; no general licensing research or new software execution was performed.
