from orchestrator.schemas.task_graph import Agent, Outcome, Status


def resolved_task(task, tasks):
    seen = set()
    while task is not None and task.replaced_by_task_id:
        if task.task_id in seen:
            return None
        seen.add(task.task_id)
        task = tasks.get(task.replaced_by_task_id)
    return task


def blockers(task, tasks):
    reasons = []
    for predecessor_id in task.depends_on:
        p = tasks.get(predecessor_id)
        p = resolved_task(p, tasks)
        if p is None:
            reasons.append(f"missing dependency {predecessor_id}")
        elif p.status not in (Status.DONE, Status.CLOSED) or p.outcome != Outcome.SUCCESS:
            reasons.append(f"dependency {predecessor_id} must succeed")
        elif (p.agent == Agent.EXECUTE and task.agent != Agent.VERIFY_INTEGRATE
              and not p.integrated):
            reasons.append(f"integration barrier {predecessor_id}")
    return reasons


def ready_tasks(tasks, agent):
    return sorted((t for t in tasks.values() if t.agent == agent
                   and t.status == Status.TODO and not blockers(t, tasks)),
                  key=lambda t: (t.priority, t.task_id))
