---
name: replan-recovery
description: Recover from failed execution, review or local merge using execution_history, approved_work and retained task branches without discarding approved work.
---

# Replan Recovery

Read execution_history and approved_work first. Identify the actual failed criterion, blocker or conflict and any descendants skipped solely because of it.
With Git isolation, main contains merged work only. Failed or unfinished code is at integration.branch/worktree; a conflicting merge candidate may be at integration.review_worktree. Inspect these read-only with git diff/log as necessary. Do not assume failed changes are on main.
Preserve approved work. Plan a repair on the latest target state that incorporates the needed changes from failed branches without overwriting another task's merge. Include the precise branch/diff, current interface, conflict files and reproduction commands in the repair context.
Do not reset, force-push, merge manually or retry the entire old plan. If progress needs information or access the agents lack, report the blocker for the orchestrator/user instead of inventing a workaround. In legacy shared-workspace mode, inspect existing unfinished changes in place.
