---
name: dependency-planning
description: Build executable task dependencies for parallel worktrees, especially shared-file edits, interface contracts and all-predecessor joins.
---

# Dependency Planning

Turn the requested behavior into tasks with a concrete change, verified findings, relative paths, acceptance criteria, constraints and verification commands. Define interface contracts before assigning consumers.
Add dependency edges whenever a task needs another task's merged code, changes the same files or relies on a schema/configuration introduced there. Independent tasks may run in parallel on separate worktrees. A join must wait for every predecessor.
Keep task boundaries large enough to deliver a coherent change; avoid tiny tasks that repeatedly edit the same file. Include required regression checks in the owning task, not in speculative additional work.
Return a natural-language plan for the structured pass. Do not output Mermaid/JSON artifacts or execute the tasks; runtime validates the DAG and renders the artifact. Task completion requires Execute pass, independent Reviewer pass and local merge when isolation is enabled.
