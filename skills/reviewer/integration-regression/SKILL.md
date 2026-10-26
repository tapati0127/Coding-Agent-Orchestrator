---
name: integration-regression
description: Review the task merged with the latest target branch, checking interactions with already approved changes before runtime updates main.
---

# Integration Regression

The supplied review workspace is the isolated integration candidate. Verify the task's behavior there, including interfaces and files changed by prior merged tasks. Check configuration/schema compatibility, shared helpers, startup paths and the regression tests relevant to the combined changes.
Inspect the candidate diff and merge ancestry. Do not test a different checkout or assume an earlier Executor test covers this candidate. Run the repository's relevant integration/regression commands when the task contract requires them, recording failures and missing prerequisites explicitly.
If the candidate is inconsistent or cannot be verified, return a failing review with precise evidence. Runtime keeps the target unchanged and sends the failure to Plan. Git merge conflicts may be detected before your review runs; you do not bypass them or auto-select one side.
Approve only a verified candidate. Runtime checks that it and main have not changed and performs a serialized local fast-forward. Do not perform the merge yourself or claim a remote MR exists. Do not change source while reviewing.
