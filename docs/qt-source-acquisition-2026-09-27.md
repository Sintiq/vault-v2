# Qt 6.11.2 source inputs acquired

Date: 2026-09-27. Offline release-kit preparation, not release clearance.
Four source archives were downloaded through Qt's official HTTPS download
service. Each local length and SHA256 matches its published mirror-details
page. All four remain intact outside the repository (73,028,200 bytes total).
No code from an archive was imported, compiled, installed or executed.

| Archive | Bytes | Published and locally matched SHA256 |
| --- | --- | --- |
| [qtbase-everywhere-src-6.11.2.tar.xz](https://download.qt.io/archive/qt/6.11/6.11.2/submodules/qtbase-everywhere-src-6.11.2.tar.xz.mirrorlist) | 50582668 | 5b2e00eccaf5a4d8c14134ffa0ea8dfd0a35ae1ffc7f8d87fa4305a1ed23cf22 |
| [qtsvg-everywhere-src-6.11.2.tar.xz](https://download.qt.io/archive/qt/6.11/6.11.2/submodules/qtsvg-everywhere-src-6.11.2.tar.xz.mirrorlist) | 2341860 | d594337feca84c26fb67fe87b85e6a5c12fda404b611d905f9d138210c311876 |
| [qtimageformats-everywhere-src-6.11.2.tar.xz](https://download.qt.io/archive/qt/6.11/6.11.2/submodules/qtimageformats-everywhere-src-6.11.2.tar.xz.mirrorlist) | 2050424 | cecd8900f34b6550076309bc94f62f828008b633a4239e0a08c86788f41001f8 |
| [pyside-setup-everywhere-src-6.11.2.tar.xz](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.11.2-src/pyside-setup-everywhere-src-6.11.2.tar.xz.mirrorlist) | 18053248 | cba47efbaad1bedd529725cbc14e21f156c7a19366f07b3edfbb076ffd7afdf8 |

Matching an HTTPS-published hash is stronger than a local hash alone, but is
not an independently verified signature or proof these were the wheel's exact
build inputs. Build configuration, patches and binary correspondence remain
open as listed in the [source map](qt-minimal-source-map-2026-09-27.md).

## Bounded metadata collection

A private data-only collector rechecked hashes and read tar members without
extracting executable code. It rejected duplicate/unsafe paths and selected
only regular bounded files: raw `qt_attribution.json` metadata, explicit
resolvable `LicenseFile` references, root/module LICENSES entries and
filename-matched notice/license/copyright/patent material. Whole source
archives remain available for unresolved references and headers.

| Module | Attribution JSON files | Retained evidence files |
| --- | --- | --- |
| qtbase | 76 | 188 |
| qtsvg | 1 | 11 |
| qtimageformats | 2 | 13 |
| pyside-setup | 5 | 32 |
| Total | 84 | 244 |

This is **not** Qt's `qtattributionsscanner`, not an equivalent implementation,
and not a compiled-component inventory. It deliberately includes source-only
and example metadata; some entries do not apply to the shipped subset.
The first attempt stopped on a strict-JSON parser error. A second, separately
retained attempt preserved every raw metadata file and recorded seven strict
parse failures and eleven records without an explicit `LicenseFile` instead
of guessing or silently dropping them. At least one raw file uses a literal
newline in a JSON string. The report does not characterize these cases as Qt
scanner failures. All explicit references parsed by this collector resolved.

The resulting evidence index records each retained file's length and SHA256,
with `complete=false` and `binary_source_correspondence_attested=false`.
Its SHA256 is `15d4219baa0329ef8dcce24c348bc29aa516f05b82362324aef148fe826aeed5`;
the private collector's SHA256 is
`b54da6b12ac453101b06650225aaca73c6ba48fba863b1ca5582013dd899b223`.
No notices were inserted into qualification11; that installer remains unchanged.
Next: reconcile actual retained DLL build scope, inspect the flagged metadata
using upstream semantics, collect relevant source-header notices and preserve
the exact source/build/replacement kit alongside the future distributable.

One material distinction already visible: Qt Image Formats metadata names
LibTIFF 4.7.2, while the OCR publisher names LibTIFF 4.7.1. Do not merge those
two independent dependency copies into one version/notice claim.

## Follow-up: parser and delivery draft

The [dependency-kit draft](dependency-kit-draft-2026-09-27.md) now physically
includes all four archives and the 244 original notice/metadata files. A new
bounded collection used Qt QJsonDocument 6.11.2: all 84 metadata files parsed;
plural LicenseFiles and CopyrightFile(s) were resolved as well. No explicit
reference remained unresolved. Twelve records with inline/no-file-reference
fields remain raw and disclosed. Selected bytes are unchanged from this report's
244-file set. This is not qtattributionsscanner or compiled-component coverage;
the seven earlier strict-Python-JSON failures are not Qt parser failures.
