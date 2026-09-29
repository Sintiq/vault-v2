# Local document text and OCR

Scope: private Windows desktop use of Vault V2. Local extraction serves Staging
Sort, Find tasks and Health's Read documents, plus background cache preparation
in explicitly marked folders for [archive Ask](archive-ask.md). Folder grants,
writable-window startup and Ask cache misses share one deduplicated serial
preparation queue. Ask uses only ready, validated caches and never waits for
extraction/OCR; missing text is prepared in the background for a later request.
Staging cache misses do not enter that automatic archive queue. There is no file
watcher or automatic tight retry loop; a later Ask may retry a failed job.
This is not a cloud OCR service, medical interpretation or a general
recognition-accuracy guarantee. Phone Sort/Tasks/Health use the same Staging
reader; no independent phone OCR engine or new OCR API is introduced.

## Staging document-job route and evidence

1. Enumerate visible regular files in Staging without extraction. Hidden/service
   paths, parent traversal and reparse paths are refused. PDFs/photos are only
   candidates at this point; their owner preview kind remains unchanged.
2. Read a source-byte snapshot. For PDF pages, extract the PDFium text layer and
   add stored AcroForm `/V` values as `field: value`. Do not infer values from
   field names or repair model/OCR wording. XFA receives an explicit incomplete
   extraction warning. Password-required or structurally unreadable forms fail.
3. If the native text layer alone has fewer than 20 alphanumeric
   characters, render that page with the shared form-aware PDFium renderer and
   recognize its pixels locally. Photos use the same fixed OCR child route.
   Language order is always `eng+rus`, LSTM-only, automatic page segmentation.
   Field names/values do not suppress OCR of a scanned background. This is a
   heuristic: an image embedded beside 20 or more native characters is not
   separately recognized; existing but incorrect text layers are not repaired.
4. Process at most the first 50 PDF pages and preserve a visible warning plus
   truthful total-page metadata. HEIC/HEIF require a local decoder; absence is a
   clear refusal, not a network install. OCR is imperfect even when nonempty.
   Multi-frame/animated image files are explicitly refused, not silently reduced
   to their first frame; use separate images or a PDF for multiple pages.
5. Save derived text/cache metadata, then hand at most 6000 characters to the
   model. The final lexical source validation, fresh source hash check and
   `agent_read` receipt share one short root write guard. Sort/Tasks/Health carry
   that exact snapshot digest; they do not attach a later cached card digest to
   earlier text. Quotes and dates retain their existing deterministic checks.

Sort collection runs inside its background worker, not before the dialog on the
GUI thread. Tasks/Health already collect in workers. `reading scans…` is visible;
failed/empty extraction is named in proposal notes, with remaining notes
available in the desktop tooltip. Sort can explicitly fall back to a name-only
BINARY proposal; it does not call that document successfully read. Canceling a
Sort result discards later drafts but does not erase cache/input-read evidence
already produced. The window retains runtime ownership until workers finish.

Extraction and OCR waiting happen outside the root guard. Native PDF work runs
in bounded children with a shared admission gate; the OCR child is not awaited
while holding that gate. Cache
publication and the final read receipt acquire short guards independently.

## Isolation and limits

Recognition uses the same Python interpreter in a one-page child process with
isolated Python startup, a fresh temporary working directory, stdin image bytes,
fixed arguments/models and bounded JSON stdout. No document filename, arbitrary
Tesseract config, URL or user-selected language argument is passed to the engine.
The parent enforces a 20-second page deadline and a 1 MiB output bound. Child
failure does not qualify the document as successfully extracted.

Input is bounded to 32 MiB per image, at most 10000 pixels per dimension and
16 million decoded pixels. Source extraction is capped at 512 MiB. A page is
bounded to 100000 extracted characters; a derived-text cache is at most 32 MiB.
These are resource bounds, not a promise that every smaller input is safe or
quick. A 50-page document can take many page deadlines; cancellation discards
pending results rather than promising instantaneous native interruption.
PDF parsing (pypdf/PDFium), native page text, stored form values and raster
generation now also run in a separate one-shot child, with a 10-second deadline
per operation, a 512 MiB source bound and a 64 MiB child-output bound. Native
text/form pages are requested individually, up to the existing 50-page limit;
OCR still has its independent 20-second image deadline. Any refused page prevents
successful partial cache publication. This does not impose one deadline on the
whole document or prevent every form of child memory/resource exhaustion.
Blocked filesystem reads and OS cleanup may add wall time beyond a page deadline.

The Windows child uses a Job Object for cleanup, assigned after process creation
and before sending image bytes. This constrains cooperative worker cleanup;
it is not a hostile-code sandbox. Python socket construction is refused in the
OCR worker and tested separately. That does not prove native DLL/OS traffic is
impossible. Vault passes only local bytes and has no OCR network fallback.
Other existing application network routes are not changed by this feature.

## Cache, privacy and receipts

`<root>/.text/<source-sha256>.txt` contains the full derived text.
Its adjacent `.json` records engine/version, languages, processed and total page
counts, extraction profile, warnings, time, source identity and text checksum.
A valid hit reuses the result for unchanged source bytes, including a rename;
invalid/incomplete pairs or a changed extraction profile are not trusted hits.
Errors do not publish a successful partial extraction result.

The recognizing OCR child reports its exact admitted native-engine version
(5.5.2 or 5.5.3), alongside the text. Missing, unknown or extra response fields
are refused; no assumed version is substituted. A PDF whose OCR children
report different versions is refused before cache publication. The v2 profile
invalidates older document caches that hard-coded 5.5.2; they are regenerated
only through normal authorized extraction, never silently relabelled. Valid v2
caches retain their historical extraction version, not a claim about the engine
currently installed. Plain UTF-8 caches keep their separate unchanged profile.

`.text` is a service folder, absent from owner panes, but included in backup.
It is not encrypted separately from the Vault filesystem. Treat its text exactly
like the private source: never add owner caches or documents to Git, browser
reviews, telemetry or public bug reports. Tests and qualification use synthetic
documents only. Cache writes create `text_extracted` receipts with source hash,
page count and engine, never recognized text. `agent_read` records the source
hash and number of excerpt characters handed over.

Empty trash removes matching derived text only after proving that no source
copy remains. Damaged/orphan trash, unreadable or linked paths make that proof
uncertain: the cache is kept, `text_cache_cleanup_deferred` is recorded and the
desktop shows a manual-review note. This is not automatic repair. Backups made
earlier continue to contain their historical cache/source copies.

## Manual pinned setup (never run automatically)

Close the application before modifying its dependencies. From the project,
explicitly run with the application's **CPython 3.12 Windows AMD64 venv**:

```powershell
.venv\Scripts\python.exe tools\install_local_ocr.py --install
```

Omitting `--install` prints help only. Unsupported Python/platform or a global
interpreter is refused; there is no automatic alternative wheel. The tool
downloads only the fixed artifacts below into a temporary directory and checks
every SHA256 and byte length before pip or model publication. It runs pip with
isolated settings, `--no-index --no-deps` and the verified local wheel, using the
same interpreter; normal project dependencies such as Pillow must already exist.
No actual package install is performed by the refusal-path tests.

Each model is published by verified atomic file replacement. The whole setup is
not an all-or-nothing operation: a pip failure or later disk failure is reported,
not silently retried or rolled back. A prior package/model installation may
remain. Check the error and rerun only by explicit choice. Runtime checks both
model hashes on OCR child startup; missing, linked or mismatched models disable
OCR with a visible reason. It never downloads replacement data. The wheel hash
checks distribution bytes at setup; this is not continuous installed-DLL
integrity attestation against arbitrary same-user modification.

| Artifact | Pinned SHA256 |
| --- | --- |
| `tesserocr-2.10.0-cp312-cp312-win_amd64.whl` | `e05d41a2b0e6f38f3a5195d05a73674d72152a775d1b8ebe481ca9306f94d27a` |
| `eng.traineddata` | `7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2` |
| `rus.traineddata` | `e16e5e036cce1d9ec2b00063cf8b54472625b9e14d893a169e2b0dedeb4df225` |

Wheel source: [Simon Flueckiger's exact Windows release](https://github.com/simonflueckiger/tesserocr-windows_build/releases/tag/tesserocr-v2.10.0-tesseract-5.5.2).
Models: [upstream tessdata_fast commit 87416418](https://github.com/tesseract-ocr/tessdata_fast/tree/87416418657359cb625c412a48b6e1d6d41c29bd).
The installer fixes these full URLs and hashes in source; they are not remote
configuration. A digest validates equality to the chosen artifact, not that its
native implementation has been independently audited or reproducibly rebuilt.

## Dependency provenance and private-use restriction

This private evaluation is **not redistribution-ready**. The manual CPython 3.12 wheel is
a third-party Windows binary build, not an official Tesseract distribution.
Its wrapper is 2.10.0, Tesseract 5.5.2, Leptonica 1.87.0. Tesseract and official
models use Apache-2.0; wrapper and Windows build scripts MIT; Leptonica uses a
two-clause BSD-style license. Bundled `cysignals` 1.12.6 carries LGPLv3, not MIT.
Codec and CRT dependencies need their own retained notices and packaging review;
the wrapper's top-level license does not cover the entire native bundle.

The exact [upstream model license](https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/87416418657359cb625c412a48b6e1d6d41c29bd/LICENSE)
is copied unmodified in substance to [ocr_models/LICENSE](../vault_v2/ocr_models/LICENSE).
See [research, primary-source links and supply-chain limitations](ocr-engine-research.md).
No redistribution clearance, native security audit or general OCR accuracy
certification is claimed by a successful synthetic test or local installation.

The separate embedded Windows qualification runtime uses its own pinned
dependency manifest and Tesseract 5.5.3. The manual-setup dependency versions
above do not describe that entire payload; see the
[actual-payload audit](redistribution-payload-audit-2026-09-26.md).

## Current synthetic requalification

The unchanged local qualification script was rerun on clean `master c4729ae`
after the PDF-process/cleanup changes: all five synthetic extraction-to-Tasks
cases passed, with 3.641 seconds for extraction and 2.547-4.438 seconds per Tasks
call after warm-up. See the [fresh reports and source manifest](qualification-c4729ae/README.md).
The older [OCR report](ocr-qualification.json) remains historical. This rerun
does not repeat the full suite or establish Sort/Health, general OCR accuracy,
cleanup-failure handling or native/OS network isolation.
