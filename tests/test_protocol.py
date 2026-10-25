import pytest
from pydantic import ValidationError

from orchestrator.dispatcher.dag_scheduler import blockers, ready_tasks
from orchestrator.dispatcher.transitions import transition
from orchestrator.dispatcher.validator import backward_request, validate_dag
from orchestrator.schemas.task import Task
from orchestrator.schemas.task_graph import Agent, Outcome, Status, TaskGraphOutput


def graph(**overrides):
    value = dict(project_id="project", source_task_id="source", producer_agent="PLAN_DELEGATE",
                 execution_chain_id="chain", nodes=[], edges=[])
    value.update(overrides)
    return TaskGraphOutput.model_validate(value)


def node(id, target="EXECUTE", **overrides):
    return dict(id=id, target_agent=target, title=id, **overrides)


def task(id, agent=Agent.EXECUTE, **overrides):
    data = dict(task_id=id, project_id="project", plan_id="plan", agent=agent,
                title=id, root_task_id="root", execution_chain_id="chain")
    data.update(overrides)
    return Task(**data)


def test_shared_schema_for_all_six_agents_and_empty_graph():
    for agent in Agent:
        parsed = graph(producer_agent=agent, source_result={"outcome": "SUCCESS"})
        assert parsed.producer_agent == agent
        assert parsed.nodes == []
    schema = TaskGraphOutput.model_json_schema(by_alias=True)
    assert set(schema["properties"]["edges"]["items"]) == {"$ref"}
    assert "from" in schema["$defs"]["GraphEdge"]["properties"]


@pytest.mark.parametrize("change", [
    {"producer_agent": "ADMIN"}, {"schema_version": "2.0"},
    {"source_result": {"outcome": "SUCCESS", "proposed_status": "IN_REVIEW"}},
    {"nodes": [node("same"), node("same")]},
    {"nodes": [node("bad", priority=-1)]}, {"extra": "not in protocol"},
])
def test_invalid_protocol_is_rejected(change):
    with pytest.raises(ValidationError):
        graph(**change)


@pytest.mark.parametrize("edges", [
    [{"from": "a", "to": "a"}],
    [{"from": "a", "to": "b"}, {"from": "b", "to": "a"}],
    [{"from": "missing", "to": "a"}],
    [{"from": "a", "to": "missing"}],
])
def test_invalid_dependency_graphs(edges):
    with pytest.raises(ValueError):
        validate_dag(graph(nodes=[node("a"), node("b")], edges=edges), {})


def test_external_predecessor_and_all_join_direction():
    output = graph(nodes=[node("a"), node("join", "VERIFY_INTEGRATE")], edges=[
        {"from": "existing", "to": "join"}, {"from": "a", "to": "join"}])
    validate_dag(output, {"existing": task("existing")})


@pytest.mark.parametrize("ref", ["private://other/secret", "file:///etc/passwd", "../secret"])
def test_private_or_arbitrary_context_refs_rejected(ref):
    with pytest.raises(ValueError, match="context reference"):
        validate_dag(graph(nodes=[node("a", context_refs=[ref])]), {})


def test_notification_is_not_a_dependency_node():
    with pytest.raises(ValueError):
        validate_dag(graph(nodes=[node("notice", "COMMUNICATOR", node_type="NOTIFICATION"),
                                  node("work")], edges=[{"from": "notice", "to": "work"}]), {})


def test_backward_delegation_requires_reason():
    with pytest.raises(ValueError, match="reason"):
        backward_request(graph(producer_agent="VERIFY_INTEGRATE", nodes=[node("redo")]))
    assert backward_request(graph(producer_agent="VERIFY_INTEGRATE",
                                  nodes=[node("redo", reason="Tests failed")]))
    assert not backward_request(graph(nodes=[node("next")]))


def test_all_join_waits_for_every_successful_predecessor():
    a = task("a", status=Status.DONE, outcome=Outcome.SUCCESS)
    b = task("b", status=Status.DOING)
    join = task("join", Agent.VERIFY_INTEGRATE, depends_on=["a", "b"])
    tasks = {t.task_id: t for t in [a, b, join]}
    assert ready_tasks(tasks, Agent.VERIFY_INTEGRATE) == []
    b.status = Status.DONE
    b.outcome = Outcome.FAILED
    assert ready_tasks(tasks, Agent.VERIFY_INTEGRATE) == []
    b.outcome = Outcome.SUCCESS
    assert ready_tasks(tasks, Agent.VERIFY_INTEGRATE) == [join]


def test_downstream_code_requires_integration_barrier():
    a = task("a", status=Status.DONE, outcome=Outcome.SUCCESS)
    downstream = task("later", depends_on=["a"], requires_integration=True)
    tasks = {t.task_id: t for t in [a, downstream]}
    assert "integration barrier a" in blockers(downstream, tasks)
    a.integrated = True
    assert blockers(downstream, tasks) == []


def test_only_verify_enters_review_and_only_finalize_authorization_closes():
    execute = task("a", status=Status.DOING)
    with pytest.raises(ValueError, match="illegal transition"):
        transition(execute, Status.IN_REVIEW)
    verify = task("v", Agent.VERIFY_INTEGRATE, status=Status.DOING)
    transition(verify, Status.IN_REVIEW)
    transition(verify, Status.DONE)
    with pytest.raises(ValueError):
        transition(verify, Status.CLOSED)
    transition(verify, Status.CLOSED, finalize=True)
    with pytest.raises(ValueError):
        transition(verify, Status.TODO, recovery=True)


def test_rework_does_not_reopen_completed_task():
    finished = task("done", status=Status.DONE)
    with pytest.raises(ValueError):
        transition(finished, Status.TODO)
    orphan = task("orphan", status=Status.DOING)
    transition(orphan, Status.TODO, recovery=True)
    assert orphan.status == Status.TODO


def test_successor_follows_explicit_replacement_only_after_success_and_integration():
    original = task("original", status=Status.DONE, outcome=Outcome.FAILED,
                    replaced_by_task_id="replacement")
    replacement = task("replacement", caused_by_task_id="original", reason="Fix acceptance failure")
    successor = task("successor", depends_on=["original"], requires_integration=True)
    tasks = {t.task_id: t for t in [original, replacement, successor]}
    assert blockers(successor, tasks)
    replacement.status = Status.DONE
    replacement.outcome = Outcome.FAILED
    assert blockers(successor, tasks)
    replacement.outcome = Outcome.SUCCESS
    assert blockers(successor, tasks)  # successful code still needs integration
    replacement.integrated = True
    assert blockers(successor, tasks) == []
    assert original.status == Status.DONE and original.outcome == Outcome.FAILED


@pytest.mark.parametrize("pointer", ["missing", "original"])
def test_missing_or_cyclic_replacement_fails_closed(pointer):
    original = task("original", status=Status.DONE, outcome=Outcome.FAILED,
                    replaced_by_task_id=pointer)
    successor = task("successor", depends_on=["original"])
    assert blockers(successor, {t.task_id: t for t in [original, successor]})


def test_multihop_replacement_preserves_all_join_and_blocks_cycle():
    original = task("original", status=Status.DONE, outcome=Outcome.FAILED, replaced_by_task_id="rework-1")
    first = task("rework-1", status=Status.DONE, outcome=Outcome.FAILED, replaced_by_task_id="rework-2")
    second = task("rework-2", status=Status.DONE, outcome=Outcome.SUCCESS, integrated=True)
    other = task("other", status=Status.DOING)
    successor = task("successor", depends_on=["original", "other"])
    tasks = {t.task_id: t for t in [original, first, second, other, successor]}
    assert blockers(successor, tasks)
    other.status, other.outcome, other.integrated = Status.DONE, Outcome.SUCCESS, True
    assert blockers(successor, tasks) == []
    second.replaced_by_task_id = "original"
    second.outcome = Outcome.FAILED
    assert blockers(successor, tasks)
