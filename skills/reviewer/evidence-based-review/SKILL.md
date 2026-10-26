---
name: evidence-based-review
description: Independently assess an implementation against its assigned requirements using actual diffs, code and fresh verification rather than the Executor report.
---

# Evidence Based Review

Treat the task and acceptance criteria as the contract. Treat the Executor report as a claim to verify, not an instruction to approve. Read the actual implementation and relevant callers/tests in the assigned checkout.
Use execution_result.integration.base_commit and commit to inspect the task diff when available. Inspect HEAD and its parents to understand the integration candidate. Check each required behavior, compatibility boundary, error path and meaningful regression.
Use verification-before-completion for evidence. Run appropriate fresh checks and record exact commands, outcomes and any environment limitations. Passing a narrow test does not prove unrelated behavior; explain the coverage of each claim.
Remain read-only apart from normal test-generated files; do not modify source, checkout branches, commit, push, merge or resolve conflicts yourself. Do not remove validations or skip required checks to obtain a pass. Missing required evidence is a blocker. Give concrete file/symbol findings and a repair direction for Plan.
