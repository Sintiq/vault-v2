# Local OCR engine decision research

Researched 2026-09-23 against primary sources. Scope: Vault V2's local OCR
slice, branch `codex/vault-v2-local-ocr`, base `10eb861`. This is research,
not recognition-quality certification or a claim
about all traffic from the PC. No owner documents were opened.

## Final local choice after synthetic qualification

Root completed the same three synthetic image probes on this host. Selected
**Tesseract 5.5.2, explicit `eng+rus`, upstream tessdata_fast**, not Windows OCR.
Windows remains a researched alternative, not a runtime fallback. The earlier
conditional Windows recommendation below was superseded by these measured results.

| Synthetic input | Windows en-US | Windows ru | Tesseract eng+rus |
| --- | --- | --- | --- |
| English payment phrase | Exact phrase | `October` became `0ctober` | Exact phrase |
| Russian payment phrase | Cyrillic mangled | Exact phrase | Exact phrase |
| Both phrases on one page | Russian phrase mangled | English `October` became `0ctober` | Both exact |

English target: `Payment is due by October 15, 2026`. Russian target:
`Оплатить до 15 октября 2026 года`. Tesseract times for these clean 1800x600
images were 0.063/0.062/0.125 seconds, excluding initial engine load. This is a
small qualification, not a general accuracy benchmark or guarantee for photographs.
There is no invented cross-language text repair or merger.

Local Windows API reported installed recognizers `en-US`, `ru`, limit10000;
engine DLL version10.0.26100.9278, Python3.12.10, PyWinRT3.2.1. Tesseract runtime
reported5.5.2/Leptonica1.87.0. The publisher wheel SHA256 was checked before
installation and matches the published release digest below. During this initial
comparison only the ignored `.venv` inside the new OCR worktree was modified,
not main's environment or the OS. Subsequently Claude's 06:20 approval authorized
the pinned wheel in the main application `.venv`; its SHA256 was checked again
before that installation. No OS language installation or live app restart occurred.

Models were downloaded from upstream tessdata_fast commit
`87416418657359cb625c412a48b6e1d6d41c29bd`, then their SHA256 values recorded:

- eng: `7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2`
- rus: `e16e5e036cce1d9ec2b00063cf8b54472625b9e14d893a169e2b0dedeb4df225`

Both native probes succeeded with Python socket creation replaced by a refusal.
This checks Python-side calls, NOT native DLL traffic or whole-host isolation.
All inputs and models were local; no owner files were read. A first WinRT probe
using asyncio failed because the socket test double replaced a type used by
the event loop. A synchronous bounded native `IAsyncOperation.wait/get_results`
probe removed that dependency and passed; the failed probe processes were stopped.
PowerShell's script policy was left unchanged after it refused a script-file probe.

For broad redistribution, the third-party Windows wheel and its transitive
license/source obligations below remain a release gate. Private local
qualification is not a redistribution license certification. The final engine
choice and these limitations were sent to Claude before production implementation.

## Initial conditional recommendation (superseded above)

**Update after the root agent's synthetic probe:** the condition below did not
hold for a single mixed-language result. English mode mangled Russian; Russian
mode misread parts of the English text (`0ctober`, `SYNTHETlC`). The next bounded
candidate is therefore an isolated Tesseract trial, not an invented splice of
two Windows OCR outputs. See the follow-up section at the end. These runtime
observations were reported by the root agent, not independently repeated here.

**Conditional choice: Windows.Media.Ocr for this Windows-only slice**, provided
the synthetic runtime checks demonstrate useful English, Russian, and mixed
content and the app clearly reports unavailable languages/engine failures.
The root agent reports that a read-only Windows PowerShell 5 query on this host
returned `en-US` and `ru` and `MaxImageDimension = 10000`; neither Tesseract nor
Python WinRT packages were found in the checked locations. These are
root-reported observations, not independently repeated by this researcher.

This choice minimizes a new native-engine supply chain on this particular PC.
It is **not** a finding that Windows OCR has higher accuracy than Tesseract.
English/Russian recognition quality and the proposed mixed-language policy
must be established separately on synthetic fixtures. The package-identity
support caveat below must remain visible: successful local execution does not
prove support on every unpackaged Windows installation.

**Fallback decision, not automatic runtime fallback:** if the synthetic tests
or supported deployment requirements rule Windows OCR out, return to the lead
with Tesseract 5 plus pinned upstream `eng` and `rus` data. Do not silently swap
engines. Tesseract has a documented combined-language interface and a more
explicitly pinnable engine/model set; it also adds native binary acquisition,
dependency notices, and deployment work. [Tesseract command-line guide](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html),
[upstream Windows downloads policy](https://tesseract-ocr.github.io/tessdoc/Downloads.html).

## Windows.Media.Ocr

### Locality, language discovery, and mixed text

Microsoft describes this OCR engine as running on the device without an
Internet connection. This is an engine-processing claim; language acquisition,
Windows telemetry, the surrounding application, and the whole host are not
thereby attested offline. [Microsoft Windows Developer Blog](https://blogs.windows.com/windowsdeveloper/2016/02/08/optical-character-recognition-ocr-for-windows-10/).

`AvailableRecognizerLanguages` enumerates recognizers available on the device;
`IsLanguageSupported` tests whether a requested language resolves to one, and
`TryCreateFromLanguage` can fail to produce an engine. Use actual installed
recognizer tags, not a guessed equivalence between Windows display language and
OCR support. [OcrEngine API](https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrengine?view=winrt-26100),
[TryCreateFromLanguage](https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrengine.trycreatefromlanguage?view=winrt-26100).

Microsoft's PowerToys documentation gives the Windows PowerShell 5 query for
installed recognizers and explains the separate OCR language-pack installation.
Language OCR is a Windows Feature on Demand with a Basic-language dependency;
absence should be surfaced to the owner, not repaired by a hidden install.
[PowerToys Text Extractor](https://learn.microsoft.com/en-us/windows/powertoys/text-extractor),
[language Features on Demand](https://learn.microsoft.com/en-us/windows-hardware/manufacture/desktop/features-on-demand-language-fod?view=windows-11).

The recognition API explicitly uses one `RecognizerLanguage`. No equivalent of
Tesseract's combined `eng+rus` option is documented in this API. A two-pass
English/Russian strategy would therefore be **Vault policy**, not native
automatic bilingual detection. Preserve language-labelled results or apply a
tested deterministic deduplication policy; do not invent text or claim that
selecting the longest output is a confidence score. [RecognizeAsync](https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrengine.recognizeasync?view=winrt-26100).

`MaxImageDimension` exposes the engine's supported dimension limit. Query it;
do not assume the local value of 10000 is a permanent contract. This is not a
safe application memory budget: enforce an independent decoded-pixel limit
before handing a bitmap to a native engine. [MaxImageDimension](https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrengine.maximagedimension?view=winrt-26100).

### Python and deployment caveat

Modern PyWinRT is the community-maintained continuation of Microsoft's xlang
projection. Its history distinguishes the old monolithic `winrt`, the
historical `winsdk`, and current modular `winrt-*` packages. These bindings are
not a new OCR engine. [PyWinRT upstream](https://github.com/pywinrt/pywinrt).

The maintainer's package listing for `winrt-Windows.Media.Ocr` 3.2.1 includes
CPython 3.12, 3.13, and 3.14 Windows wheels, including x86-64. Record the exact
wheel and hash when choosing Python bindings rather than relying on an
unbounded package name or installing the old monolith. The listing exposes
SHA-256 checksums; this research has not audited an installed wheel or attested
its binary provenance. [OCR package release metadata](https://pypi.org/project/winrt-Windows.Media.Ocr/).

**Important support mismatch:** Microsoft's current desktop WinRT support page
lists `Windows.Media.Ocr.OcrEngine`, `OcrLine`, `OcrResult`, and `OcrWord` among
APIs requiring package identity. PyWinRT also warns that APIs requiring UWP or
package identity may not work from ordinary Python. That cannot be erased by a
successful PowerShell experiment. Conversely, a successful local probe should
not be falsely described as impossible. Treat the actual runtime test as
local evidence and retain a supported-packaging qualification before broad
distribution. [Microsoft desktop API restrictions](https://learn.microsoft.com/en-us/windows/apps/desktop/modernize/winrt-api-desktop-app-support#apis-that-require-package-identity),
[PyWinRT API warning](https://pywinrt.readthedocs.io/en/stable/api/index.html).

### License and redistribution boundaries

PyWinRT's source license is MIT and requires preserving its copyright and
permission notices. The OCR projection and `winrt-runtime` metadata likewise
declare MIT. This does **not** license Microsoft's OS OCR engine or language
packs as MIT. Use the installed Windows component; do not copy OS DLLs or
language packs into Vault under the binding's license. No grant to redistribute
those engine files was established in this research.
[PyWinRT LICENSE](https://raw.githubusercontent.com/pywinrt/pywinrt/main/LICENSE),
[OCR projection metadata](https://raw.githubusercontent.com/pywinrt/pywinrt/main/projection/winrt/winrt-Windows.Media.Ocr/pyproject.toml).

Do not label an entire binary dependency set MIT without inspection:
`winrt-runtime`'s build metadata contains a wheel-repair step for
`msvcp140.dll`. Microsoft separately documents redistribution conditions for
Visual C++ runtime files. Inspect the exact chosen wheels and retain applicable
third-party notices before redistributing a packaged Vault. Invoking a
preinstalled OS component avoids bundling the OCR engine itself.
[PyWinRT runtime build metadata](https://raw.githubusercontent.com/pywinrt/pywinrt/main/projection/winrt-runtime/pyproject.toml),
[Microsoft runtime redistribution guidance](https://learn.microsoft.com/en-us/cpp/windows/redistributing-visual-cpp-files?view=msvc-170).

## Tesseract

Tesseract is a native OCR engine with a command-line interface and library API.
Its installation separates the engine from language data. A local-file CLI
adapter does not require a Python-ABI-specific OCR binding; launching it from
Python 3.12+ is an integration choice, not an upstream Python compatibility
guarantee. [Installation guide](https://tesseract-ocr.github.io/tessdoc/Installation.html).

Official model repositories include both
[`eng.traineddata`](https://github.com/tesseract-ocr/tessdata_fast/blob/main/eng.traineddata)
and [`rus.traineddata`](https://github.com/tesseract-ocr/tessdata_fast/blob/main/rus.traineddata).
`tessdata_fast` trades some accuracy for speed and supports the LSTM engine,
not legacy OCR modes. Selecting it over `tessdata_best` still requires a
quality test; the name is not proof of suitability for this corpus.
[tessdata_fast upstream description](https://github.com/tesseract-ocr/tessdata_fast).

The CLI supports `-l eng+rus`. Upstream documents that language order can affect
both output and timing, so a fixed order belongs in an extraction/cache
fingerprint. `--list-langs`, with an explicit `--tessdata-dir`, permits discovery
of the actual available models. Neither installed filenames nor a non-empty
OCR result proves useful accuracy.
[combined-language usage](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html),
[upstream CLI manual](https://github.com/tesseract-ocr/tesseract/blob/main/doc/tesseract.1.asc).

The engine can process local images and local model files without a cloud
service. Do not overstate this as "the executable cannot use a network":
Windows distributor history includes URL/HTTPS image support. A Vault adapter
must pass only validated local input or stdin, fixed arguments, and an explicit
data directory; no untrusted URL/config/argument passthrough.
[upstream CLI manual](https://github.com/tesseract-ocr/tesseract/blob/main/doc/tesseract.1.asc),
[UB Mannheim distributor history](https://github.com/UB-Mannheim/tesseract/wiki).

There is no comparable stable `MaxImageDimension` capability API established
here. The current upstream `ThresholdToPix` path explicitly refuses a width or
height above `INT16_MAX` (32767), but that source-path limit is **not** a promise
that all images below it are safe or practical. Bound pixels, bytes, wall time,
and concurrent jobs independently. [upstream thresholding source](https://github.com/tesseract-ocr/tesseract/blob/main/src/ccmain/thresholder.cpp).

### Native binary provenance and licenses

Upstream explicitly says it does not provide a current official Windows
installer; it links third-party options including UB Mannheim. The latter is
the distributor's own project, not a random download mirror, but its installer
must still be described as a third-party build. Follow the upstream-to-publisher
chain, pin the exact release URL/hash, check available Authenticode evidence,
and inventory bundled DLLs. A checksum collected from the same download alone
does not independently authenticate a publisher. A reproducible/source-built
artifact is another option, with significantly more build work.
[upstream Windows download policy](https://tesseract-ocr.github.io/tessdoc/Downloads.html),
[upstream Windows build instructions](https://tesseract-ocr.github.io/tessdoc/Compiling.html),
[UB Mannheim's own installer page](https://github.com/UB-Mannheim/tesseract/wiki).

Tesseract and the official `tessdata_fast` repository each publish Apache-2.0
licenses. Preserve their notices and audit the specific distributor's
additional libraries rather than treating every installer byte as Apache-2.0.
This is a dependency inventory boundary, not legal certification of a release.
[Tesseract LICENSE](https://raw.githubusercontent.com/tesseract-ocr/tesseract/main/LICENSE),
[tessdata_fast LICENSE](https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/LICENSE).

## Qualification still required

These are proposed acceptance checks, not completed results:

- Native OCR on synthetic English, Russian, and mixed-script images, including
  numbers and short identifiers; report exact failures instead of declaring
  universal accuracy from one clean image.
- Runtime availability, missing language, unsupported host, corrupt image,
  excessive dimensions, cancellation/timeout, and one-job-at-a-time behavior.
- Text-layer PDF extraction and form values remain separate from OCR. An
  existing form value must not be replaced by an OCR guess; scans use actual
  rendered pixels. Preserve source provenance and warnings.
- The content hash names `.text/<sha>`, but cache metadata must also identify
  extraction version, engine route, languages/policy, and relevant model/runtime
  version. Otherwise changing OCR configuration can silently reuse stale text.
- Cache text has the same privacy sensitivity as the source document. No
  extracted personal text, source images, or owner PDFs belong in online
  reviews, tests, diagnostics, or repository fixtures.
- Error results stay honest and retryable; no cloud fallback, silent language
  download, or automatic whole-vault scan is implied by choosing either engine.

## Follow-up: isolated Windows tesserocr trial

Researched 2026-09-23 after the Windows mixed-language probe. **Yes: the
upstream-recommended Windows wheel route can support a bounded trial without
installing Tesseract into the OS**, subject to validating the actual wheel and
its native load on this PC. The publisher describes its wheels as containing
their native libraries; models remain a separate download. Use a disposable
Python 3.12 x64 environment and an explicit model path, not a machine-wide
PATH or `TESSDATA_PREFIX` change. This is a trial recommendation, not a completed
quality test. [Windows wheel publisher README](https://github.com/simonflueckiger/tesserocr-windows_build).

### Exact candidate and availability

The current upstream `tesserocr` PyPI release is **2.11.0**, published
2026-08-04. Its listed CPython 3.12 wheels target Linux/macOS, not Windows.
The wrapper's own Windows installation instructions instead link Simon
Flueckiger's separate Windows-build repository. Do not expect
`pip install tesserocr==2.11.0` to supply this Windows binary.
[tesserocr PyPI release and installation instructions](https://pypi.org/project/tesserocr/).

The inspected Windows publisher release is:

| Item | Value |
| --- | --- |
| Build tag | `tesserocr-v2.10.0-tesseract-5.5.2` |
| Wrapper / engine / Leptonica | `2.10.0` / `5.5.2` / `1.87.0` |
| Wheel | `tesserocr-2.10.0-cp312-cp312-win_amd64.whl` |
| Byte length | `4213119` |
| Published SHA-256 | `e05d41a2b0e6f38f3a5195d05a73674d72152a775d1b8ebe481ca9306f94d27a` |
| Asset updated | `2026-03-15T00:34:27Z` |

Sources: [publisher release](https://github.com/simonflueckiger/tesserocr-windows_build/releases/tag/tesserocr-v2.10.0-tesseract-5.5.2),
[publisher release metadata](https://api.github.com/repos/simonflueckiger/tesserocr-windows_build/releases/tags/tesserocr-v2.10.0-tesseract-5.5.2),
[exact wheel asset](https://github.com/simonflueckiger/tesserocr-windows_build/releases/download/tesserocr-v2.10.0-tesseract-5.5.2/tesserocr-2.10.0-cp312-cp312-win_amd64.whl).
The metadata above was read directly from GitHub's release API; this researcher
has not downloaded or hashed the artifact. The root agent must compare actual
download bytes to this digest before loading it.

Newer wrapper release entries in the inspected Windows listing included
2.11.0 builds with Tesseract 4.1.3 or 3.05.02. Wrapper version alone therefore
does not select the desired engine. [publisher release list](https://github.com/simonflueckiger/tesserocr-windows_build/releases).

### Build transparency and remaining trust

The selected tag's AppVeyor script builds Tesseract from upstream commit
`6e1d56a` and Leptonica from `13275a2`, using Visual Studio 2022. It collects
DLL dependencies, patches the wrapper, and vendors `cysignals`. It also
downloads a moving Software Network client and uses non-pinned build
dependencies; this is inspectable source, **not a reproducible-build
attestation**. The script's existence and a signed Git commit do not prove
that a release wheel contains precisely those builds.
[tag-pinned build script](https://raw.githubusercontent.com/simonflueckiger/tesserocr-windows_build/tesserocr-v2.10.0-tesseract-5.5.2/appveyor.yml),
[DLL collection helper](https://raw.githubusercontent.com/simonflueckiger/tesserocr-windows_build/tesserocr-v2.10.0-tesseract-5.5.2/res/find_libraries_and_dependencies.py).

This is a **third-party Windows binary distribution recommended by the wrapper
upstream**, not an official Tesseract Windows installer. Before recognition,
inspect wheel paths/metadata/native libraries, verify the published digest,
then record `tesserocr.tesseract_version()` and `get_languages(explicit_path)`.
Use upstream `eng`/`rus` models pinned to a commit and hash, and test a fixed
`lang="eng+rus"` policy against the same synthetic fixtures.
[wrapper API examples](https://github.com/sirfz/tesserocr),
[upstream model repository](https://github.com/tesseract-ocr/tessdata_fast).

### Do not flatten the licenses

- The wrapper and Windows build scripts are MIT.
  [wrapper license](https://raw.githubusercontent.com/sirfz/tesserocr/master/LICENSE),
  [Windows-build license](https://raw.githubusercontent.com/simonflueckiger/tesserocr-windows_build/tesserocr-v2.10.0-tesseract-5.5.2/LICENSE).
- Tesseract and the selected official models use Apache-2.0, as documented
  above. Leptonica 1.87.0 publishes a two-clause BSD-style license requiring
  its notice/disclaimer in binary distributions.
  [Leptonica 1.87.0 license](https://raw.githubusercontent.com/DanBloomberg/leptonica/1.87.0/leptonica-license.txt).
- **`cysignals` 1.12.6 is bundled for this CPython 3.12 build and carries
  LGPLv3, not MIT.** The setup patch copies it into `tesserocr/cysignals` and
  removes the external runtime requirement; another patch changes the import
  to this bundled copy. This means an ordinary dependency list can conceal
  a material license obligation.
  [bundling patch](https://raw.githubusercontent.com/simonflueckiger/tesserocr-windows_build/tesserocr-v2.10.0-tesseract-5.5.2/res/patches/tesserocr/setup.py.patch),
  [relative-import patch](https://raw.githubusercontent.com/simonflueckiger/tesserocr-windows_build/tesserocr-v2.10.0-tesseract-5.5.2/res/patches/tesserocr/tesserocr.pyx.patch),
  [cysignals 1.12.6 license](https://raw.githubusercontent.com/sagemath/cysignals/1.12.6/LICENSE).

The LGPL text includes conditions for conveying combined works, notices, and
user modification/relinking; redistribution needs a specific packaging review,
not an assumption that the wrapper's MIT label covers the bundle. Other image
codec and CRT notices also require inventory of the actual artifact. This
research is not a legal clearance for a distributable release. A private,
isolated synthetic trial does not itself require distributing the wheel.
[cysignals license, combined works](https://raw.githubusercontent.com/sagemath/cysignals/1.12.6/LICENSE).

Keep the trial honest: Python-level socket blocking can test that Python code
does not create sockets, but cannot by itself attest all native DLL traffic.
Provide only in-memory synthetic pixels and a local model directory; no URL,
arbitrary configuration, private source, or network-fallback path. A successful
mixed-page test would qualify only that fixture and configuration, not general
document correctness or safe clinical/financial interpretation.
