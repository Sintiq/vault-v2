# Date evidence and owner edits

Base: 9be8bcb. Scope: date grounding, desktop owner controls, HTTP contract,
owner text preview. No desktop file viewer or phone client changes.

## Read contract

Authenticated `GET /api/shelves` returns `{ "shelves": [...], "standard": [...] }`.
The full current catalog has standard and owner shelves in desktop order;
`standard` contains only defaults. It reflects shelf changes without a server
restart, performs no writes and creates no receipt, including on read-only roots.

Existing `/api/tasks` and `/api/health` fields are retained. Both add
`due_source` (`quote`, `none`, `owner`) and `flags` (array). Tasks also expose
`days_left` (integer or null) and `overdue` (boolean), computed by code. Model
extraction proposals carry the same source/flags. A historical unsupported date
is omitted in the read projection with `not_verified`; saved bytes stay intact.
Owner-supplied null remains an explicit owner decision after reload/extraction.

Supported absolute formats: `2026-10-15`, `10/15/2026`, `15.10.2026`,
`October 15, 2026`, `Oct 15 2026`, `15 октября 2026`. Health keeps supported
literal partial years/months without inventing a day. Slash dates with both
numbers at most 12 use US order and carry `ambiguous_date`. When no relative
deadline is recognized, a missing model date can use one quoted date
(`date_from_quote`); multiple distinct absolute dates with no model selection
leave the date empty with `several_dates`. An explicit model selection supported
by the quote can be accepted even if it contains several absolute dates.
Unsupported model dates produce `date_not_in_quote` while keeping the task/entry.
Task titles and health labels are not independently grounded.

Recognized relative intervals always leave the extracted date empty with
`relative_deadline`, without choosing a base date. Mixed absolute dates and
relative terms also remain unresolved, even if the model selects a literal date.
The desktop explains that the mixed quote contains both and asks the owner to
check and set a date. This is deliberate conservative refusal, not an attempt
to infer which date is the deadline.

## Owner write contract

Authenticated JSON POSTs, with the same root guard and HTTP error semantics
as existing writes:

- `/api/card/shelf`: `{ "pane": "staging", "rel": "note.md", "shelf": "IMMIGRATION" }`.
  Pane can be staging/documents/personal; shelf must already exist. Returns
  `{pane, rel, card}`. Confirms origin HUMAN, receipt `card_confirm`. A missing
  card starts with empty topics. Existing metadata survives the shelf change.
- `/api/task/due`: `{ "id": "task-id", "due": "2026-10-15" }` or due null.
  Returns id/due/due_source/flags, receipt `task_due_set`.
- `/api/health/date`: `{ "id": "entry-id", "date": "2026-10-15" }` or date null.
  Returns id/date/due_source/flags, receipt `health_date_set`.

Manual dates require a real strict ISO YYYY-MM-DD or null; omitted/invalid
values and unknown IDs are refused. Each successful edit increments the matching
store generation: previously shown handles for that store now fail with409.
This includes saving the same owner date or no-date again: a successful
acknowledgment always has a new receipt and invalidates prior handles.
Busy writes return503; readonly roots refuse writes; a possibly applied failure
continues to block writes until restart, per the existing guard contract.

## Owner text view

`GET /api/file?pane=staging&rel=note.md` keeps name/rel/text and adds
`truncated: false|true`. It reads at most2MiB+1 bytes and decodes the first2MiB as
UTF-8 with replacement for invalid/incomplete bytes. Exactly2MiB is not truncated.
Owner preview does not create an agent_read receipt. StagingReader.read_text
continues to return at most6000 characters and emits its existing receipt.

## Document generation and repeatable qualification

Tasks, Health and Sort request an array of objects; Ask requests an object.
Ollama receives the structural schema through `format`, with request-local
`temperature: 0` and `seed: 42`. Ordinary chat receives neither field. The
existing parsers still validate IDs, quotes, kinds, dates and card metadata;
no missing punctuation is appended and no malformed JSON is repaired.
Container schemas are intentional for this slice; per-field schemas are deferred.
Opening the Claude chat door does not change document routing: the local wrapper
preserves these same request options and schema, without a cloud fallback.
This uses [Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs).

Run only the selected model, preserving all synthetic raw replies locally:

```
python -B -m tools.pick_local_model --model llama3.1:8b --warm-runs 5 --output shots/grounded-dates-qualified.json
```

The initial generation is reported separately; its cold/warm residency is not
claimed without server evidence. Five subsequent generations must each preserve
three quoted tasks through both the benchmark score and the real task parser.
The tool exits unsuccessfully if any required warm run fails. `shots/` is ignored;
these built-in inputs contain no owner data. This checks one synthetic document,
not a universal extraction-accuracy guarantee.

For the open-door path, an explicit local-only check uses a fresh synthetic Vault
and the real desktop selector, waits for local warm-up, then scores one answer:

```
python -B -m tools.check_dates_open_door --run-local-synthetic --output shots/grounded-dates-open-door-qualified.json
```

The output must be a new writable file before any model request is made. The
check records the raw reply and actual request options; it does not call Claude
or use owner documents. This checks local document extraction while the cloud
chat door is open, not the cloud responder itself.
