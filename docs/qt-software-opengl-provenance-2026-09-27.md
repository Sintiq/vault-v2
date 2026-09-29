# Software OpenGL: qualification12 provenance

Date: 2026-09-27. Read-only binary inspection and official-source acquisition;
no downloaded code executed, no product files changed, no release clearance.
This narrows the Mesa gap in [the Qt source map](qt-minimal-source-map-2026-09-27.md).

## Actual retained bytes

The inspected file is qualification12's `vendor/PySide6/opengl32sw.dll`,
not a similarly named host/System32 file. Local measurements:

- Length: **20,639,544 bytes**.
- Whole-file SHA256: `34b444c016289b560662ff896deceb7f4b2c0723aed3d319ae167c9186ce42b3`.
- Windows FileVersion/ProductName/FileDescription fields are empty.
- Embedded ASCII strings identify **Mesa 11.2.2** and **LLVM 3.6.2**
  (`llvm-mc (based on LLVM 3.6.2)`). This is static inspection, not execution.
- Windows `Get-AuthenticodeSignature` reported `Valid`, signer subject
  `The QT Company Oy`. This is this host's verification result, not an
  independent reproducible-build or lifetime security guarantee.

## Matched official binary ancestry

Downloaded three Qt-hosted archives, verified their lengths and SHA256 against
their HTTPS mirror-details pages, listed them before extraction, and extracted
only the sole `opengl32sw.dll` member. No executables were launched. The fetched
archives contain no source or notice bundle.

| Official Qt archive | Bytes | Published and locally matched archive SHA256 |
| --- | ---: | --- |
| [unsigned 2016](https://download.qt.io/development_releases/prebuilt/llvmpipe/windows/opengl32sw-64-mesa_11_2_2.7z.mirrorlist) | 5013770 | `142a74bfbe7d2b1bf633925f130b29eb10ffa9fb87cc1bbd05ee2624ee81d261` |
| [signed 2020](https://download.qt.io/development_releases/prebuilt/llvmpipe/windows/opengl32sw-64-mesa_11_2_2-signed.7z.mirrorlist) | 5017569 | `e2927d195699ef03685f38a64009b4c791d85f066a7d0ca580540eacb5b50a3d` |
| [SHA256-signed 2022](https://download.qt.io/development_releases/prebuilt/llvmpipe/windows/opengl32sw-64-mesa_11_2_2-signed_sha256.7z.mirrorlist) | 5023568 | `c61f801c1760aa24b02c7a8354323cddf368b86f8f5c34b50b3b224f7d728afd` |

The whole signed DLL hashes differ. Do not claim qualification12 is byte-for-byte
identical to the current prebuilt signed archive. Instead, a bounded comparison
establishes the same underlying PE content:

1. Read `e_lfanew` at offset 60: all four files have PE offset 288 and PE32+
   optional-header magic `0x20b`.
2. Optional header starts at 312. Its 4-byte checksum field starts at 376;
   its 8-byte Security Directory entry starts at 456.
3. Each signed file's Security Directory points to file offset 20,627,456.
   Certificate-table lengths are respectively 12,088 (qualification12),
   12,432 (2022), and 5,752 (2020); each ends exactly at that file's end.
   The unsigned member has no Security Directory and length 20,627,456.
4. Retain all bytes before that table and zero only those two header fields.
   All four results have SHA256
   `d303dd60bbf11418063f4bb72292e481ca1e88d84915fbd049a21666b92c0ee0`,
   also the **unmodified whole unsigned 2016 DLL's** SHA256.

Thus the payload's non-signature/checksum content matches Qt's official 2016
prebuilt. This is a content comparison, not an implementation of Authenticode
hashing and not proof of the source/build inputs. It does not alter any file.
The 2020 DLL's whole SHA256 is
`bca15d37fdd6dcec34a01459f7710a572b9eb7f6f8b5d382a8d66c65d65b16d5`;
2022 is `b04de4541863bc7d8879040a78889c4849c1b1da2784c4630f734c146c2998ce`.

Qt's tagged 6.11.2 provisioning script still selects Mesa `11_2_2`, pins the
2022 archive's SHA1 `58f948746696b17a594b2f542e87b0e831b28dc3`, and provisions
that prebuilt rather than building Mesa from qtbase. This is corroborating
upstream selection evidence, not the PySide wheel's complete production log.
[Tagged provisioning script](https://raw.githubusercontent.com/qt/qt5/v6.11.2/coin/provisioning/common/windows/mesa_llvmpipe.ps1)

## Source and recipe: located, not fully bound

The upstream Mesa 11.2.2 source archive was acquired separately:
**7,860,932 bytes**, SHA256
`40e148812388ec7c6d7b6657d5a16e2e8dabba8b97ddfceea5197947647bdfb4`.
This matches the publisher's release notes. Its presence is stronger than a
prospective URL, but does not establish Qt's patches/toolchain/build settings.
[Official archive](https://archive.mesa3d.org/older-versions/11.x/11.2.2/mesa-11.2.2.tar.xz),
[published digest](https://docs.mesa3d.org/relnotes/11.2.2.html)

The current Qt wiki recipe explicitly calls itself instructions for its
prebuilt DLL. However, its actual build instructions were updated to Mesa
17.2.2 and LLVM 5.0 / Visual Studio 2017, while its older identification block
still names Mesa 11.2.2 / LLVM 3.6. It mentions additional patches but does not
provide a frozen patch set for the matched 2016 binary. Therefore **do not
label today's recipe as the exact recipe for this DLL**. Wiki revision-history
access returned a login/group restriction; no login was attempted.
[Qt recipe, revision 33647](https://wiki.qt.io/index.php?title=MesaLlvmpipe&oldid=33647)

The actual 11.2.2 source's `src/gallium/targets/libgl-gdi/SConscript` links
WGL/GDI, glapi, compiler, Mesa, selected drivers, Gallium and GLSL; llvmpipe is
conditional on LLVM. This gives a concrete dependency walk starting point,
not a binary-level proof that every upstream default applies unchanged.

## Required notice work, specific to these versions

Qt's llvmpipe attribution supplies three text families: Brian Paul/Mesa MIT,
Khronos MIT-style, and yohhoy C11 thread-emulation Boost 1.0. These agree with
the component map in acquired Mesa 11.2.2 `docs/license.html`, which also says
to consult individual source files. Collect the exact headers for compiled
Mesa/Gallium/GLSL/WGL paths, not merely one generic MIT text.
[Qt attribution](https://doc.qt.io/qt-6/qt-attribution-llvmpipe.html)

**LLVM 3.6.2 is not covered by those three texts.** Its release license is
University of Illinois/NCSA, not a modern LLVM Apache-2.0-with-exception text.
It requires attribution/terms/disclaimers with binaries and identifies extra
notices in LLVM Support and other components. The release license was fetched
unaltered: 3,281 bytes, SHA256
`e3bc36440fc927c62d5cc24efeefe225a14d4e34ffeb0c92e430625cce9ee444`.
[LLVM 3.6.2 license](https://releases.llvm.org/3.6.2/LICENSE.TXT)

Two concrete Support notice candidates were fetched from the matching tag:

- `COPYRIGHT.regex`: 2,718 bytes, SHA256
  `0424e57d4303164dc59a8509c20dae0518b853692e5c2b0e98b11816fdbc97c7`.
  [Exact tagged file](https://raw.githubusercontent.com/llvm/llvm-project/llvmorg-3.6.2/llvm/lib/Support/COPYRIGHT.regex)
- `MD5.cpp`, including its notice: 9,148 bytes, SHA256
  `19edb938505f839ba5d2d96c26d5db454542cc9d22d277652f5085c62d0ac792`.
  [Exact tagged source](https://raw.githubusercontent.com/llvm/llvm-project/llvmorg-3.6.2/llvm/lib/Support/MD5.cpp)

These are acquired candidates, not an assertion that those objects are linked;
neither do they prove all other LLVM subcomponent notices inapplicable.
Do not accidentally include only build-tool/test notices and miss linked
Support objects. The full LLVM source archive was not acquired in the initial
pass; the follow-up below records its later acquisition.

All downloaded material is retained under the private temporary evidence
directory `vault-mesa-provenance-20260927`; no absolute owner path is required
in the eventual public provenance record. None was inserted into candidate12.

## Remaining blockers and next concrete work

1. **Notice delivery:** assemble the complete relevant Mesa/LLVM notices from
   these exact versions, preserving copyright holders and terms, then add
   them through the verified supplemental-notice input mechanism and verify
   the final installer's inventory. Candidate12 does not gain them merely
   because this report exists.
2. **Build/source confidence:** preserve the full LLVM 3.6.2 source and locate
   Qt's actual 2016 build inputs/patches, or explicitly document the limit of
   reconstruction. The binary ancestry is resolved; exact source correspondence
   and reproducibility are not. This report does not assert that permissive
   Mesa/LLVM licenses themselves require a reproducible-build/source offer;
   that is distinct from the project's source/provenance gate and Qt LGPL duties.
3. **Compiled-component coverage:** follow Mesa target scripts and LLVM
   libraries to reconcile retained code against per-file notices; the current
   source-level overview is not sufficient for a completeness attestation.
4. **Security/servicing:** the matching implementation is from 2016 despite
   its placement in a 6.11.2 wheel. Age alone is not a vulnerability verdict,
   but a current Qt version must not be used to claim this fallback is current.
   Assess actual fallback use/exposure and servicing before public release;
   do not silently remove this retained compatibility component to pass a gate.

No legal clearance, application-license choice, publication, or request to the
upstream maintainers was performed. Acquisition hashes authenticate against
the publisher's HTTPS metadata, not a separately verified detached signature.

## Follow-up: exact notice draft and LLVM source acquired

The full [LLVM 3.6.2 source archive](https://releases.llvm.org/3.6.2/llvm-3.6.2.src.tar.xz)
is now retained: **12,802,380 bytes**, local SHA256
`f60dc158bfda6822de167e87275848969f0558b3134892ff54fced87e4667b94`.
The byte count matches the [official index](https://releases.llvm.org/3.6.2/).
No publisher SHA256 was found in that index, the inspected download-page
section, or the bounded official-domain digest search; do not call this a
publisher-digest match. Its [detached signature](https://releases.llvm.org/3.6.2/llvm-3.6.2.src.tar.xz.sig)
is also retained: 287 bytes, SHA256
`b2e96fc46b7b13e5655b9bf9c97b812235f3c7ad61e5b10cb186b135ef8928b9`.
An already-installed GPG binary, using only a fresh isolated keyring and no
automatic key retrieval, could not verify it: `NO_PUBKEY 8F0871F202119294`.
The Hans Wennborg key linked from LLVM's 3.6.0 section is a different key;
it must not be represented as the 3.6.2 signer. No owner keys were accessed.
Consequently the LLVM archive's current assurance is official HTTPS retrieval
and local integrity, **not detached-signature verification**.

A new private `vault-mesa-provenance-20260927/supplemental-notice-draft/`
contains **15 flat .txt files, 1,062,756 bytes**. It is separate from the
existing notice bundle and every candidate12 file/manifest. Fourteen are
upstream source material; one is our explicit format/scope index. Nothing
was compiled/imported or run from either downloaded archive.

| Prepared file | Bytes | SHA256 |
| --- | ---: | --- |
| MESA-11.2.2-license-html.txt | 3763 | `288ddfae338be168ef2492c21de42ee2f5c8ebc5a734b4e58b37007141bc2d6d` |
| MESA-11.2.2-c11-threads-h.txt | 2539 | `2b3477e2a1d54869cc841c5a9195209b50609a81a7d18ffc07b343f8f7313aaf` |
| MESA-11.2.2-c11-threads-win32-h.txt | 16403 | `2d6c7d991c8f5c7a78da51cdff748bd1483a6d0558c75765253eafeff43e18d1` |
| MESA-11.2.2-GL-glext-h.txt | 797884 | `559c95f323962b9f3abfdc7be26cb661b6c136a7f27318ad2881a7680b051b27` |
| MESA-11.2.2-GL-glxext-h.txt | 45416 | `f3f6888e721cfd0b4a758c50196cad70e2f86a89c4f3f088264480cab0c5efde` |
| MESA-11.2.2-GLES2-gl2ext-h.txt | 172628 | `b06ac3aed1655ea7cefaeb855e0b06eba74ec42b3e4016ec82e8595fdcf5e87e` |
| LLVM-3.6.2-LICENSE.txt | 3281 | `e3bc36440fc927c62d5cc24efeefe225a14d4e34ffeb0c92e430625cce9ee444` |
| LLVM-3.6.2-COPYRIGHT-regex.txt | 2718 | `0424e57d4303164dc59a8509c20dae0518b853692e5c2b0e98b11816fdbc97c7` |
| LLVM-3.6.2-MD5-cpp.txt | 9148 | `19edb938505f839ba5d2d96c26d5db454542cc9d22d277652f5085c62d0ac792` |
| LLVM-3.6.2-MD5-h.txt | 1975 | `f7db858aba5c9ee861b8a8e8b80f4c943f712ca4c15fad4bc2bd002a589bb1d4` |
| LLVM-3.6.2-Support-LICENSE.txt | 284 | `a012d664e4e01df52a65b2eeafdfb8aeb856fec0e6c372265d01b0109c3f5e2a` |
| QT-6.11.2-llvmpipe-Mesa-MIT.txt | 1122 | `d08bca2f323f6789100511db06dd9ca93c0379584009ae8b6342c32fb1ca8a22` |
| QT-6.11.2-llvmpipe-Khronos.txt | 1152 | `dac43d3b982f4a93049f5d0abcaa5197d646412afbb53945cf792549992401e2` |
| QT-6.11.2-llvmpipe-C11-Boost.txt | 1494 | `a5b95495aca4f03571f5c4b3b123d0986ae567226166bb9c3fb9a3d18c3b9997` |
| MESA-LLVM-DRAFT-INDEX.txt | 2949 | `3d1feb50bb3da85a7f1724082d32787c15225c798716204fb6920fc2b8dd6cc0` |

Archive members are entire unaltered files, only renamed to `.txt`: Mesa
`docs/license.html`, `include/c11/{threads.h,threads_win32.h}`,
`include/GL/{glext.h,glxext.h}`, `include/GLES2/gl2ext.h`; LLVM `LICENSE.TXT`,
`lib/Support/{COPYRIGHT.regex,MD5.cpp}`, `include/llvm/Support/{MD5.h,LICENSE.TXT}`.
The last Support license adds an **eXtensible Systems, Inc.** copyright;
it supplements, not replaces, the NCSA release text. All three earlier LLVM
downloads match their source-archive members exactly.

The three Qt notice files are exact byte ranges inside `badcode` blocks of
[tagged `doc/src/legal/licenses.qdoc`](https://raw.githubusercontent.com/qt/qtdoc/v6.11.2/doc/src/legal/licenses.qdoc),
with the original indentation and license words retained. The complete source
QDoc is retained separately, SHA256
`27a7061237f0c8077783316d8ec4e974004df6d44e1e728cd7e66d9e95bba91e`.
Qt puts these notices inline, rather than referring to a separate llvmpipe
license file. The Qt Khronos block uses years **2013-2016**; Mesa 11.2.2's
`glext.h` uses **2013-2015**. Both are preserved without inventing a reconciliation.

The private acquisition index
`supplemental-notice-draft-acquisition.json` binds all fourteen upstream files
to member paths/URLs/lengths/SHA256; its SHA256 is
`813d9a6bea7a8325275149952f3cba697bd7b7a12f74f2e14e911fa42fb30945`.
The data-only extraction script SHA256 is
`966b6da7e2598783fb3149f935b3843aa784909d22311514f1818e41b7869a99`.
The index intentionally says coverage, signature verification and release
inclusion are false. No new payload input manifest was created.

**Missing required terms:** none of the specifically requested Brian Paul,
Khronos, C11 Boost, LLVM release, regex or MD5 texts are missing from this
draft. This is not a statement that all terms required for the binary are
present. Other linked Mesa/LLVM objects still need reconciliation, and GLX,
GLES, ARM, test and build-tool applicability must not be inferred from mere
source availability. Original Qt patch/build correspondence and actual
delivery of these notices remain open.
