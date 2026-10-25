"""Dispatcher builds task inputs; agent processes never need a Store client."""

from uuid import uuid4

from orchestrator.runtime.context_scope import ScopedContext


async def build_job(store, project, task, worker_id, max_backward):
    thread_id = uuid4().hex
    rows = await store.search(("coding", task["project_id"], "tasks"))
    by_id = {row["task_id"]: row for row in rows}
    predecessors, seen = [], set()

    def visit(task_id):
        if task_id in seen:
            return
        seen.add(task_id)
        row = by_id.get(task_id)
        if row:
            for dependency in row.get("depends_on", []):
                visit(dependency)
            predecessors.append(row)

    for task_id in task.get("depends_on", []):
        visit(task_id)
    context = await ScopedContext(store).build(
        project_id=task["project_id"], agent=task["agent"], thread_id=thread_id,
        worker_id=worker_id, context_refs=task.get("context_refs", []),
        execution_chain_id=task["execution_chain_id"],
    )
    chain = await store.get(("coding", task["project_id"], "chains"), task["execution_chain_id"]) or {}
    context["delegation"] = {"backward_count": chain.get("backward_count", 0),
                             "max_backward_delegations": chain.get("max_backward_delegations", max_backward)}
    context["predecessors"] = [
        {key: row.get(key) for key in ("task_id", "outcome", "commit_sha", "result_refs", "title")}
        for row in predecessors
    ]
    context["chain_tasks"] = [
        {key: row.get(key) for key in (
            "task_id", "agent", "status", "outcome", "depends_on", "title", "acceptance_criteria",
            "result_refs", "commit_sha", "integrated", "reason", "replaced_by_task_id", "caused_by_task_id",
        )}
        for row in rows if row.get("execution_chain_id") == task["execution_chain_id"]
    ]
    return {"project": project, "task": task, "thread_id": thread_id,
            "context": context, "predecessors": predecessors}
