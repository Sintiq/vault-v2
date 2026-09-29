# Root writer and pending evidence

Scope: the desktop window, its HTTP handlers, reminders and background work
cooperate on one canonical Vault root. No database or automatic repair engine.

## Admission and lock order

`ReceiptLog.write(label)` uses the root's shared reentrant guard. Lock order is
always **root guard -> AgentAPI proposal lock -> store mutation lock**. An outer
entry checks the full receipt chain before allowing an effect. Nested operations
reuse the same thread's entry; independent ReceiptLog instances for the same
canonical root share the guard. Read-only belongs to each log/runtime, not the
root's global state, so opening another window cannot disable the first.

Covered writers: VaultOps, Cards/Tasks/Health, Gatekeeper execution, upload and
temporary-upload cleanup, pairing/key creation, shelves, reminder marks and
receipts. Staging read receipts also pass through the same guard. Snapshot
capture holds it for the whole ZIP; encryption is outside, then a second short
entry writes the backup output and completion receipt. API rebinding shares the
same stores/root, not another runtime.

Model inference, warm-up and phone/network requests never run under this guard.
Publication acquires it after inference, while storing drafts/batch revisions.
Ordinary HTTP write admission and publication use nonblocking guard entry,
not a lock over the request body/network lifetime. Contention returns 503
`vault is busy — try again` immediately.

The final outcome receipt of an already started Claude chat-door request is an
intentional exception. After process teardown, `ReceiptLog.completion_write`
waits for the root guard to record `agent_door_result`; the answer or failure is
returned only after that audit step. No model/process wait occurs while holding
the guard. Receipt errors still refuse the result, and initial request admission
remains nonblocking. This is not a queue for ordinary HTTP writes.

Backup runs on a worker; the GUI displays the busy reason. UI-thread admission
is also nonblocking, including receipt/pending reads and confirmations of an
already-open dialog, so a new snapshot cannot make that callback wait silently.

## Outcomes

- Invalid chain before entry: refusal, no operation data changed.
- Normal return: data and corresponding receipt completed.
- Failure after an effect may have begun: `operation may have applied; writes
  blocked until restart`. No automatic retry, no undo claim. Store memory is
  reloaded from actual persisted data; failed first save means an empty store.
- Proposal batches retain their first structured partial outcome. All selected
  handles remain consumed; later writes fail with blocked503 until restart.
- Purge can already have removed bytes; no undo is offered.

The in-process stop latch is cleared by restarting the process, not by making
another ReceiptLog. A damaged chain still refuses writing after restart.

## Runtime ownership

`.vault.lock` is retained while a writable window exists. An OS lock serializes
ownership decisions; metadata includes PID, machine, start time and active or
released state. A live/unknown owner yields a read-only window, without starting
its model or phone services. The second window can browse files but cannot edit.
Only a demonstrably absent process permits stale takeover, recorded as
`runtime_lock_reclaimed`. A clean close marks released while still holding the
OS lock, then unlocks; it does not unlink a successor's file. Shutdown must keep
ownership until admitted HTTP and GUI/background writers have finished.

## B-lite: intentions, not instructions

Before a VaultOps effect, `.receipts/pending/<id>.json` stores the operation,
paths, expected hashes and timestamp. Nested `clear_staging -> trash` leaves one
outer intention; successful completion removes it after receipts. A failure
leaves evidence, including uncertainty about whether any bytes were changed.
Single files carry a content SHA-256. Directories carry `tree_sha256`, computed
over sorted relative-path/file-digest JSON records; purge carries a digest of its
selected slot/hash map. This keeps evidence headers bounded for large folders,
without pretending to supply individual-file repair instructions.

Startup lists unresolved files without modifying them. Close is not acknowledge.
Only the owner's explicit acknowledgement moves exact bytes into `pending/seen/`
and records `pending_acknowledged`; it does not declare the operation successful.
The acknowledgement receipt comes before archiving: a crash before the move
leaves evidence visible, even if the owner's decision is already logged.
Malformed evidence stays visible for manual review. The monitor returns a count
in `pending_intentions`. Backup includes this evidence, but not runtime ownership.
Linked/reparse evidence containers are refused without following their targets.

## Limits

No automatic replay, rollback, power-loss guarantee, all-or-nothing multi-file
effect, or protection from hostile concurrent filesystem changes. The runtime
lease protects cooperating application runtimes; low-level library callers must
honor ownership. Store generations still do not detect arbitrary external file
editing. Simultaneous reminder runs can both notify before the first delivery is
recorded; external exactly-once delivery is not promised. A partially changed
shelf can leave some cards moved to INBOX while the old shelf still exists.

Tests use synthetic roots and loopback HTTP. The native walkthrough script
`tools/manual_write_guard.py` creates two safe windows and forbids networking;
the operator selects only those windows for the read-only check.
