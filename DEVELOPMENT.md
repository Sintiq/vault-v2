# Developer reference

Looking to install and use Vault? Start with the [README](README.md) or the
[step-by-step guide](START-HERE.md). You do not need these source-build commands
for the Windows installer.

The technical reference below is retained from the original source snapshot.
Its dated qualification notes, including the installer status in the Tests
section, describe development before the public 0.1.0 release. For the published
downloads and current user instructions, use the links above and the
[release page](https://github.com/Sintiq/vault-v2/releases/tag/v0.1.0).

---

# Vault V2

## License

Vault V2's original code and documentation are licensed under the
[MIT License](LICENSE). This permits modification and redistribution, including
commercial use, subject to retaining its copyright and license notice.
The project attribution is "Vault V2 contributors"; no personal contact details
are required in this notice.

Third-party code, libraries, runtimes, OCR models and other separately licensed
materials retain their own licenses and notices. The root MIT license does not
relicense them or replace their source/replacement/distribution requirements.
Private vault contents, credentials and private qualification artifacts are
not part of the public project. License selection is not release qualification;
see [release readiness](docs/release-readiness-2026-09-26.md).

## Overview

Local-first document vault: three Commander-style panes and a gatekept
agent. Local Ollama is the default. The owner can explicitly open a separate
Claude Code cloud door for chat only; it never opens automatically.
Single owner — no pane is shared with other people.

This is not a claim that nothing can leave the PC. The owner can export files
and access Vault from a paired phone over Tailscale; phone chat shares the
conversation with the desktop. Daily reminders also contact the configured
phone door and send its access key, task count, titles, due dates and relative due
status. Those phone/export paths are separate
from model routing. The application does not attest the loopback server's
internals or guarantee that the whole PC is offline.

What the owner holds in hand at the end of step 1: a desktop window with
three panes — **Staging / Documents / Personal** — where files are dragged
with the mouse, successful operations normally leave a hash-chained receipt, deletes go
to a restorable trash, and an agent chat sees only the Staging pane.

## Run

For the source setup/test path below on Windows, install CPython 3.12 x64, then run from
the project directory (PowerShell):

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

The `dev` extra includes the pinned MCP SDK used by the phone-door adapter's
tests. For application-only development use `-e .`; for the optional adapter
without pytest use `-e ".[phone-door]"` and launch it from this source checkout
with `.\.venv\Scripts\python.exe -m tools.phone_mcp.server`. Installing its dependency does not
configure, connect or enable the phone door. Do not use personal Vault data for
tests. Native OCR and Ollama setup are separate, as described below; installing
Python dependencies does not install either engine or download a model.

The complete Windows source test suite also requires the pinned local OCR
engine and eng+rus models. OCR is optional for opening the application, but
required by these integration tests. In the CPython **3.12 x64** environment
created above, explicitly set up OCR before running the full suite:

```powershell
.\.venv\Scripts\python.exe tools\install_local_ocr.py --install
.\.venv\Scripts\python.exe -m pytest
```

The setup command downloads and SHA256-verifies the pinned wheel and models;
it is never run automatically by Vault. See [OCR setup](#local-pdf-text-and-ocr)
for provenance and limitations. The application's `Python >=3.12` requirement
does not mean this CP312-only setup tool supports later interpreters. This setup
does not install Ollama.

These are source-development instructions, not a qualified Windows installer
or a complete offline/reproducible dependency lock. The Python package includes
only application code and explicit static assets, never the root data directory,
Android builds or local diagnostic reports.

```
run.bat                 # default vault root: ./data
run.bat D:\my-vault     # any folder
```

Keys: F5 copy → next pane, F6 move, F7 new folder, F8 trash, Tab next
pane, Backspace up, Enter open, Ctrl+O upload into Staging, Ctrl+D theme.

Drag between panes asks Move / Copy / Cancel; Shift+drag
moves without asking; drag from Windows Explorer copies in.

## Encrypted backup and recovery

Backup writes vault files, including hidden/service data and access keys, as an
encrypted `VAULTBK1` archive, except for `.exports/`, `.api/incoming/` and
`.vault.lock` at those paths relative to the vault root. It reads the archive
back before reporting success. Creation is limited to **512 MiB of uncompressed
included source-file bytes**; this is not an archive-size or peak-RAM limit and
does not impose a restore size cap on older backups.

Names use `vault-YYYYMMDD-HHMMSS[-N].vault`; exclusive creation prevents
same-second or concurrent backups from replacing existing `.vault`/`.part`
files. An interrupted write can leave an unverified `.vault`, not a successful
backup receipt. Do not equate a filename with a verified backup.

The Backup window creates/checks archives. `restore_backup.py`, exported beside
them with `RESTORE.md`, restores to an empty/absent folder without the app
(Python 3.12+ and cryptography). App restore and the standalone script share the
same source implementation: before creating the destination they verify exact
manifest/ZIP membership, duplicate ZIP names and JSON keys, Windows-compatible
relative paths and aliases, every CRC and SHA256, and a destination without
links/junctions. Explicit ZIP directory/link/special entries are refused;
hidden/service files are retained. Root `MANIFEST.json` is reserved metadata,
not silently omitted from backup. Encryption and the existing manifest format
are unchanged. No new restore size cap is imposed on older backups.

Preflight prevents partial restore from archive defects. It is not rollback
after disk errors, a filesystem sandbox, or protection against another process
changing the destination concurrently. Use a folder you control. The app's
source distribution includes `backup_restore.py` to generate the script; the
exported script then needs neither that source file nor the app.

## Agent

The default model route is **local Ollama**, at `http://127.0.0.1:11434`. Start Ollama
and install the exact model selected by `ollama_model` (default `llama3.1:8b`).
Another installed model is not silently substituted. If the selected local
model is unavailable, the local agent is unavailable; there is no automatic external or
file-bridge fallback. A legacy `agent_backend` value such as `anthropic` or
`bridge`, or any unknown value, is explicitly refused. Cloud credentials in
the environment do not select another backend.

The status distinguishes an unreachable Ollama from a responding server without
the selected model. A missing model is named with its `ollama pull <model>` command;
the application does not run that command itself. Both availability states are
checked again every 20 seconds until the selected model is available. Unsupported
configuration stops automatic retries. Malformed server responses retain a
generic, sanitized unavailable status rather than guessing that a model is missing.

Model discovery, warm-up and chat use an allowlisted local HTTP route, without
environment proxies, a global urllib opener or redirects. This constrains the
client destination; it is not proof that a separately configured local server
cannot relay data. The explicit chat door below is separate from this route.

### Explicit chat door (Claude)

Above the chat, choose **Door: Claude** to permit Anthropic cloud processing of
your new conversation and the current Staging listing (names and sizes, not
document bodies). A visible cloud disclosure and divider mark the change.
The listing skips hidden/service entries and does not descend into symlinks,
junctions or other reparse points. An unsafe Staging root refuses the request.
Previously local messages are not copied into the new branch. Phone chat shares
this selection/history, but cannot open the door. Local is restored on every app
launch; the selection is not saved. Switching back, clearing a pending turn or
closing the window revokes pending results. Data already sent to the provider
cannot be recalled by cancellation.

Only chat uses this door. Sort, Ask, Tasks and Health keep the local backend;
when it is unavailable with the door open, they report that the door covers
chat only. Old proposal confirmations still follow their existing gates.

Install the native Claude Code CLI and sign in yourself before use. The reviewed
launch profile targets **2.1.280** with no tools/MCP/customizations, no saved
session, a fresh temporary working directory and stdin-only input. It uses
`claude-opus-5-5`; optional `agent_door_claude_model` may be exactly that value or
`claude-fable-5-1`. Aliases, other models and automatic model fallback are refused.
An unsupported flag, missing login or unavailable model fails without retrying
with fewer restrictions. No API key is required or collected by Vault.

One request at a time; deadline 180 seconds. Malformed/empty/oversized output and
nonzero exits are refused without exposing raw stdout/stderr. Output is rendered
as plain text, not HTML or actions. Open/request/result/close receipts identify the
launched agent and hashes, not message bodies. Failure to record the request
prevents launch; failure to record an accepted result prevents its publication.
Model execution/waiting never holds the Vault writer guard.
After child teardown, removal of its own temporary directory retries for up to
2 seconds. If removal still fails, no answer is accepted: a visible cleanup
error and `cleanup_failed` result receipt warn that local temporary data may
remain. This warning takes priority over a simultaneous cancellation.

**Door: Codex** is visible but disabled: `not available yet — no tools-free mode
verified`. An empty working directory plus `read-only` is not a tool-free
configuration. This route performs no Codex inference.

Limits: one-shot process cleanup is not an OS sandbox. Windows Job Object
assignment happens after process creation and before input; a hostile executable
could fork before assignment. The profile is for the installed cooperative CLI,
not protection against arbitrary same-user code. CLI auth/caches or the provider
may retain their own state; Vault does not promise zero retention or provider
deletion. Existing Claude sessions, keys and old `.chat` files are not touched.

What the local model receives depends on the action:

- Chat: relative Staging file/folder paths and file sizes, your messages and chat history.
- Sort: Staging names, sizes, file kinds and local document-text excerpts (including supported PDF/scans), up to 6000 characters per file.
- Ask: names, pane-relative paths and confirmed saved cards from all three panes,
  plus your request. In Staging and owner-marked folders, it may also receive up
  to 6000 characters per file from a ready, validated local text cache.
- Tasks and Health extraction: Staging names and text excerpts, up to 6000 characters per file.

Calls also include fixed system instructions and, where used, temporary document IDs.

Documents and Personal are included by Ask under the rules above, not by Chat,
Sort, Tasks or Health. Copy a document to Staging when you want it available to
those Staging-only document jobs or export. User messages can themselves contain
private content. Former `.chat/inbox.jsonl`
and `.chat/outbox.jsonl` history is not read, deleted or migrated by model
selection; removing the old bridge code does not delete the owner's history.

## Cards, Sort, Ask (step 3)

Desktop Sort remembers the card-store generation when it publishes its drafts.
Confirm checks that generation under the same writer/store guard as the update.
Another desktop or phone edit makes the old dialog refuse with
`proposals changed — refresh the list`, without confirmation writes or receipts.
A row can be confirmed only once; confirming selected rows and then the remaining
rows is allowed. Model calls and document extraction stay outside that guard.

### One document-read boundary

StagingReader, Ask's card collection, Sort, owner phone reads, previews and the
OCR source cache use the same lexical visible-file check before content reads or
hash/cache lookup. Dot-prefixed components and names ending in `.trash.json`
(case-insensitive) are excluded at every depth. Links, junctions and reparse
points are rejected, including ancestors of the vault root. Windows trailing-dot
and trailing-space aliases are refused. A recursive listing does not descend
into an excluded folder. Ask and owner viewing can use all three panes; Sort,
Tasks and Health remain Staging-only. Ask may hash visible source bytes anywhere
to match a saved card, but outside its text permissions it does not decode the
source, read cached text or extract text. This is not protection against a hostile
local process replacing filesystem entries after validation. Ask rechecks its permission
snapshot and collected source hashes before model dispatch and before returning
the result; a changed or no-longer-visible source requires a new request.

Export follows this read boundary and reports `N hidden items not exported`
in its manifest, bound to the approved preview. A skipped hidden directory counts
as one boundary item; its descendants are not inspected. Links are not counted
as hidden items. Internal service storage, backup and verification of explicitly
exported bytes have their own rules; the low-level hash function is not a global
document permission gate.

Owner copy/move/trash/clear/restore operations instead preserve the complete
selected tree, including hidden files. A link or junction anywhere in that tree
refuses the operation before the first write or receipt. Clear Staging preflights
the entire tree before moving its first item. Thus moving a folder keeps it whole
without following a link outside the vault.

### Available actions

- **Sort…** — the agent proposes a card for every Staging document
  (shelf, topics, issuer, recipients; year is derived by the runtime).
  A rules-only baseline is always computed too; rows say where they
  disagree. Proposed drafts may already be saved; edit any cell, then
  **Confirm** to mark the card confirmed, with a receipt. Cards live in `.cards/cards.json`, keyed by the file's
  SHA-256, so a card follows the bytes across panes and renames.
- **Ask…** — type what you need; the local agent proposes documents from all
  three panes and a recipient. Names and confirmed cards are searchable
  everywhere. Both the baseline and local model may use up to 6000 characters
  of ready, valid cached text in Staging and owner-marked folders only.
  On the desktop, right-click a folder and choose **Allow agent to read text
  here** to grant recursive text access and start background cache preparation;
  **Stop reading text here** revokes it without deleting the cache. Ask uses
  ready text without waiting for extraction/OCR. A missing/stale cache in an
  explicitly marked folder queues background preparation; that request can
  still search the file's name and card.
  Keyword matching uses Unicode `casefold` and `ё` → `е`; it does not perform
  morphology (`мигрени` does not match `мигрень`). Results show pane and relative
  path. Tick the final set, **Copy to Staging** for archive files, then
  **Export selected…** through the unchanged Staging-only Gatekeeper.
  Writable-window startup also queues preparation of marked folders. Grant,
  startup and Ask work share one deduplicated serial queue per vault root. There is no file
  watcher: a later Ask discovers new/changed files and can retry failed jobs,
  without an automatic tight retry loop. Staging's implicit permission uses
  ready caches only; an Ask cache miss there does not queue preparation.
  See [archive Ask permissions, cache and API](docs/archive-ask.md).
- Standard shelves are `INBOX HEALTH TAXES FINANCE
  INSURANCE IDENTITY PHOTOS`, recipients `DOCTOR ACCOUNTANT INSURANCE
  PERSONAL`, topics as lowercase hyphenated slugs (≤ 8, ≤ 40 chars), issuer
  `UNCONFIRMED` by default. Agent issuers must occur literally (case-insensitive)
  in the document excerpt; derived years come only from that text, never a filename.
  Topics support Unicode letters and numbers separated by single hyphens:
  `мигрень`, `анализ-крови`, `mrt-мозг`. Canonicalization uses `casefold` and
  `ё` → `е`; the 40-character limit applies after full normalization. As before,
  literal spaces and underscores become hyphens; other non-alphanumeric
  characters are removed, repeated hyphens collapse, and edge hyphens are
  trimmed. Empty normalized entries are skipped. For example `Car Insurance`,
  `car_loan`, `Анализ Крови` and `tax 2025!` become `car-insurance`, `car-loan`,
  `анализ-крови` and `tax-2025`. Outer whitespace and comma/semicolon/newline-separated
  topic-list entry remain supported. Duplicate normalized topics count once
  toward the eight-topic limit. Existing valid ASCII cards need no migration;
  reading them does not rewrite their stored bytes or add receipts. Ask can
  match confirmed Cyrillic topics even without permission to use that file's text.

### Reading coverage and whole-request refusal

Sort, Tasks, Health and Ask display local reading limitations, also returned in
top-level phone API `notes`. For example: `read 6 000 of 48 210 characters — the
rest was not checked`. Counts are decoded Unicode characters, not UTF-8 bytes.
For PDF/photos the label says **extracted characters**: the denominator is the
available extracted text, not proof that every source page or form was read.
Existing page-limit, failed-page and XFA warnings remain visible. Ask identifies
its permitted cached-excerpt input. At collection completion, queued/running work
is reported as `N documents in marked folders are still being read — ask again in a minute`;
already finished jobs are noted as ready for the next Ask or failed per path.
These notes are a collection-time snapshot, not a live indicator or a guarantee
of completion within one minute.
`agent_read` receipts record `read_chars`, `total_chars`, `truncated` and `character_basis`;
they attest preparation of a local excerpt, not a completed model analysis.

Document jobs use one aggregate estimate of the complete system and user request
(JSON, names, metadata and Ask question included). Sort, Tasks and Health refuse
an over-budget request **before inference**, name every document and publish
no new proposals or baseline fallback. They do not silently trim, batch or chunk.
Archive Ask first ranks candidates locally when the complete archive will not
fit, sends a budget-fitting subset and reports `model saw K of N candidates`.
Desktop rows outside that subset say `not sent to model — budget`, not
`omitted by agent`.
A model request that still cannot fit (including an oversized Ask question)
is refused before inference, not silently shortened.
Previously saved data and the phone's previous proposal batch remain untouched;
local read/cache receipts may already exist. The phone gets HTTP **413** plus
`error`, `documents` and `notes`. A model output cut by its output limit, or a
stream ending without completion, is separately refused with HTTP **422**;
inference did run in that case, but partial results are not published.

The budget is **8192 − 2048 − 256 = 5888 estimated input tokens**, including a
25% calibration margin. The complete canonical UTF-8 request is divided by
**4.43 bytes/token** (rounded down from the worst measured EN/RU/mixed minimum),
then multiplied by 1.25 and rounded up. The same worst-case measured factor is
used throughout, without guessing the request's language. This is an empirical estimate for synthetic English,
Russian and mixed prose, **not an exact tokenizer or a guarantee** for arbitrary
Unicode, OCR noise, different models/templates or complete extraction accuracy.
The local-only calibration tool is `tools/calibrate_document_budget.py`.
Its historical `docs/document-budget-calibration.json` report with raw
`prompt_eval_count` evidence is not included in the public snapshot.
Ordinary chat is unchanged. On this PC the
tested 8192 context uses partial CPU offload; it is not fully resident in 6 GiB VRAM.

The saved calibration JSON is historical evidence: its
`estimated_full_request_tokens` values use the measured minimum bytes/token
for each language, as recorded in its `formula`. They are not recalculated with
the current single 4.43 bytes/token factor used for all requests. Raw counts and
historical estimates remain unchanged; apply the current formula above when
comparing a request with today's admission budget.

Health also limits its response shape: **at most 12 entries per document and
quotes of at most 200 Unicode characters**. Both the system instruction and JSON
schema impose limits; runtime checks each document's row count and unsliced quote
length before accepting any model result. A violation refuses the whole response
with 422; it is not repaired by quietly taking the first rows. Desktop/API notes
always state `Health lists at most 12 entries per document; a long record may have
more`. Baseline additions use only the remaining places within the same cap; with
no model, baseline takes the first 12 per document in source order. If additions
are omitted, an extra note says so. This is a bounded reading, not a complete
medical history or a guarantee of finding the most important facts.

The 27-case calibration above used the earlier unrestricted Health response:
26 responses stopped normally; RU6000 Health reached the 2048-token output limit.
Its exact tool/prompts are preserved at local commit `c1dda65`. The two separate
post-limit Health checks use the current production request and are recorded in
`docs/health-response-limits.json`; old and new measurements are not interchangeable.
The bounded-response synthetic checks at `59a7353` completed normally: RU 6000
characters in 42.8 s (6 accepted entries), EN in 36.9 s (7). Both real requests
carried the limits; their raw quotes stayed within 200 characters. These two
examples are not a guarantee for arbitrary documents or extraction accuracy.
The later `IncompleteRead` handling fix only changes interrupted/error paths;
the saved benchmark fingerprints deliberately remain those of the measured code.

### Local PDF text and OCR

Sort, Find tasks and Health's Read documents can read supported PDFs and photos
from Staging. PDF text and stored AcroForm values are used first; pages with
fewer than 20 native-text alphanumeric characters use local OCR. Form values
do not suppress OCR of a scanned background. The admitted engines are
**Tesseract 5.5.2 or 5.5.3, eng+rus, tessdata_fast**; the actual recognizing
child's version is recorded with the text. Reading is a background job with
`reading scans…` visible; the model still receives at most 6000 characters per
document. Only the first 50 PDF pages are read, with a warning. XFA data is not
extracted. No original file is flattened, rewritten or sent to cloud OCR.
Quotes and dates are checked against the actual extracted excerpt, not the
filename or an assumed OCR correction. Owner preview still displays the source.

The OCR child has a 20-second deadline per page; a crash, timeout, bad model hash
or missing decoder is a visible failure, not successful empty recognition.
Owned temporary-directory removal uses the same bounded retry and explicit
cleanup-error policy as the PDF and agent children; it never silently succeeds.
HEIC depends on an available local decoder. Runtime never downloads an engine or
model. Python socket refusal tests do **not** establish an OS/native network
sandbox. Existing explicit cloud chat, phone and export disclosures above remain.

Derived text in `<root>/.text/<source-sha256>.txt` and its metadata is sensitive
local data. It is hidden from panes, included in backups and reused after a
rename. Purging the last provable copy removes its cache; uncertainty keeps the
cache and visibly requests manual review. Receipts name hashes/engine/pages,
not document text. See [local OCR behavior and setup](docs/local-ocr.md).

**Private-use evaluation only; this package is not redistribution-ready.** The
Windows `tesserocr` wheel is Simon Flueckiger's third-party build, **not an
official Tesseract Windows build**. The manual CPython 3.12 setup uses
wrapper 2.10.0 / Tesseract 5.5.2 /
Leptonica 1.87.0. Tesseract and models are Apache-2.0; wrapper/build scripts MIT;
Leptonica BSD-style; bundled `cysignals` 1.12.6 LGPLv3. Codec, CRT and other native
dependency notices also apply. Do not relabel the whole bundle MIT/Apache or
distribute this private build without a separate dependency/packaging review.
The [engine research and sources](docs/ocr-engine-research.md) identify provenance
and remaining supply-chain limits; the model license is retained
[beside the pinned models](vault_v2/ocr_models/LICENSE).

Manual setup, only when explicitly wanted, using the application's CPython 3.12
Windows x64 virtual environment:

```
.venv\Scripts\python.exe tools\install_local_ocr.py --install
```

This command uses the network to obtain the fixed wheel and models, verifies all
SHA256 values **before** running pip with `--no-index --no-deps`, and publishes
each verified model by an atomic file replacement. It never opens owner files.
Without `--install` it only prints help. This tool is not called by Vault startup.

### Dates and owner corrections

Task deadlines and Health dates are parsed by code from each visible quote.
A model date absent from that quote is cleared, while the item remains with
`date_not_in_quote`. With no recognized relative deadline, a single quoted date
can fill an omitted model date. Multiple distinct absolute dates with no model
selection leave the date empty with `several_dates`; an explicitly selected
absolute date supported by the quote can be accepted. In contrast, a recognized
relative deadline, including a quote mixing absolute and relative dates, always
leaves the extracted date empty with `relative_deadline`, even when the model
selects a quoted absolute date. The owner can set the date separately.
Numeric slash dates use US month/day order and ambiguous ones are flagged.
Titles and labels remain model wording; only the quote and date evidence are checked.

Saved legacy entries are checked on read without rewriting their files. An
unsupported old date is shown without an effective date and marked `not verified`;
it cannot trigger a reminder. Day counts and reminder timing use calendar code.

**Edit due date…** in Tasks and **Edit date…** in Health offer a calendar and
**No date**. An owner choice (`due_source: owner`), including an explicit empty
date, takes precedence over quoted dates and clears date warning flags. File
context menus offer **Shelf >**, including custom shelves and a check beside the
current choice. **Manage shelves…** lives at the bottom of that submenu;
Ctrl+Shift+S still opens the manager, without a toolbar button. A shelf choice
applies to every selected file with one receipt per file; a failed file does not
undo or discard the others, and the result reports `N set, M failed`. The card is
confirmed as `HUMAN`; a file without a card receives a minimal card with no topics.
All three edits use the root guard, leave receipts and invalidate the matching
API proposal batch. See [the date and owner-edit API](docs/grounded-dates.md).

Document jobs (Tasks, Health, Sort, Ask) send request-local `temperature: 0`,
`seed: 42`, `num_ctx: 8192`, `num_predict: 2048` and a structural JSON schema to
Ollama. Chat sends neither `format` nor
these options. A schema constrains syntax; source quotes and dates still need
the independent runtime checks above. Malformed output is never repaired.

Phone text preview reads up to 2 MiB of UTF-8 bytes with replacement decoding and
reports `truncated`. This owner-view limit does not change the model's 6000-character
per-file excerpt, text-permission boundary or `agent_read` receipt.
Phone UI integration is separate.

### Read-only previews on the PC

Double-click a file, press Enter, or choose **Open** in its context menu to view
it inside Vault. Images fit the window and support zoom; text (including Markdown)
is shown in full as plain, read-only, scrollable text. PDF pages have previous/next
controls. Opening or changing a PDF page says `rendering…` while a background
worker supervises the child process; the GUI remains responsive. Closing the
viewer cancels the pending operation and discards its late result. Navigation
waits for the pending page, and a failed page change retains the last good image
and its page number. Unsupported types show `no preview for this file type` plus name, size,
modified time, shelf and the first 12 SHA-256 characters.

Preview does not acquire the Vault write guard, write files or create receipts;
it also works in a read-only window. Only visible regular files inside the three
panes are accepted, with links/junctions rejected before reading. These checks
do not sandbox a hostile process replacing filesystem entries after validation.
There is no external-open button, OS launcher, form editor or model call.

PDF rendering uses pinned **pypdfium2 5.13.0** with PDFium form initialization and
form-field rendering, then displays the resulting image in Qt. The renderer is
separate from Qt and shared with the phone PDF endpoints. PDFium and pypdf run
only in a one-shot child process, also for page metadata and native text/form
extraction. Each operation has a 10-second deadline, including admission wait;
source input is at most 512 MiB and child output at most 64 MiB. Only one PDF
child is admitted at a time per app process. HTTP refuses a busy PDF operation
with 503 instead of queueing. A child crash, timeout or invalid output produces
a visible refusal; it does not run the native parser inside the window.
Windows Job cleanup reaps the owned child and descendants. This is not a
hostile-code sandbox or a total memory/whole-document time guarantee; local
filesystem reads and OS cleanup can add latency. No document path or parent
credentials are passed to the child; the snapshot travels through stdin.
After process/pipe teardown, the newly created child directory is removed with
up to 2 seconds of retries, 50 ms apart. Persistent removal failure is an explicit
cleanup error (temporary local data may remain), not an accepted page. This
retry budget is additional to the operation deadline and does not bound an OS
filesystem call that itself stalls. No other temporary directories are scanned.
Ordinary QtPdf annotation rendering alone omits form widgets in our tested build;
we do not treat successful loading as proof that filled fields are visible.
Checks cover synthetic filled AcroForms both with a saved appearance and with
only a field value (`/V`, no `/AP`), including owner-password-only encryption and
both `NeedAppearances` settings. This is not a promise for every PDF.
The `pypdf[crypto]` dependency supports structural checks of AES-encrypted forms.
If a damaged PDF or unavailable crypto provider prevents that check, preview
reports that form structure could not be checked; it never claims XFA is absent.
XFA detected structurally through pypdf receives an explicit warning:
`this is an XFA form; its filled data may not show here`.
The source PDF is never flattened, modified, saved or exported by preview.

Licenses: the installed pypdfium2 package identifies **Apache-2.0 / BSD-3-Clause**
for its bindings; bundled PDFium has a BSD-style license and additional dependency
notices. Preserve the wheel's `licenses/` and `BUILD_LICENSES/` when distributing
an application. See the [upstream licensing notes](https://pypi.org/project/pypdfium2/5.13.0/)
and the exact license files in `pypdfium2-5.13.0.dist-info/licenses/`.

Synthetic native check (does not open real documents or start network services):
`python -B -m tools.check_desktop_viewer_ui --seconds 900`.
The smaller offscreen process/viewer check is
`python -B -m tools.qualify_pdf_process_preview --output-directory <new-folder>`.
See [scope and qualification](docs/pdf-process-topics.md).

### PDF pages for the phone

Authenticated `GET /api/pdf/info` returns the true page count and the same form
warning as the desktop. `GET /api/pdf/page` returns a PNG rendered with the same
native form support. Pages are zero-based; HTTP permits only the first 50 pages,
with a default width of 1080 and an allowed width of 1–1600 pixels.
Both routes accept only visible pane files, create no receipts, do not acquire
the Vault write guard and use `Cache-Control: no-store`. A busy PDF operation
(desktop or HTTP) returns 503 `busy` immediately to another HTTP request, before
reading its file.

The current Android viewer has narrower client limits: it requests the first
**30 pages at 1000 pixels wide**, and shows a notice if more pages remain. Each
page request gets at most **five attempts total** on `busy` (the initial attempt
plus four retries, with waits of 400/800/1200/1600 ms). The initial `info` request
is outside that retry loop. These client choices do not change the server's
50-page limit, 1080-pixel default or immediate busy response. See
[the PDF API contract](docs/pdf-api.md).

### Proposal confirmations over the API

Sort, Tasks and Health proposals use random, one-use `id` handles bound to the
shown payload, not document/task/entry IDs. A newly published list (even empty)
or a change in the matching store makes old handles stale. This includes changes
made in desktop dialogs. Invalid, duplicate, stale or already-used selections
return HTTP **409**: `proposals changed — refresh the list`, before any
confirmation/addition writes. Sort drafts are still saved when proposed.

The complete selection and all Sort edits are checked before writing. All
selected handles are consumed before the first write attempt; a failure stops
the remaining writes and reports completed, unknown-effect and not-attempted
handles. There is no automatic retry or rollback. Successful partial selection
also invalidates the remaining proposals: refresh before another selection.
Generation counters track this process's stores; the root write guard is
outermost and the desktop runtime lease excludes a second writable window.
After a possible write effect fails, further root writes return 503 until a
process restart; the first partial proposal outcome still names unknown effects.

An unsuccessful model call (`LocalModelUnavailable`, HTTP 503) does not replace
the current API proposal batch. Model responses use the same parser as the
desktop: an array inside Markdown fences or surrounding prose is accepted;
unusable reply text publishes the rules-based baseline with a warning, replacing
the old batch, except for the explicit request-budget or incomplete-output refusals
described above (no replacement or baseline). The Android conflict/partial-result UI is separate work, not part
of this server change.
See [the wire contract](docs/proposal-handles.md).

## Settings — `<root>/vault.json` (optional)

```json
{
  "titles": {"documents": "Archive"},
  "agent_backend": "ollama",
  "ollama_model": "llama3.1:8b",
  "dark": false
}
```

Desktop folder text permissions are also stored here as root-relative
`agent_text_folders`, with `agent_text_generation` for request invalidation.
Use the folder context menu to change them; see [archive Ask](docs/archive-ask.md).

## Rules

- Copy is the default; nothing is overwritten (a copy gets ` (1)`).
- F8 moves to `.trash` with a restore manifest; normal Trash entries can be
  restored. A damaged manifest appears as `manifest damaged`; a file or folder
  without one appears as `no manifest`. These entries remain visible, cannot be
  restored automatically, and do not hide healthy entries. No destination is
  guessed and no metadata is repaired automatically.
- **Empty trash** asks for confirmation before permanently deleting healthy
  entries with receipts. Damaged and orphan entries are kept; the dialog reports
  `N removed, M damaged kept`. Keep those bytes for the owner's manual review.
- Receipts in `.receipts/receipts.jsonl` are hash-chained and checked before
  writing. One root guard covers checks, data writes and receipts. A damaged
  chain refuses changes; a failure after an effect says **may have applied**,
  reloads affected store memory from disk, and blocks further writes until a
  process restart. No undo or automatic retry is claimed.
- Exports (step 2) leave only from Staging through an explicit approve
  step.

### One writer, visible uncertainty

Another window on the same root opens **read-only**, with the reason in its
title and write controls disabled. The writable window keeps its own phone
server. `.vault.lock` carries runtime ownership and retains an OS lock; a
demonstrably dead owner can be reclaimed with a receipt, unknown ownership cannot.
A clean close marks release without deleting the lock file. Active background
work must finish before the writable window releases ownership.

If ownership is known, the read-only reason includes the owner's PID; a recorded
PID in damaged metadata is only a troubleshooting hint, not proof of ownership.
Normally, leave `.vault.lock` alone: released locks are reused and demonstrably
dead owners are recovered automatically. If a damaged/unknown lock or a reused
PID leaves the vault read-only **on Windows**:

1. Close all Vault instances and check the shown PID in Task Manager. A missing
   visible window is not enough: no Vault process or background worker may still
   be using this exact vault folder. If ownership remains uncertain, stop.
2. Preserve a copy of `.vault.lock` for diagnosis, then remove **only that file**
   from the confirmed vault folder and start one Vault instance. Do not remove
   receipts, pending evidence, or any other vault files.
3. If deletion is refused, stop: do not force deletion or terminate an unknown
   process. In the tested Windows environment, the runtime's open file prevents
   deletion while it owns the lease. This is not a portable safety guarantee:
   POSIX locks do not prevent unlinking, and shared/network storage is untested.

Closing a running Sort or Ask dialog cancels its pending result immediately.
The model call may continue in the background, but its late success or failure
is discarded: no draft, selection, or receipt is published from that result.
Already published drafts, confirmed work, or receipts for input reads are not
undone. The main window retains ownership until the outstanding worker finishes
safely.

Backup capture runs under the root guard, with a visible busy label. Model calls,
phone calls and backup encryption run outside it. Ordinary HTTP write admission
and publication fail immediately with 503 `vault is busy — try again` when the
root is busy. One intentional exception is the final outcome receipt of an
already started Claude chat-door request: after process teardown it waits for
the guard before recording the outcome and returning an answer or failure.
No model/process wait occurs while holding the guard; receipt errors still refuse
the result.

File operations leave intentions in `.receipts/pending/` before effects. After
a crash the next window lists them as **may have applied**, without repeating,
reversing or declaring success. Closing the notice leaves the files untouched;
explicit acknowledgement keeps their bytes in `pending/seen/` with a receipt.
`/api/monitor` exposes `pending_intentions` for a phone badge. `.vault.lock` is
excluded from backup snapshots; pending evidence remains included.

These safeguards cover cooperating app writers. They do not promise automatic
crash repair, protection from arbitrary external file edits, or undo for purge.
See [write guard and pending evidence](docs/write-guard.md) for scope and checks.

## Tests

Windows packaging is a **qualification candidate**, not a user release. The
isolated embedded payload, signed-release staging, explicit handoff and Inno
recipe are documented in [Windows installer status](docs/windows-installer-v1-plan.md).
Some native installation checks have passed; the complete clean-machine and
installed update acceptance matrix remains open. The [separate updater](docs/windows-updater-ui.md)
does nothing until pressed and is disabled without explicit key/channel build
inputs. Building does not supply owner keys or publish a release.

For the full source suite, first complete the explicit CPython 3.12 x64
[OCR setup](#local-pdf-text-and-ocr) described under [Run](#run). Installing
`.[dev]` alone does not supply the engine required by the real OCR integration
tests. Use synthetic data only; missing prerequisites are not passing tests.

```
.venv\Scripts\python.exe -m pytest
```
