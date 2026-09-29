# OCR source inputs acquired and publisher patches checked

Date: 2026-09-27. Data-only source preparation, not source-build reproducibility
or legal clearance. Qualification12 and product code remain unchanged.

## Acquired archives

| Input | Bytes | Local SHA256 |
| --- | ---: | --- |
| cysignals 1.12.6 sdist | 79,583 | `3ef3a37bdb244821b85475a08e2762ca1019570b369e321504995fa9a54675ce` |
| Windows wheel publisher, commit `cb737e34870e2568da274ac89e1b11db2c52aad8` | 19,844 | `0ade91bb7a8155fa14cc79f64afb30c2b5466e528c4b717b3f116af5bf639c71` |
| tesserocr submodule, commit `d27f2e394c79739c75b062fc150b08f420300ab6` | 75,351 | `8742b65af283174f5b86cd95176e35e817fa0ef79ee789245350a844dd168924` |

The cysignals download URL, size and SHA256 matched the maintainer's current
[PyPI release metadata](https://pypi.org/pypi/cysignals/1.12.6/json) before and
after download. Its archive is from
[files.pythonhosted.org](https://files.pythonhosted.org/packages/b9/5a/d258fd8d6ee1538b8472f39051a87d3d6aa2ab26ffa2da4ac809fb851b88/cysignals-1.12.6.tar.gz).

GitHub's [publisher tag reference](https://api.github.com/repos/simonflueckiger/tesserocr-windows_build/git/ref/tags/tesserocr-v2.11.0-tesseract-5.5.3)
resolved to the publisher commit above. Its
[Git tree](https://api.github.com/repos/simonflueckiger/tesserocr-windows_build/git/trees/cb737e34870e2568da274ac89e1b11db2c52aad8?recursive=1)
records `tesserocr` as mode 160000 at the named submodule commit. The recipe
uses `git submodule update --init --recursive`, not a remote-head update in
that step. Both snapshots were downloaded from GitHub's commit-addressed
codeload service. Their SHA256 values are acquired-byte fingerprints, **not
separately publisher-signed archive digests**.

## Concrete correspondence checks

The complete archives remain intact outside the repository. A bounded tar
reader rejected duplicate/unsafe selected paths and nonregular source inputs;
no archived Python/Cython/build script was imported, compiled or executed.

The two plain-Python files shipped inside qualification12's nested
`vendor/tesserocr/cysignals/` match the sdist byte-for-byte:

| File | Bytes | SHA256 |
| --- | ---: | --- |
| `__init__.py` | 91 | `18e44bfb94f4d5b547c6487d7c033bf36e1ee70a2b71fd0660a2a1c99d89691b` |
| `cysignals-CSI-helper.py` | 4,966 | `81d50f469bbc25117e3dafac9b1c32d8cc0798e32ad05e5afd5bc541bd5e5d90` |

The acquired cysignals LICENSE is also identical to the supplemental text
already delivered in qualification12: SHA256
`da7eabb7bafdf7d3ae5e9f223aa5bdc1eece45ac569dc21b3b037520b4464768`.
This narrows the plain-source/notice correspondence; it does **not** prove the
nested `signals.cp314-win_amd64.pyd` was compiled from this exact sdist.

## Patch applicability evidence

Verified Git blob IDs against both primary Git trees, using Git's
`blob <length>\0<bytes>` SHA1 representation, for the recipe, three patch
files and the three target source files. SHA1 here binds the Git object IDs
reported by the server; it is not a modern detached-signature claim.

| Patch | SHA256 |
| --- | --- |
| `setup.py.patch` | `734ae9b9290f70566f0503858db6e987eb01afb648b786d72f40f2a8743b187a` |
| `tesserocr.pyx.patch` | `963832a130ac4ab5ef6f589cbd23c00f57ebff200004fab8672f5e42d0309a42` |
| `test_api.py.patch` | `c0c502b5ac1e33d25ca9218c025ffd18c332aaf0d648ebc6eea9e1756b249c33` |

Only those six named regular text files were extracted into a fresh private
scratch directory. **`git apply --check` accepted all three patches together,
exit 0, no diagnostic; all six files remained byte-identical.** The patches
were not applied and no upstream source code was run. This establishes that
the recorded publisher patches apply to its frozen submodule, not that a
build reproduces the shipped wheel.

The actual recipe SHA256 is
`8334110ae010d2c4b2e0875be1882833cd191ded9749c237570cc9ffcb2a04cd`.
In these acquired bytes, patch selection is at lines 306–319 and unpinned
`pip install --no-binary cysignals cysignals` at line 428. Earlier source-map
line references must not substitute for inspecting this exact recipe.

## Remaining source-kit gates

- Retain archive snapshots, frozen submodule and publisher patches with the
  eventual release kit; they are not present merely because a URL is listed.
- Bind cysignals native build inputs/toolchain and Software Network's codec
  resolution. Two matching `.py` files are not evidence for every native byte.
- Acquire/reconcile the remaining exact native sources and build inputs named
  in the [OCR map](ocr-native-source-map-2026-09-27.md).
- Verify the actual library replacement route and final source/notice delivery.

No app license was selected, no publisher contacted, no personal data sent and
no archive executed. Public redistribution remains unqualified.
