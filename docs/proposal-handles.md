# One-use proposal API contract

This is the server contract, not a claim that the Android app has already been
updated. Existing endpoint paths and successful confirmation counters remain.

## Display and identity

`POST /api/agent/sort`, `/tasks`, `/health` return `{proposals: [...], notes: [...]}`.
Every returned proposal has a random opaque `id`. Natural identities are separate:

| Family | Display/reference field | Confirmation request |
| --- | --- | --- |
| Sort | `sha256` | `/api/agent/sort/confirm`, `{"items":[{"id":"<handle>"}]}` |
| Tasks | `task_id` | `/api/agent/tasks/add`, `{"ids":["<handle>"]}` |
| Health | `entry_id` | `/api/agent/health/add`, `{"ids":["<handle>"]}` |

Natural identities cannot authorize acceptance. The client need not hash or echo
the payload: its handle identifies the frozen row already held by the server.
The server hashes the complete shown row, including `id` and natural identity,
as canonical JSON: sorted keys, compact separators, UTF-8, no NaN/Infinity. The
same frozen semantic values drive the eventual write; mutating a returned dict
does not change the server snapshot. Sort may additionally carry owner edits to
`shelf`, `topics`, `issuer`, `year`, `recipients`; `Card.build` validates these
before any write.

There is one current batch for each family. New successful publication, including
an empty one, replaces its old handles. `LocalModelUnavailable` from a failed
model/transport call returns HTTP 503 without replacing the batch or advancing
its generation; previously shown handles remain usable unless another operation
has invalidated them. Error details are sanitized, and there is no cloud fallback.

Successful model responses use the very same domain parsers as the desktop.
They extract the JSON array from surrounding text, including Markdown fences.
If reply text cannot be parsed, they publish the deterministic baseline with an
`agent reply unusable` warning (per-row `flags` for Sort, top-level `notes` for
Tasks/Health). This is a successful replacement publication, so the old handles
become stale. Domain filtering/baseline supplementation is unchanged; this is not
an independent grounding claim or a general malformed-protocol hardening change.
Duplicate natural store identities retain the first generated row (Sort uses the
stable input order), so one selection cannot count two overwrites as two additions.

## Validation and concurrency

Each shared store has an in-memory generation and reentrant mutation lock. A
batch records the generation after publication (after saving Sort drafts). Accept
checks that generation first, then the entire selected set, while holding the
same store lock used by desktop mutations. Unknown, wrong-family, stale, reused,
duplicate or structurally invalid handles, or invalid Sort edits, fail closed:

```json
{"error":"proposals changed — refresh the list"}
```

Status **409**, no additional confirm/add/store or receipt writes. Valid handles
in a rejected mixed set are not consumed. Opening/publishing a Sort list still
saves unconfirmed drafts and may log Staging reads; those are not confirmation
effects. A successful selection changes store generation, so any unselected
handles from that batch also require refresh. Generation over-invalidation is
deliberate. Restart forgets handles and resets counters.

A non-object request body on proposal/search routes is HTTP 400 (`request body
must be a JSON object`), not a stale-approval conflict. On confirmation/addition
routes a non-object body remains the same HTTP 409 refusal described above.

Successful API publication itself also advances the in-memory generation, even
for an empty list and even for Tasks/Health which write no proposal data files.
Registries are per API instance, but this shared revision also expires handles
held by an older API instance over the same store objects (for example, around a
server rebind). Failed model calls do not advance it. Distinct store instances
pointing at the same files do not share this counter.

## Store errors are not validation conflicts

After prevalidation, the whole selected set is consumed before the first store
call. Normal success keeps `{"confirmed":N,"errors":[]}` for Sort or `{"added":N}`
for Tasks/Health. On the first store exception, further writes stop; the response
keeps the completed counter and adds an error plus:

```json
{
  "outcome": {
    "completed": ["<handle whose store call returned>"],
    "failed_unknown": ["<handle whose store call raised>"],
    "not_attempted": ["<remaining selected handle>"]
  }
}
```

All three groups remain consumed; never retry automatically. A failure can occur
after saving data but before appending its receipt, or after changing memory but
before saving. Therefore the failed item is **unknown-effect**, not "not written".
The client must inspect the outcome/error fields, not just HTTP success. Current
store failure results are structured method responses, not 409 conflicts.
After a write may have applied, the root blocks later writes until process
restart: subsequent calls return 503, taking precedence over handle409. A busy
root can also return 503 without consuming handles; a retry after contention
must still pass all original one-use and generation checks.

## Refusal before publishing a new batch

An aggregate document request above the calibrated input budget is refused
**before the model call**, with HTTP 413 and `{error, documents, notes}`. The full
document list identifies this attempted request; `notes` retains local reading
limitations. There is no partial/new list, draft publication or baseline fallback.
An incomplete model output (output-token limit or stream without a completion
marker) instead returns HTTP 422 with the same fields: inference already ran,
but the output is not usable as a proposal list. Neither refusal consumes or
replaces a previously published batch; the usual confirmation checks still apply.
Reading/cache receipts can precede refusal; saved cards/tasks/health entries are
not changed by refusing the proposal attempt. Successful responses continue to
carry completeness warnings in top-level `notes`.

## Limits

The lock order is root guard -> proposal lock -> store mutation lock. All
cooperating writes to a root are serialized, including receipt append, and the
runtime lease excludes a second writable desktop window. Model work stays
outside the root guard. These safeguards do not supply rollback, automatic crash
repair, protection against arbitrary code modifying process memory, or global
all-or-nothing changes across stores. External file changes bypass in-memory
generations. See [the root guard contract](write-guard.md).
