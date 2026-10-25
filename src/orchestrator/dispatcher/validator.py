from orchestrator.schemas.task_graph import Agent
from orchestrator.schemas.delegation import validate_delegation


RANK = {a: i for i, a in enumerate(Agent)}


def validate_dag(graph, existing):
    validate_delegation(graph)
    nodes = {n.id: n for n in graph.nodes}
    task_ids = {n.id for n in graph.nodes if n.node_type == "TASK"}
    adjacency = {key: [] for key in task_ids}
    for edge in graph.edges:
        if edge.from_id == edge.to_id:
            raise ValueError("self-loop")
        if edge.to_id not in task_ids:
            raise ValueError("dependency target must be a proposed TASK")
        if edge.from_id not in task_ids and edge.from_id not in existing:
            raise ValueError("unknown dependency")
        if edge.from_id in nodes and edge.from_id not in task_ids:
            raise ValueError("non-task nodes cannot be dependency predecessors")
        if edge.from_id in task_ids:
            adjacency[edge.from_id].append(edge.to_id)
    visiting, visited = set(), set()

    def visit(key):
        if key in visiting:
            raise ValueError("dependency cycle")
        if key in visited:
            return
        visiting.add(key)
        for nxt in adjacency[key]:
            visit(nxt)
        visiting.remove(key)
        visited.add(key)
    for key in adjacency:
        visit(key)
    for node in graph.nodes:
        if node.node_type != "TASK" and node.target_agent != Agent.COMMUNICATOR:
            raise ValueError("non-task nodes must target Communicator")
        for ref in node.context_refs:
            if not ref.startswith(("shared://", "artifact://")):
                raise ValueError("private or unsupported context reference")


def backward_request(graph):
    # Human intake/resumption is transport, not another agent delegation.
    if graph.source_task_id == "human":
        return False
    backwards = [n for n in graph.nodes if n.node_type == "TASK"
                 and (RANK[n.target_agent] < RANK[graph.producer_agent]
                      or n.target_agent == graph.producer_agent == Agent.COMMUNICATOR)]
    if any(not n.reason for n in backwards):
        raise ValueError("backward delegation requires reason")
    return bool(backwards) or (graph.producer_agent == Agent.COMMUNICATOR
                              and graph.human_request is not None)


def validate_authoritative_dag(tasks):
    """Include existing dependencies and replacement aliases in cycle checks."""
    from .dag_scheduler import resolved_task

    visiting, visited = set(), set()

    def visit(task):
        task = resolved_task(task, tasks)
        if task is None:
            raise ValueError("missing or cyclic replacement")
        key = task.task_id
        if key in visiting:
            raise ValueError("dependency cycle through existing task/replacement")
        if key in visited:
            return
        visiting.add(key)
        for dep in task.depends_on:
            if dep not in tasks:
                raise ValueError("unknown authoritative dependency")
            visit(tasks[dep])
        visiting.remove(key)
        visited.add(key)
    for task in tasks.values():
        visit(task)
