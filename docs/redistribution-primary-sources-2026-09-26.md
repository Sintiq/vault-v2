# Redistribution: bounded primary-source review

Date: 2026-09-26. Scope: pinned PySide6_Essentials 6.11.2, shiboken6 6.11.2, and the Windows tesserocr 2.11.0 build reporting Tesseract 5.5.3. This is an engineering release-gap checklist, not legal clearance. No binaries or source archives were downloaded or executed; no publication, account action, or product change was performed.

## Local evidence boundary

The coordinating agent independently inspected the actual wheel ZIPs and reported:

- Both Qt wheels advertise `LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only` in METADATA, but their packaged license directory contains only `LicenseRef-Qt-Commercial.txt`.
- The tesserocr wheel contains its MIT LICENSE only. Across 11 wheels, all 31 notice entries found by the local filename scan were preserved byte-for-byte by packaging. This establishes preservation of those entries, not completeness of upstream notices.
- The extracted Essentials payload has 106 native files directly under `vendor/PySide6`, plus plugins/QML, including Virtual Keyboard and PDF QML resources and a Qt6QuickTimeline DLL. Resource presence alone does not establish that a complete usable Qt PDF or Virtual Keyboard native module is included.
- The pinned OCR wheel is 4,365,116 bytes, SHA-256 `823215ecc09a7bd3f62e8c374fcad3cc2e22ec4dbda076a6c73037392b1fa3ed`. Local native filenames identify Leptonica 1.87.0, gif 6.1.3, jpeg 10, png 1.6.58, tiff 4.7.1, zlib 1.3.2, webp 1.6.0, openjp2 2.5.4, **zstd 1.5.7 and lzma 5.8.3**, plus `tesseract55.dll` and a cysignals native module. Filenames are useful evidence but are not source/build provenance; the last two codec dependencies also show that the publisher release list is not exhaustive.
- The existing README already limits OCR to private-use evaluation and does not claim redistribution readiness.

Those are supplied local findings, not conclusions inferred from online documentation. This report does not independently certify the local binary inventory or its hashes.

The README's older source-venv paragraph describes tesserocr 2.10.0 / Tesseract 5.5.2 / cysignals 1.12.6. It must not be used as the packaged Python 3.14 bundle manifest; the packaged cysignals version remains to be verified locally.

The coordinating agent's [exact local inventory](redistribution-payload-audit-2026-09-26.md) records the byte-preservation check and its filename-scan limits. The package manifest separately pins English/Russian tessdata_fast files and their license; this research report does not replace those local model hash checks.

## Component and source map

| Layer | Verified primary evidence | Remaining boundary |
| --- | --- | --- |
| PySide / Shiboken runtime wrappers | Qt's tagged PySide runtime source explicitly offers commercial, LGPLv3, GPLv2 and GPLv3 alternatives. The shared source distribution has a 6.11.2 archive listing. [Tagged runtime file](https://raw.githubusercontent.com/pyside/pyside-setup/v6.11.2/sources/pyside6/libpyside/pyside.cpp), [official source listing](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.11.2-src/) | A wrapper license expression does not identify the license of every native Qt module or third-party library copied alongside it. |
| Native Qt | Qt publishes a separate 6.11.2 source archive (`qt-everywhere-src-6.11.2.zip` / `.tar.xz`). Qt explicitly documents module-specific licenses and third-party code. [Exact archive listing](https://download.qt.io/archive/qt/6.11/6.11.2/single/), [Qt licensing](https://doc.qt.io/qt-6/licensing.html), [third-party index](https://doc.qt.io/qt-6/licenses-used-in-qt.html) | Archive existence is verified; correspondence to the exact shipped binaries, build options, patches and included components is not. |
| tesserocr wrapper | The v2.11.0 tagged MIT license requires preservation of its copyright and permission notice. Its README identifies Tesseract and Leptonica as native dependencies and points Windows users to a separate build publisher. [MIT license](https://raw.githubusercontent.com/sirfz/tesserocr/v2.11.0/LICENSE), [tagged README](https://raw.githubusercontent.com/sirfz/tesserocr/v2.11.0/README.rst) | MIT does not replace the licenses of code compiled or bundled into the Windows extension. |
| Windows OCR build | The build publisher's exact release identifies tesserocr 2.11.0, Tesseract 5.5.3 and Leptonica 1.87.0. It also lists libgif 6.1.3, libjpeg 10, libpng 1.6.58, libtiff 4.7.1, zlib 1.3.2, libwebp 1.6.0 and libopenjp2 2.5.4 for both architectures. [Publisher release](https://github.com/simonflueckiger/tesserocr-windows_build/releases/tag/tesserocr-v2.11.0-tesseract-5.5.3) | Match the local wheel to the exact release asset/hash before treating that dependency list as the local manifest. Publisher statements are first-party evidence for its build, not an independent inspection of this payload. |
| Tesseract | The official 5.5.3 release and tagged Apache-2.0 LICENSE are readable. Its tagged README distinguishes Leptonica and image-library dependencies. [Release](https://github.com/tesseract-ocr/tesseract/releases/tag/5.5.3), [LICENSE](https://raw.githubusercontent.com/tesseract-ocr/tesseract/5.5.3/LICENSE), [README](https://raw.githubusercontent.com/tesseract-ocr/tesseract/5.5.3/README.md) | Exact upstream source exists; whether this build has additional patches or notices remains to be mapped. |
| Leptonica | The 1.87.0 tagged license is readable and requires the copyright, conditions and disclaimer in binary-distribution documentation/materials. [Exact tagged license](https://raw.githubusercontent.com/DanBloomberg/leptonica/1.87.0/leptonica-license.txt) | Version attribution is conditional on the wheel match above; image-codec notices need their own review. |

## Qt: concrete obligations and the wholesale-copy issue

For components actually offered under LGPLv3, section 4 calls for a prominent notice of library use, copies of both LGPLv3 and GPLv3, and terms that permit modification of the library portions and reverse engineering to debug those modifications. If the program displays copyright notices, include the library notice and a reference to those licenses. Section 4(d) allows either a suitable shared-library mechanism that runs with an interface-compatible modified library, or the specified relinking material. Installation Information is conditional under section 4(e), referring to GPLv3 section 6; it is not an unconditional rule that every Windows download must supply device keys. [LGPLv3 text](https://doc.qt.io/qt-6/lgpl.html)

Practical release evidence should include a documented replacement procedure for the actual PySide/Shiboken/Qt DLL layout and a clean-machine check that a compatible modified library can load. Merely observing separate DLL files does not establish the runtime property. This is an engineering verification proposal derived from section 4(d), not a claim that a particular packaging format is automatically compliant. [Tagged LGPL text](https://raw.githubusercontent.com/pyside/pyside-setup/v6.11.2/LICENSES/LGPL-3.0-only.txt)

Qt's official obligations page says corresponding library source, including modifications, must be supplied or made available under an appropriate offer even for unmodified libraries. For a network download, GPLv3 section 6(d) specifies equivalent source access at no extra charge and clear directions next to the binary; third-party hosting does not remove the distributor's availability responsibility. Corresponding Source includes the relevant build/install scripts. The section 6(b) written-offer route is tied to physical-product distribution, so a generic promise to provide source on request is not a universal substitute. Free distribution does not remove these duties. [Qt obligations](https://www.qt.io/development/open-source-lgpl-obligations), [GPLv3 sections 1 and 6](https://doc.qt.io/qt-6/gpl.html)

The Essentials package name must not be treated as an LGPL classification:

- Qt Virtual Keyboard is commercial-or-GPLv3; its exact v6.11.2 source header confirms that choice. [Module documentation](https://doc.qt.io/qt-6/qtvirtualkeyboard-index.html), [tagged source](https://raw.githubusercontent.com/qt/qtvirtualkeyboard/v6.11.2/src/virtualkeyboard/qvirtualkeyboardinputcontext.cpp)
- Qt Quick Timeline is commercial-or-GPLv3, with an exact v6.11.2 source file available. [Module documentation](https://doc.qt.io/qt-6/qtquicktimeline-index.html), [tagged source](https://raw.githubusercontent.com/qt/qtquicktimeline/v6.11.2/src/timeline/qquicktimeline.cpp)
- Qt PDF offers LGPLv3/GPLv2 alternatives, but embeds PDFium and its third-party dependencies, with their separate licenses. [Qt PDF licensing](https://doc.qt.io/qt-6/qtpdf-licensing.html)

Inference for this payload: not importing an included module does not remove obligations for conveying that module's files. Conversely, mere inclusion as an independent aggregate does not by itself establish that the entire application must become GPL; GPLv3 section 5 distinguishes aggregation. Determine what is shipped, linked or loaded before choosing a license path. [GPLv3 sections 5–6](https://doc.qt.io/qt-6/gpl.html)

## OCR notice handling

Tesseract's Apache-2.0 terms require supplying that license; modified files need change notices. Preserve applicable source-form notices when distributing source, and include applicable NOTICE attributions if the upstream work includes a NOTICE file. A direct lookup of the tag's root `NOTICE` did not return a readable file in this bounded pass; this is not evidence that the entire source/build has no attribution requirements. [Tesseract 5.5.3 license, section 4](https://raw.githubusercontent.com/tesseract-ocr/tesseract/5.5.3/LICENSE)

Keep the tesserocr MIT notice, add the matched Tesseract Apache license and Leptonica notice, then map the publisher's remaining native dependencies and any bundled traineddata to their own exact provenance and notices. The tagged wrapper README treats traineddata as a separate acquisition; this review did not identify the app's model files. [tesserocr MIT](https://raw.githubusercontent.com/sirfz/tesserocr/v2.11.0/LICENSE), [Leptonica 1.87.0 license](https://raw.githubusercontent.com/DanBloomberg/leptonica/1.87.0/leptonica-license.txt), [tesserocr data instructions](https://raw.githubusercontent.com/sirfz/tesserocr/v2.11.0/README.rst)

## Five release gaps and safe next actions

1. **Actual Qt license scope is unresolved.** Produce a per-file/native-module inventory, including plugins and QML, tied to exact source modules and license choices. Resolve Virtual Keyboard/Timeline explicitly. If unnecessary, propose their exclusion with dependency checks; do not silently remove them or assume all Essentials contents are LGPL.
2. **Wheel-preserved notices are incomplete as a release notice set.** Prepare a component-to-notice manifest for the retained payload, including LGPLv3 plus GPLv3 texts where applicable and native Qt/PDFium/OCR attributions. Verify the final installer carries the complete set; preserving 31 upstream entries alone cannot close this gap.
3. **Corresponding-source delivery is not established.** Match Qt and PySide source versions, build configuration, patches and scripts to retained binaries; choose the delivery route for the intended distribution method. Exact archive URLs are starting evidence, not proof of correspondence or long-term availability.
4. **Library replacement is unverified.** Document the actual replacement/loading process and check it on a clean test machine using compatible modified libraries; review app terms and integrity/update behavior for conflicts with that process. Keep this as a separate gate from ordinary startup tests.
5. **OCR provenance stops at the wrapper/wheel.** Match the local asset/hash to the publisher release, record native dependency versions and their complete notices, and separately identify any traineddata. Do not infer a complete license package from the wrapper's MIT file.

The next bounded task is an exact native-module/source/notice inventory and a proposed minimal payload. This report does not select a commercial/GPL/LGPL route, authorize distribution, change the existing private-evaluation status, or implement packaging changes.
