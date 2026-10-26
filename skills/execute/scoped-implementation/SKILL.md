---
name: scoped-implementation
description: Implement only the assigned task in its supplied checkout, preserving dependency contracts and reporting concrete verification evidence.
---

# Scoped Implementation

Use the checkout supplied as workspace. Resolve any older absolute paths in findings to relative paths in this checkout. Do not edit the main checkout or another task's worktree.
Start from Plan findings, inspect the touched implementation and callers, and reuse established helpers and conventions. Expand discovery only when findings are incomplete or contradicted by code. Keep the complete change within the assigned acceptance criteria; do not broaden the feature or refactor unrelated code.
Use Ponytail's full mode for the smallest complete solution. For bugs, load systematic-debugging when relevant. Before completion, load verification-before-completion and run the checks that support the task's claims; record command, exit result and remaining gaps.
Do not change branches, create worktrees, commit, push or merge. Runtime commits after assessment and asks an independent Reviewer to check an integration candidate. Report changes, relative files, verification evidence and unresolved blockers as natural language for assessment.
