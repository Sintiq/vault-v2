# Historical synthetic input calibration

`document-budget-input-calibration.json` is a structural allowlist projection of
the original synthetic report named by `source_report_sha256`. Each of its 27
cases retains the original prompt, input-token observation, language, length,
job and completion reason, unchanged. The existing budget test still checks
the complete original input matrix and the historical truncated output case.

Machine inventory, loaded-model metadata, request logs, run identifiers and
timestamps are deliberately not copied. The original report stays in the
private development checkout; distributing it is unnecessary for this test.
These historical observations are neither new model runs nor evidence that all
model outputs succeeded.
