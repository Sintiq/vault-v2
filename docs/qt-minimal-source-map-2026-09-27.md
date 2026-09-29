# Minimal Qt profile: source and notice map

Date: 2026-09-27. Research only; no archive or binary downloads, execution,
publication, license purchase or application-license change. This is an
engineering checklist, not legal clearance. Distribution remains unqualified.

## Exact local scope

Read `tools/qt-widgets-6.11.2-profile.json` and the prior subset/redistribution
reports. The profile selects 37 PySide6 members from
`pyside6_essentials-6.11.2-cp310-abi3-win_amd64.whl`, SHA256
`c8a29def77032773a30879f7f24415b5395ad08592d147c170824ef4c735dfc1`.
That is a local profile binding, not an independently rehashed payload here.
Shiboken and other wheels remain outside this filter. Preserved wheel notices
are inputs to the release kit, not proof that the notice set is complete.

## Source map: more than qtbase and qtsvg

The official Qt 6.11.2 module index lists these exact archives. Links below
identify candidates for a later verified acquisition; no archive hash or
binary/source correspondence is asserted by this research.
[Qt 6.11.2 source index](https://download.qt.io/archive/qt/6.11/6.11.2/submodules/)

| Retained family | Source candidate | Qualification still needed |
| --- | --- | --- |
| Qt6Core/Gui/Widgets; Windows/minimal/offscreen/direct2d platform plugins; Windows style; basic image plugins | [qtbase-everywhere-src-6.11.2.tar.xz](https://download.qt.io/archive/qt/6.11/6.11.2/submodules/qtbase-everywhere-src-6.11.2.tar.xz) | Resolve exact upstream commit, patches, build options and third-party objects compiled into each retained DLL. |
| Qt6Svg and qsvg | [qtsvg-everywhere-src-6.11.2.tar.xz](https://download.qt.io/archive/qt/6.11/6.11.2/submodules/qtsvg-everywhere-src-6.11.2.tar.xz) | Bind module source and its own notices to the shipped binary. |
| qicns, qtga, qtiff, qwbmp, qwebp | [qtimageformats-everywhere-src-6.11.2.tar.xz](https://download.qt.io/archive/qt/6.11/6.11.2/submodules/qtimageformats-everywhere-src-6.11.2.tar.xz) | Include this additional module; identify actual bundled/system codec configuration. |
| QtCore/Gui/Widgets Python wrappers, pyside6 runtime, support; separate shiboken6 wheel | [pyside-setup-everywhere-src-6.11.2.tar.xz](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.11.2-src/pyside-setup-everywhere-src-6.11.2.tar.xz) | Preserve wrapper-generation and wheel-build inputs, not merely generated DLLs. |

The PySide archive is independently listed by Qt. The tagged superproject
includes `sources/shiboken6`, `sources/shiboken6_generator` and
`sources/pyside6`; it is the shared source starting point, not a need to invent
a separate Shiboken archive. [PySide 6.11.2 archive index](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.11.2-src/),
[v6.11.2 build entrypoint](https://raw.githubusercontent.com/pyside/pyside-setup/v6.11.2/CMakeLists.txt)

Image Formats documentation explicitly identifies its optional plugin model,
LGPLv3/GPLv2 alternatives, and potentially bundled TIFF/WebP libraries.
Documentation version labels alone do not prove which codec revision was used
in this wheel. **qtbase + qtsvg alone is an incomplete source/notice plan for
the selected profile.** [Qt Image Formats](https://doc.qt.io/qt-6/qtimageformats-index.html)

## License texts and attribution extraction

The exact PySide v6.11.2 tag contains the
[LGPL-3.0-only text](https://raw.githubusercontent.com/pyside/pyside-setup/v6.11.2/LICENSES/LGPL-3.0-only.txt)
and [GPL-3.0-only text](https://raw.githubusercontent.com/pyside/pyside-setup/v6.11.2/LICENSES/GPL-3.0-only.txt).
For an LGPLv3 route, include both unabridged texts, library-use notices and
applicable component notices. Section 4 calls for a suitable shared-library
mechanism or the prescribed relinking material; separate DLL filenames alone
do not demonstrate this. Do not select the application's GPL license from the
presence of a GPL text needed alongside LGPL.

GPLv3 section 1 includes relevant build/install scripts in Corresponding
Source. For internet delivery, section 6(d) describes equivalent source access
and clear directions; an unverified upstream URL is not a completed delivery
kit. Exact build provenance and an appropriate source-delivery route remain
unresolved. [GPLv3 text](https://raw.githubusercontent.com/pyside/pyside-setup/v6.11.2/LICENSES/GPL-3.0-only.txt)

Qt's metadata is `qt_attribution.json`, not a scan for filenames containing
LICENSE. It records identifiers, versions, usage, source paths, license IDs,
copyrights and license-file references. Capture every referenced file as well
as the JSON, retaining module-relative paths. [Qt attribution metadata](https://wiki.qt.io/Qt_attribution.json)

The actual 6.11.2 `qtattributionsscanner` entrypoint supports recursive paths,
`--input-files qt_attributions`, `--output-format json` or `qdoc`, and `--output`.
It validates referenced paths by default; do not use `--no-check-paths` to hide
missing materials. A proposed later offline invocation is:

```text
qtattributionsscanner --input-files qt_attributions --output-format json --output qt-attributions.json qtbase qtsvg qtimageformats pyside-setup
```

This command was **not run**. Scanner source is in Qt Tools, whose same-version
archive is listed in the module index. A build-time scanner is not a reason to
ship Qt Tools in the application. Generated metadata is not evidence that all
listed source components were compiled into the wheel; reconcile it against
the exact build and payload. [Tagged scanner entrypoint](https://raw.githubusercontent.com/qt/qttools/v6.11.2/src/qtattributionsscanner/main.cpp)

## Two non-module provenance gaps

**Software OpenGL:** retained `opengl32sw.dll` is explicitly associated by Qt
with Mesa llvmpipe. Its attribution page contains Mesa/Khronos/Boost notices,
but does not establish this wheel's exact Mesa/LLVM revision, patches or full
build dependency set. Do not declare this DLL covered by qtbase source merely
because it sits beside Qt6Gui. Obtain its matched provenance and all applicable
notices; otherwise retain an explicit release blocker.
[Qt Mesa attribution](https://doc.qt.io/qt-6/qt-attribution-llvmpipe.html)

**Microsoft runtime:** the profile retains MSVCP/VCRUNTIME DLLs; another
MSVCP copy is placed beside Python for OCR. Microsoft documents app-local
deployment as technically possible, but restricts redistribution of individual
binaries/packages to licensed Visual Studio users under the applicable terms.
A wheel's inclusion of a DLL is not a license grant to this distributor.
Record every retained copy's hash, file/product version, original package and
applicable terms/REDIST entry; establish the owner's permitted redistribution
route before release. Do not substitute a DLL copied from Windows System32.
For app-local copies, plan servicing updates too. This review did not determine
the owner's licensing status or the wheel publisher's pass-through rights.
[Microsoft redistribution documentation](https://learn.microsoft.com/en-us/cpp/windows/redistributing-visual-cpp-files?view=msvc-170)

## Offline release-kit checklist (proposed, not completed)

1. Freeze candidate payload inventory, subset manifest and all wheel hashes.
   Map each native/Python file to source module, license choice and attribution.
2. Acquire the four exact source archives above in a separate controlled step.
   Record URL, retrieval date, SHA256, upstream published digest/signature when
   available, resolved tag/commit, and verification outcome. Do not equate an
   internally computed hash with publisher authentication.
3. Obtain or reconstruct documented matching wheel/Qt build settings, patches,
   toolchain/dependency versions and generation/build/install scripts. Mark
   unmatched binaries explicitly; do not replace them with a nearest version
   and call it Corresponding Source.
4. Generate a source-attribution report; collect referenced licenses,
   copyrights and notices. Reconcile build-enabled dependencies, including
   image codecs, Mesa/LLVM and Microsoft runtime copies. Preserve original
   notices, and add a readable component-to-file index.
5. Package `sources/`, `licenses/`, `notices/`, provenance checksums, and build
   instructions in an offline-readable release kit. Final installer must carry
   the required notices/license texts; its download location needs a verified
   corresponding-source access route. A note promising future work is not it.
6. Write and test a library replacement procedure on a disposable install:
   compatible modified PySide/Shiboken/Qt libraries must actually load, without
   an integrity check/update silently restoring originals or blocking use.
   Review application terms for prohibited reverse-engineering restrictions.
   No replacement test or determination of Installation Information duties
   was performed here.
7. Reconcile the final exact payload with this kit after build changes. Obtain
   the remaining licensing/provenance decisions before public release; this
   document does not approve distribution, paid licensing, or owner disclosure.

The ten primary evidence sources used here are the Qt module index, PySide
archive index, tagged PySide build entrypoint, two tagged license texts, Qt
Image Formats documentation, attribution metadata specification, tagged
scanner entrypoint, Qt Mesa attribution, and Microsoft redistribution guidance.
Archive links are entries in the two inspected indexes, not downloaded content.
