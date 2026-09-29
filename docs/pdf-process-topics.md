# PDF process isolation and Unicode card topics

Source: lead brief in `TO-CODEX.md`, 2026-09-23 11:20, corrected by 12:00/12:10.
Base `7013201`.
This is a local implementation slice, not a release or owner-document audit.

## Required behavior

- PDFium and pypdf work for desktop preview, phone PDF routes and native text /
  AcroForm extraction executes in a child process. A page operation has a
  10-second deadline, bounded input/output and Windows Job cleanup.
- Desktop PDF opening and page changes leave the GUI event loop responsive,
  visibly say `rendering…`, and never publish a closed viewer's late result.
- Valid concurrent phone PDF requests get HTTP 503 `busy`, without an internal
  retry or queue. Existing auth, visible-file checks, page/width bounds,
  source preservation and no-receipt behavior remain.
- Native form values remain visible with and without saved appearances,
  including owner-password-only encryption. XFA warnings remain honest.
- `canonical_topics` admits casefolded Unicode alphanumeric words separated
  by a single hyphen, maps `ё` to `е`, and keeps the eight-topic / 40-character
  limits. Cyrillic topics survive persistence and can be found by Ask.
- No migration of existing valid ASCII cards, archive-wide Ask, new sensor
  work, cloud/model call, phone deployment or live-app restart is included.

## Compatibility detail

The12:00 lead correction restores legacy input normalization rather than
rejecting owner/model wording: casefold, ё→е, literal U+0020 spaces and
underscores to hyphens, remove characters that are neither alphanumeric nor
hyphens, collapse repeated hyphens and strip edge hyphens. Empty entries are
skipped; nonempty entries longer than40 normalized characters are refused.
Existing required/nonempty and eight-unique-topic checks remain. Thus
`Car Insurance`/`car_loan`/`Анализ Крови`/`tax 2025!` become
`car-insurance`/`car-loan`/`анализ-крови`/`tax-2025`. Tabs/newlines inside a
list entry are removed as before; a top-level string still splits its topic
list on comma/semicolon/newline. The strict-rejection experiment in866ad36
was a compatibility regression, not the intended final contract.

## Verification seams

Tests use the same public interfaces as callers: PDF read/render functions,
StagingReader/native extraction, FileViewer with the actual Qt event loop,
loopback HTTP, canonical_topics, CardStore and Ask. Synthetic child programs
replace only the OS subprocess launch to exercise timeout, crash and malformed
output. Native form rendering and extraction also run through the real child.
No owner file, cached owner text or owner API key is a fixture.

## Implementation limits

The public PDF facade never imports PDFium or pypdf. One admission semaphore
schedules child operations; the native parser has no shared lifetime in the
GUI process. Input is an immutable snapshot through stdin, not an owner path.
The child gets isolated Python startup, an empty temporary working directory,
and a small OS environment without inherited model credentials or proxies.

Source input is limited to 512 MiB, matching the existing local extraction cap;
child stdout is limited to 64 MiB. Width stays at most 1600, height at most
10000 and raster area at most 16 million pixels. The parent validates result
shape, page identity and PNG dimensions/framing before publishing it. Deadline
and cancellation cleanup own only that request's process/job, not other agents.

PDF, OCR and the explicit agent door share owned temporary-directory cleanup.
After process/Job/pipe teardown, removal retries for up to 2 additional seconds,
50 ms apart. The exact freshly created directory is checked before deletion
(parent, identity, directory type, no reparse point); no temp-folder sweep or
deferred finalizer deletion is used. Missing directories count as removed.
Persistent failure refuses an otherwise valid result with an explicit cleanup
warning that temporary local data may remain. The door records `cleanup_failed`,
including when cancellation races with cleanup, without accepting a reply hash.
The desktop receives this fixed typed warning independently of conversation
output, including after Clear or a mode switch and for a phone-originated
request. It never restores a cancelled answer/history or classifies model text
as an operational warning merely because that text mentions cleanup.
Tests inject transient/permanent OS removal failures at the three public seams;
the original PDF output-flood assertion remains unchanged.

A 10-second request deadline includes waiting for admission and the native
operation. It is not a hard end-to-end time guarantee for stalled filesystem
I/O or OS cleanup, a job memory cap, or a hostile-code/network sandbox. The
existing Windows Job assignment-after-launch caveat remains. Native text pages
are separate children (up to 50); OCR retains its separate 20-second deadline.
There is no one deadline for a whole 50-page document. Native/form extraction
semantics and the extraction cache profile are unchanged; existing valid cache
entries are not forced through another extraction merely by this isolation move.

## Qualification

Run the synthetic offscreen check with `QT_QPA_PLATFORM=offscreen`:

```powershell
python -B -m tools.qualify_pdf_process_preview --output-directory <new-folder>
```

It constructs a filled AcroForm without `/AP`, with AES owner restrictions,
observes GUI heartbeats during the actual child, saves the displayed image,
and verifies unchanged source/zero receipts. Raw source fingerprints are checked
before and after the run. It explicitly loads installed Segoe UI for Windows
offscreen font discovery; it does not alter the production application's fonts.

The final suite and independent review results are recorded in the handoff
report. Passing synthetic examples are not a guarantee for every PDF, a review
of owner documents or a medical/recognition-accuracy claim.

The retained pre-cleanup-fix [synthetic viewer measurement](pdf-process-preview.json) finished
in 0.594 seconds, including 0.016 seconds constructing the viewer; 54 GUI
heartbeats occurred while rendering. The displayed page was 1200 x 720 pixels,
with its filled value visually checked; the source stayed unchanged and no
receipt was written. Hashes describe raw working-tree source bytes at that
measurement, including Windows line endings, not normalized Git object bytes.

The unchanged qualification script was rerun on clean `master c4729ae` after the
cleanup fixes: 0.563 seconds to render, 51 GUI heartbeats, unchanged source and
zero receipts. The fresh PNG was visually checked for the filled value. See the
[current reports, screenshot and before/after source manifest](qualification-c4729ae/README.md).
The earlier measurement above is retained, not relabeled as current. This rerun
is one success-path check, not a repeat of the cleanup-failure regression suite
or a new phone/owner-document qualification.
