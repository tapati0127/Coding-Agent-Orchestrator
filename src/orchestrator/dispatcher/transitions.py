from orchestrator.schemas.task_graph import Agent, Status


def transition(task, status, *, finalize=False, recovery=False):
    old = task.status
    allowed = {(Status.TODO, Status.DOING), (Status.DOING, Status.DONE),
               (Status.IN_REVIEW, Status.DONE)}
    if task.agent == Agent.VERIFY_INTEGRATE:
        allowed.add((Status.DOING, Status.IN_REVIEW))
    if finalize:
        allowed.add((Status.DONE, Status.CLOSED))
    if recovery:
        allowed |= {(Status.DOING, Status.TODO), (Status.IN_REVIEW, Status.TODO)}
    if (old, status) not in allowed:
        raise ValueError(f"illegal transition {old} -> {status}")
    task.status = status
    task.version += 1
