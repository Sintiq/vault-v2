# Explicit supplemental notices in Windows payloads

Date: 2026-09-27. Build-time transport only; no license-clearance claim.

`build_payload(..., notice_bundle_path=...)` / `--notice-bundle` accepts an
explicit local JSON manifest. Omitting it retains original dependency notices
only and records supplemental notices as absent. It never downloads notices
or searches adjacent files automatically.

```json
{
  "schema": "vault-notice-inputs@1",
  "files": [
    {"name": "LIBRARY-LICENSE.txt", "size": 123, "sha256": "64 lowercase hex characters"}
  ]
}
```

The example is schematic, not a valid ready-to-use hash. Texts sit beside the
manifest. Only listed flat `.txt` names are accepted; no separators, traversal,
Windows device names, alternate streams or case-insensitive duplicate names.
The schema is strict and duplicate JSON keys are refused. No field can claim
legal completeness, certification or release approval.

All input files are read and matched against exact length/SHA before runtime
downloads or creation of the output parent. Verified bytes are held in memory
and those same bytes are copied, without a second source read. Limits: 64 KiB
manifest, 256 entries, 2 MiB each, 16 MiB total. Existing linked-path refusal
applies; this is not protection from an active concurrent filesystem attacker.

The payload receives `third-party-notices/inputs.json` byte-for-byte plus
exact listed texts. The normal final inventory hashes all of them. Neighboring
unlisted files are excluded; original Python/wheel/model notices are untouched.
`supplemental_notices_status=included_not_release_cleared` does not change
`release_status=unqualified` or assert corresponding-source availability.

## Evidence

At the already agreed public builder seam, the positive test first failed with
unsupported `notice_bundle_path`, then passed after implementation. Eleven
invalid-input cases passed against that implementation: digest, shorter/longer
size, missing file, traversal, reserved name, case duplicate, duplicate JSON key,
unsupported completeness claim, boolean size and empty list. Invalid tests
remove a cached runtime and forbid network, proving rejection precedes fetch
and output creation. Positive coverage verifies original CRLF bytes, manifest,
unlisted sibling exclusion, inventory inclusion and unchanged release status.

Focused payload/installer suite: **117 passed, 91.78 seconds** while the VM was
running. No new full-suite result is claimed. Qualification11 remains byte-
identical; only a newly built candidate can demonstrate inclusion of this kit.

A colleague's separate static read of this narrow diff found no concrete
contract defect, but did not execute tests. It confirmed that ordinary
check-then-copy source changes cannot change the retained bytes, while noting
the inherited `lstat -> open` boundary against an active filesystem attacker.
This is not a complete release audit or three-stream approval.
