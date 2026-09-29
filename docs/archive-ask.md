# Ask across the archive

Desktop Ask and authenticated `POST /api/agent/ask` search visible documents in
Staging, Documents and Personal. Each request collects a fresh archive snapshot;
desktop **Propose** does not reuse the previous request's collection.
This does not expand Chat, Sort, Tasks, Health or Gatekeeper export permissions.

## What Ask may use

- Everywhere: filename, pane-relative path and the saved card if it is confirmed
  (shelf, topics, issuer, year and recipients). A file without a confirmed card
  remains a name-only candidate unless permitted cached text is available.
- In Staging and owner-marked folders: additionally, at most 6000 decoded Unicode
  characters per file from a ready, validated local text cache. Both the keyword
  baseline and the local model may use that excerpt.
- Elsewhere: no source-text decoding, cached-text reading or extraction for Ask.
  A cached copy of identical bytes in an allowed folder does not authorize the
  unmarked path. Technical SHA-256 reads of source bytes are allowed to find the
  matching saved card; those bytes are not decoded or sent to the model.

Ask uses the existing local-model getter. The explicit Claude cloud door is for
chat only and never receives Ask cards, excerpts or questions. With that door
open and no local model, Ask reports the existing chat-only/local-unavailable
refusal. A closed-door no-model run can still use the keyword baseline.

The shared visible-file boundary excludes dot-prefixed components, trash
manifests, links, junctions/reparse paths and unsafe Windows path aliases.
It is not an OS sandbox against hostile same-user filesystem changes.

## Owner permission and cache preparation

On the desktop, right-click a folder in Documents or Personal and choose
**Allow agent to read text here**. The grant includes its visible descendants;
allowed folders show a short `[A]` suffix after the name. Tooltip and accessibility
text spell out the permission, including the granting ancestor where applicable.
**Stop reading text here** withdraws a direct grant. For an inherited
grant, the menu names the granting parent: stopping there also withdraws nested
grants. There is no independent deny below an allowed parent. Staging text
permission is always enabled and cannot be switched off in this menu.

Grants are root-relative `agent_text_folders` in `vault.json`; the revision is
tracked by `agent_text_generation`. Changes use the root writer guard and record
`agent_scope_granted` / `agent_scope_revoked`. Unrelated settings are retained.
Invalid permission settings fail closed and require manual review.

Allowing a folder queues background preparation. Writable-window startup also
queues all saved marked folders, and each Ask can queue missing/stale caches in
explicitly marked folders outside Staging. These triggers share one deduplicated
serial queue per vault root: the same queued/running file is not added again, and only one
archive preparation job runs at a time. It reuses valid cache pairs or locally
extracts supported PDF/photo text; plain-text types including
`.txt` and `.md` use UTF-8 replacement decoding. Existing extraction limits,
warnings, source checks and cache metadata still apply. Preparation runs outside
the root guard; cache publication rechecks permission and source identity under
a short guard. No model is called to prepare the cache. See
[local extraction and OCR limits](local-ocr.md).

Ask consumes only ready, validated cached text and never waits for extraction/OCR.
A cache miss in an explicitly marked folder requests background preparation;
until text is ready, the candidate remains searchable by name and card. The note
`N documents in marked folders are still being read — ask again in a minute`
counts preparation jobs still queued/running when collection finishes, not all
unread files. Jobs already finished at that point are instead noted as ready for
the next Ask or as a preparation failure for that path. These are collection-time
snapshots, not live status updates while the model runs or after the result is
shown. A later Ask can use a successfully prepared cache. One minute is guidance,
not a completion deadline.
The request itself does not decode original plain text or extract PDF/photo text;
that work belongs to the background queue.

There is no file watcher. New or changed files in marked folders are picked up
by a later Ask or the next writable-window startup; the owner need not revoke
and regrant permission to discover them. A failed job is not immediately retried
in an automatic loop, but a later Ask can request another attempt while the path
is still permitted. Staging retains its implicit text permission and can use
ready caches, but Ask misses there do not queue preparation. The automatic
archive queue is limited to explicitly marked folders outside Staging.

Revocation takes effect for the next Ask and invalidates an in-progress request's
permission snapshot. Permissions are checked before source/cache access and again
before accepting cached text, dispatching a model request and returning its result.
A running extraction may finish its computation, but cannot publish with revoked
permission. Revocation does not erase bytes already read or sent to the local
model. It retains `.text` files and historical backups; Ask must not read their
text through a now-unpermitted path.

Collected source paths and SHA-256 values are rechecked before model dispatch
and before returning a result, including name/card-only candidates. A moved,
trashed or byte-changed source refuses the changed request instead of publishing a
selection from the old snapshot. A refusal after model dispatch cannot undo the
local model computation or input-read receipts already produced.

## Coverage, receipts and large archives

Each prepared cached excerpt has an `agent_read` receipt with source hash,
`read_chars`, `total_chars`, `truncated` and `character_basis: cached`, not its text.
The final receipt follows a guarded permission/source check. The receipt proves
excerpt preparation, not a completed model analysis. Notes carry extraction
warnings and `read N of M` coverage; the denominator is cached/extracted text,
not a guarantee that every source page or form field was understood.

The keyword baseline uses Unicode `casefold` and `ё` → `е`, not morphology.
If all candidates fit the aggregate input budget, the local model can see all
of them. Otherwise Ask locally ranks candidates by keyword evidence in their
names, card metadata and permitted excerpts, then takes a budget-fitting prefix.
The result states `model saw K of N candidates`; the baseline still considers the
full collection. This is a disclosed subset, not whole-archive model analysis or
a claim that every relevant document was selected.
Desktop rows not sent in that subset say `not sent to model — budget`;
baseline matches remain selected even when not sent to, or not chosen by, the model.
The model can rank candidates and add matches, but cannot remove baseline matches.
`omitted_by_agent` remains a compatible empty field: each Ask is a fresh result,
not a revision history of earlier model additions.

The existing estimate and 5888-input-token admission budget still apply to the
complete selected model request, including its question and JSON metadata.
An oversized question, or a highest-ranked candidate that cannot fit even alone
with it, refuses the request before inference; the phone gets HTTP 413 with
`error`, `documents` and `notes`. Incomplete model output is separately refused
with HTTP 422. These refusals do not publish a baseline as a successful model
answer. A normal unusable model reply can return the baseline with a warning.

## Selection and export

Desktop results show `pane/relative/path` and a structural **Proposed sources**
summary of the initial proposal's panes, not the owner's later checkbox edits.
The initial checked set is the union of baseline and model matches;
model order comes first, remaining baseline matches follow, then unchecked rows.
The model's free-text explanation appears separately as **model's note, unverified**,
not as authoritative source information. It cannot change the structured pane/path.
The owner can still untick any row; preserving baseline only sets the initial proposal.
The owner ticks the final set and
chooses **Copy to Staging** for selected archive files. Each successful copy gets
the usual receipt and a unique destination name instead of overwriting a file;
already-Staging files are not copied again. Partial failures remain visible and
do not undo successful copies. Export stays disabled while a selected source
remains outside Staging. Then **Export selected…** follows the unchanged
Gatekeeper preview and explicit approval; finding a file never authorizes export.

The phone Ask response preserves `phrase`, `matches`, `recipient`, `notes` and
`export: "at the desk only"` for a nonempty collection. Each match adds `pane`
and `rel` alongside `name`, `shelf`, `added_by_agent` and `omitted_by_agent`.
For a nonempty archive, the model explanation is a separate `agent_reason` string
(empty when absent), never included in `notes`. Clients should label it unverified.
`notes` contains the structural source summary and coverage/refusal information.
An empty archive returns `phrase`, empty `matches` and `notes: ["Archive is empty"]`.
Budget/error document labels include the pane-relative path.

No HTTP route is added for text grants, revocation or Ask's **Copy to Staging**.
Those owner controls stay on the desktop; existing generic phone file operations
are unchanged. Phone UI rendering is separate from this API contract.

## Verification

See [the test/review record](archive-ask-verification.md) and
[synthetic desktop evidence](archive-ask-qualification.json). The walkthrough
uses a scripted backend and does not qualify real-model retrieval accuracy.
