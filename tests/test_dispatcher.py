"""Dispatcher invariants exercised with a real protocol and an isolated Store."""
import uuid
import subprocess

import pytest

from orchestrator.dispatcher.engine import Dispatcher
from orchestrator.schemas.task import Task
from orchestrator.schemas.task_graph import Agent, Outcome, Status
from orchestrator.store import InMemoryStore


PROJECT = "project"


async def setup_dispatcher(**options):
    store = InMemoryStore()
    await store.put(("coding", "projects"), PROJECT,
                    {"project_id": PROJECT, "title": "Test project"})
    return store, Dispatcher(store, **options)


async def seed(store, id, agent="ANALYZE", **overrides):
    data = dict(task_id=id, project_id=PROJECT, plan_id="plan", agent=agent,
                title=id, root_task_id="root", execution_chain_id="chain")
    data.update(overrides)
    task = Task(**data)
    await store.put(("coding", PROJECT, "tasks"), id, task.model_dump(mode="json"))
    return task


def envelope(kind, task=None, payload=None, worker="worker", agent=None, request_id=None):
    return dict(request_id=request_id or uuid.uuid4().hex, kind=kind, project_id=PROJECT,
                agent=agent or (task.agent.value if task else "COMMUNICATOR"),
                worker_id=worker, task_id=task.task_id if task else None,
                payload=payload or {})


def proposal(task, *, nodes=None, edges=None, outcome="SUCCESS", **extra):
    return dict(schema_version="1.0", project_id=PROJECT, source_task_id=task.task_id,
                producer_agent=task.agent.value, execution_chain_id=task.execution_chain_id,
                summary="Result", source_result={"outcome": outcome}, nodes=nodes or [],
                edges=edges or [], **extra)


def node(id, agent="PLAN_DELEGATE", **extra):
    return dict(id=id, target_agent=agent, title=id, **extra)


async def active(store, id="source", agent="ANALYZE", **overrides):
    defaults = dict(status=Status.DOING, assigned_worker="worker", lease_expires_at=2000., attempt=1)
    defaults.update(overrides)
    return await seed(store, id, agent, **defaults)


async def test_valid_result_applies_source_and_new_tasks():
    store, dispatcher = await setup_dispatcher(clock=lambda: 1000.)
    source = await active(store)
    result = await dispatcher.handle(envelope("result", source,
                                              proposal(source, nodes=[node("plan")])) )
    assert result["accepted"]
    tasks = await dispatcher.tasks(PROJECT)
    assert tasks[source.task_id].status == Status.DONE
    assert tasks[source.task_id].outcome == Outcome.SUCCESS
    children = [t for t in tasks.values() if t.task_id != source.task_id]
    assert len(children) == 1
    assert children[0].agent == Agent.PLAN_DELEGATE
    assert children[0].parent_task_id == source.task_id
    assert children[0].execution_chain_id == source.execution_chain_id


async def test_duplicate_request_is_idempotent_and_does_not_duplicate_tasks():
    store, dispatcher = await setup_dispatcher(clock=lambda: 1000.)
    source = await active(store)
    request = envelope("result", source, proposal(source, nodes=[node("plan")]))
    first = await dispatcher.handle(request)
    second = await dispatcher.handle(request)
    assert first["accepted"] and second["accepted"]
    tasks = await dispatcher.tasks(PROJECT)
    assert len(tasks) == 2


async def test_request_id_cannot_be_reused_with_different_content():
    store, dispatcher = await setup_dispatcher(clock=lambda: 1000.)
    source = await active(store)
    request = envelope("result", source, proposal(source, nodes=[node("plan")]))
    assert (await dispatcher.handle(request))["accepted"]
    request["payload"]["summary"] = "Changed request"
    assert not (await dispatcher.handle(request))["accepted"]
    assert len(await dispatcher.tasks(PROJECT)) == 2


async def test_request_id_is_unique_across_producer_agents():
    store, dispatcher = await setup_dispatcher(clock=lambda: 1000.)
    analysis = await active(store, "analysis", "ANALYZE", assigned_worker="analysis-worker")
    plan = await active(store, "planner", "PLAN_DELEGATE", assigned_worker="plan-worker")
    rid = "same-request-id"
    first = await dispatcher.handle(envelope("result", analysis,
        proposal(analysis, nodes=[node("next")]), worker="analysis-worker", request_id=rid))
    second = await dispatcher.handle(envelope("result", plan,
        proposal(plan, nodes=[node("next", "EXECUTE")]), worker="plan-worker", request_id=rid))
    assert first["accepted"]
    assert not second["accepted"]
    tasks = await dispatcher.tasks(PROJECT)
    assert tasks["planner"].status == Status.DOING
    assert len(tasks) == 3


async def test_pending_journal_replays_after_mid_apply_crash_without_duplicate_tasks():
    class InterruptedStore(InMemoryStore):
        armed = False

        async def put(self, ns, key, value):
            if self.armed and tuple(ns) == ("coding", PROJECT, "tasks") and key != "source":
                self.armed = False
                raise OSError("simulated process crash during authoritative writes")
            await super().put(ns, key, value)

    store = InterruptedStore()
    await store.put(("coding", "projects"), PROJECT, {"project_id": PROJECT})
    source = await active(store)
    request = envelope("result", source, proposal(source, nodes=[node("plan")]))
    store.armed = True
    dispatcher = Dispatcher(store, clock=lambda: 1000.)
    with pytest.raises(OSError, match="simulated process crash"):
        await dispatcher.handle(request)
    journals = await store.search(("coding", PROJECT, "operations"))
    assert len(journals) == 1 and not journals[0]["applied"]
    assert await store.get(("coding", PROJECT, "responses", "ANALYZE"), request["request_id"]) is None
    restarted = Dispatcher(store, clock=lambda: 1000.)
    receipt = await restarted.handle(request)
    assert receipt["accepted"]
    assert len(await restarted.tasks(PROJECT)) == 2
    assert (await store.search(("coding", PROJECT, "operations")))[0]["applied"]


@pytest.mark.parametrize("nodes,edges", [
    ([node("a"), node("b")], [{"from": "a", "to": "b"}, {"from": "b", "to": "a"}]),
    ([node("a")], [{"from": "absent", "to": "a"}]),
    ([node("a", context_refs=["private://other/secret"])], []),
    ([node("a", agent="UNRECOGNIZED")], []),
])
async def test_rejected_graph_does_not_partially_complete_source(nodes, edges):
    store, dispatcher = await setup_dispatcher(clock=lambda: 1000.)
    source = await active(store)
    result = await dispatcher.handle(envelope("result", source,
                                              proposal(source, nodes=nodes, edges=edges)))
    assert not result["accepted"]
    tasks = await dispatcher.tasks(PROJECT)
    assert len(tasks) == 1
    assert tasks[source.task_id].status == Status.DOING


async def test_wrong_worker_cannot_finish_another_workers_task():
    store, dispatcher = await setup_dispatcher(clock=lambda: 1000.)
    source = await active(store)
    result = await dispatcher.handle(envelope("result", source, proposal(source), worker="intruder"))
    assert not result["accepted"]
    assert (await dispatcher.tasks(PROJECT))[source.task_id].status == Status.DOING


async def test_wrong_producer_and_chain_cannot_spoof_result():
    store, dispatcher = await setup_dispatcher(clock=lambda: 1000.)
    source = await active(store)
    for change in [{"producer_agent": "FINALIZE"}, {"execution_chain_id": "other-chain"},
                   {"project_id": "other-project"}, {"source_task_id": "other-task"}]:
        output = proposal(source)
        output.update(change)
        result = await dispatcher.handle(envelope("result", source, output))
        assert not result["accepted"]


async def test_envelope_agent_cannot_claim_another_agents_active_result():
    store, dispatcher = await setup_dispatcher(clock=lambda: 1000.)
    source = await active(store)
    request = envelope("result", source, proposal(source), agent="FINALIZE")
    assert not (await dispatcher.handle(request))["accepted"]
    assert (await dispatcher.tasks(PROJECT))[source.task_id].status == Status.DOING


async def test_claim_is_exclusive_for_two_workers():
    store, dispatcher = await setup_dispatcher(clock=lambda: 1000.)
    await seed(store, "pending", "EXECUTE")
    a = await dispatcher.handle(envelope("claim", agent="EXECUTE", worker="execute-01"))
    b = await dispatcher.handle(envelope("claim", agent="EXECUTE", worker="execute-02"))
    assert a["accepted"]
    assert a["task"]["task_id"] == "pending"
    assert not b["accepted"]
    task = (await dispatcher.tasks(PROJECT))["pending"]
    assert task.assigned_worker == "execute-01"
    assert task.attempt == 1


async def test_global_qwen_slots_apply_to_analyze_and_execute_together():
    store, dispatcher = await setup_dispatcher(clock=lambda: 1000., max_qwen_sessions=2)
    await active(store, "analysis", "ANALYZE")
    await active(store, "execution", "EXECUTE", assigned_worker="execute-01")
    await seed(store, "next", "PLAN_DELEGATE")
    result = await dispatcher.handle(envelope("claim", agent="PLAN_DELEGATE", worker="plan-01"))
    assert not result["accepted"]
    assert (await dispatcher.tasks(PROJECT))["next"].status == Status.TODO


async def test_claim_all_join_waits_for_two_predecessors():
    store, dispatcher = await setup_dispatcher(clock=lambda: 1000.)
    await seed(store, "a", status=Status.DONE, outcome=Outcome.SUCCESS)
    await seed(store, "b", status=Status.DOING, assigned_worker="b-worker", lease_expires_at=2000.)
    await seed(store, "join", "VERIFY_INTEGRATE", depends_on=["a", "b"])
    first = await dispatcher.handle(envelope("claim", agent="VERIFY_INTEGRATE", worker="verify"))
    assert not first["accepted"]
    await seed(store, "b", status=Status.DONE, outcome=Outcome.FAILED)
    second = await dispatcher.handle(envelope("claim", agent="VERIFY_INTEGRATE", worker="verify"))
    assert not second["accepted"]
    await seed(store, "b", status=Status.DONE, outcome=Outcome.SUCCESS)
    third = await dispatcher.handle(envelope("claim", agent="VERIFY_INTEGRATE", worker="verify"))
    assert third["accepted"] and third["task"]["task_id"] == "join"


async def test_only_finalize_can_request_closed_status():
    store, dispatcher = await setup_dispatcher(clock=lambda: 1000.)
    source = await active(store)
    output = proposal(source)
    output["source_result"]["proposed_status"] = "CLOSED"
    result = await dispatcher.handle(envelope("result", source, output))
    assert not result["accepted"]
    assert (await dispatcher.tasks(PROJECT))[source.task_id].status == Status.DOING


async def test_finalize_can_close_successful_chain_including_itself():
    store, dispatcher = await setup_dispatcher(clock=lambda: 1000.)
    await seed(store, "earlier", status=Status.DONE, outcome=Outcome.SUCCESS)
    source = await active(store, "final", "FINALIZE")
    output = proposal(source)
    output["source_result"].update(proposed_status="CLOSED", close_task_ids=["earlier", "final"])
    result = await dispatcher.handle(envelope("result", source, output))
    assert result["accepted"]
    assert all(t.status == Status.CLOSED for t in (await dispatcher.tasks(PROJECT)).values())


async def test_backward_budget_is_shared_by_chain_and_fourth_requires_human():
    store, dispatcher = await setup_dispatcher(clock=lambda: 1000.)
    for index in range(4):
        source = await active(store, f"review-{index}", "VERIFY_INTEGRATE")
        output = proposal(source, nodes=[node(f"rework-{index}", "EXECUTE", reason="Tests failed")],
                          outcome="CHANGES_REQUESTED")
        receipt = await dispatcher.handle(envelope("result", source, output))
        if index < 3:
            assert receipt["accepted"]
        else:
            # The completed source result is recorded, but no fourth backward
            # delegation is authorized; a human decision must create a new chain.
            assert receipt["accepted"] and receipt["human_intervention"]
            assert receipt["delegation_accepted"] is False
            assert receipt["task_ids"] == []
    approvals = await store.search(("coding", PROJECT, "approvals"))
    assert approvals


async def test_expired_lease_retries_without_consuming_backward_budget():
    now = [1000.]
    store, dispatcher = await setup_dispatcher(clock=lambda: now[0], lease_seconds=30)
    task = await seed(store, "pending", "EXECUTE", backward_count=2)
    claim = await dispatcher.handle(envelope("claim", agent="EXECUTE", worker="execute-01"))
    assert claim["accepted"]
    now[0] = 1100.
    await dispatcher.recover(PROJECT)
    recovered = (await dispatcher.tasks(PROJECT))[task.task_id]
    assert recovered.status == Status.TODO
    assert recovered.assigned_worker is None
    assert recovered.attempt == 1
    assert recovered.backward_count == 2


async def test_exhausted_technical_retry_is_failed_not_reopened():
    store, dispatcher = await setup_dispatcher(clock=lambda: 1000.)
    await active(store, "dead", "EXECUTE", attempt=3, max_attempts=3, lease_expires_at=900.)
    await dispatcher.recover(PROJECT)
    recovered = (await dispatcher.tasks(PROJECT))["dead"]
    assert recovered.status == Status.DONE
    assert recovered.outcome == Outcome.FAILED


async def test_finalize_rejects_unresolved_failed_history_without_partial_closure():
    store, dispatcher = await setup_dispatcher(clock=lambda: 1000.)
    await seed(store, "review-failed", "VERIFY_INTEGRATE", status=Status.DONE,
               outcome=Outcome.CHANGES_REQUESTED)
    source = await active(store, "final", "FINALIZE")
    output = proposal(source)
    output["source_result"].update(proposed_status="CLOSED", close_task_ids=["review-failed", "final"])
    receipt = await dispatcher.handle(envelope("result", source, output))
    assert not receipt["accepted"]
    tasks = await dispatcher.tasks(PROJECT)
    assert tasks["review-failed"].status == Status.DONE
    assert tasks["review-failed"].outcome == Outcome.CHANGES_REQUESTED
    assert tasks["final"].status == Status.DOING


async def test_finalize_closes_resolved_rework_but_preserves_failed_historical_outcome():
    store, dispatcher = await setup_dispatcher(clock=lambda: 1000.)
    await seed(store, "review-failed", "VERIFY_INTEGRATE", status=Status.DONE,
               outcome=Outcome.CHANGES_REQUESTED, replaced_by_task_id="replacement")
    await seed(store, "replacement", "VERIFY_INTEGRATE", status=Status.DONE, outcome=Outcome.SUCCESS,
               caused_by_task_id="review-failed", reason="Fix failing acceptance tests", integrated=True)
    source = await active(store, "final", "FINALIZE")
    output = proposal(source)
    output["source_result"].update(proposed_status="CLOSED",
                                  close_task_ids=["review-failed", "replacement", "final"])
    receipt = await dispatcher.handle(envelope("result", source, output))
    assert receipt["accepted"]
    tasks = await dispatcher.tasks(PROJECT)
    assert all(t.status == Status.CLOSED for t in tasks.values())
    assert tasks["review-failed"].outcome == Outcome.CHANGES_REQUESTED
    assert tasks["replacement"].outcome == Outcome.SUCCESS


@pytest.mark.parametrize("replacement_extra", [
    {"reason": None, "integrated": True},
    {"reason": "Unintegrated fix", "integrated": False},
    {"reason": "Unrelated work", "integrated": True, "caused_by_task_id": "another-task"},
])
async def test_unaccepted_or_unrelated_replacement_cannot_resolve_failed_history(replacement_extra):
    store, dispatcher = await setup_dispatcher(clock=lambda: 1000.)
    await seed(store, "review-failed", "VERIFY_INTEGRATE", status=Status.DONE,
               outcome=Outcome.CHANGES_REQUESTED)
    extras = {"caused_by_task_id": "review-failed", **replacement_extra}
    await seed(store, "replacement", "EXECUTE", status=Status.DONE, outcome=Outcome.SUCCESS, **extras)
    source = await active(store, "final", "FINALIZE")
    output = proposal(source)
    output["source_result"].update(proposed_status="CLOSED",
                                  close_task_ids=["review-failed", "replacement", "final"])
    assert not (await dispatcher.handle(envelope("result", source, output)))["accepted"]


async def test_pending_human_approval_prevents_chain_closure():
    store, dispatcher = await setup_dispatcher(clock=lambda: 1000.)
    await seed(store, "earlier", status=Status.DONE, outcome=Outcome.SUCCESS)
    source = await active(store, "final", "FINALIZE")
    await store.put(("coding", PROJECT, "approvals"), "approval", {
        "approval_id": "approval", "execution_chain_id": "chain", "status": "PENDING",
        "question": "Is the final behavior acceptable?"})
    output = proposal(source)
    output["source_result"].update(proposed_status="CLOSED", close_task_ids=["earlier", "final"])
    assert not (await dispatcher.handle(envelope("result", source, output)))["accepted"]
    assert (await dispatcher.tasks(PROJECT))["final"].status == Status.DOING


@pytest.mark.parametrize("commit", ["not-a-commit", "0" * 40])
async def test_execute_success_requires_real_commit_in_registered_repository(tmp_path, commit):
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(["git", "-C", str(repository), "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "initial"], check=True)
    store, dispatcher = await setup_dispatcher(clock=lambda: 1000.)
    await store.put(("coding", "projects"), PROJECT,
                    {"project_id": PROJECT, "repository_path": str(repository)})
    source = await active(store, "execution", "EXECUTE")
    output = proposal(source)
    output["source_result"]["commit_sha"] = commit
    receipt = await dispatcher.handle(envelope("result", source, output))
    assert not receipt["accepted"]
    assert (await dispatcher.tasks(PROJECT))["execution"].status == Status.DOING


async def test_execute_success_cannot_claim_commit_without_registered_repository():
    store, dispatcher = await setup_dispatcher(clock=lambda: 1000.)
    source = await active(store, "execution", "EXECUTE")
    output = proposal(source)
    output["source_result"]["commit_sha"] = "a" * 40
    assert not (await dispatcher.handle(envelope("result", source, output)))["accepted"]


async def test_execute_success_accepts_real_commit_and_preserves_reference(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(["git", "-C", str(repository), "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "initial"], check=True)
    commit = subprocess.check_output(["git", "-C", str(repository), "rev-parse", "HEAD"], text=True).strip()
    store, dispatcher = await setup_dispatcher(clock=lambda: 1000.)
    await store.put(("coding", "projects"), PROJECT,
                    {"project_id": PROJECT, "repository_path": str(repository)})
    source = await active(store, "execution", "EXECUTE", worktree_path=str(repository))
    output = proposal(source)
    output["source_result"]["commit_sha"] = commit
    receipt = await dispatcher.handle(envelope("result", source, output))
    assert receipt["accepted"]
    assert (await dispatcher.tasks(PROJECT))["execution"].commit_sha == commit


async def test_explicit_rework_links_failed_task_without_reopening_it():
    store, dispatcher = await setup_dispatcher(clock=lambda: 1000.)
    await seed(store, "failed-execute", "EXECUTE", status=Status.DONE, outcome=Outcome.FAILED)
    source = await active(store, "review", "VERIFY_INTEGRATE")
    output = proposal(source, outcome="CHANGES_REQUESTED", nodes=[node("redo", "EXECUTE",
        replaces_task_id="failed-execute", reason="Fix the failed task")])
    receipt = await dispatcher.handle(envelope("result", source, output))
    assert receipt["accepted"]
    tasks = await dispatcher.tasks(PROJECT)
    original = tasks["failed-execute"]
    assert original.status == Status.DONE and original.outcome == Outcome.FAILED
    assert original.replaced_by_task_id == receipt["task_ids"][0]
    assert tasks[original.replaced_by_task_id].status == Status.TODO


@pytest.mark.parametrize("old_overrides,node_agent", [
    ({"status": Status.TODO, "outcome": None}, "EXECUTE"),
    ({"status": Status.DOING, "outcome": None}, "EXECUTE"),
    ({"status": Status.CLOSED, "outcome": Outcome.FAILED}, "EXECUTE"),
    ({"status": Status.DONE, "outcome": Outcome.SUCCESS}, "EXECUTE"),
    ({"status": Status.DONE, "outcome": Outcome.FAILED}, "PLAN_DELEGATE"),
    ({"status": Status.DONE, "outcome": Outcome.FAILED, "execution_chain_id": "another-chain"}, "EXECUTE"),
    ({"status": Status.DONE, "outcome": Outcome.FAILED, "replaced_by_task_id": "pending-replacement"}, "EXECUTE"),
])
async def test_invalid_replacement_cannot_change_original_task(old_overrides, node_agent):
    store, dispatcher = await setup_dispatcher(clock=lambda: 1000.)
    original = await seed(store, "failed-execute", "EXECUTE", **old_overrides)
    source = await active(store, "review", "VERIFY_INTEGRATE")
    output = proposal(source, outcome="CHANGES_REQUESTED", nodes=[node("redo", node_agent,
        replaces_task_id="failed-execute", reason="Fix the failed task")])
    receipt = await dispatcher.handle(envelope("result", source, output))
    assert not receipt["accepted"]
    tasks = await dispatcher.tasks(PROJECT)
    assert len(tasks) == 2
    assert tasks["failed-execute"].model_dump() == original.model_dump()
    assert tasks["review"].status == Status.DOING


async def test_replacement_requires_existing_target_and_explicit_reason():
    store, dispatcher = await setup_dispatcher(clock=lambda: 1000.)
    source = await active(store, "review", "VERIFY_INTEGRATE")
    for target, reason in [("missing", "Fix"), ("review", None)]:
        output = proposal(source, outcome="CHANGES_REQUESTED", nodes=[node("redo", "EXECUTE",
            replaces_task_id=target, reason=reason)])
        assert not (await dispatcher.handle(envelope("result", source, output)))["accepted"]


async def test_replacement_of_foreign_project_task_is_rejected():
    store, dispatcher = await setup_dispatcher(clock=lambda: 1000.)
    foreign = Task(task_id="foreign", project_id="foreign-project", plan_id="plan", title="foreign",
                   agent="EXECUTE", status=Status.DONE, outcome=Outcome.FAILED,
                   execution_chain_id="chain", root_task_id="root")
    await store.put(("coding", "foreign-project", "tasks"), foreign.task_id,
                    foreign.model_dump(mode="json"))
    source = await active(store, "review", "VERIFY_INTEGRATE")
    output = proposal(source, outcome="CHANGES_REQUESTED", nodes=[node("redo", "EXECUTE",
        replaces_task_id="foreign", reason="Fix foreign task")])
    assert not (await dispatcher.handle(envelope("result", source, output)))["accepted"]


async def test_two_proposed_nodes_cannot_replace_same_failed_task():
    store, dispatcher = await setup_dispatcher(clock=lambda: 1000.)
    await seed(store, "failed-execute", "EXECUTE", status=Status.DONE, outcome=Outcome.FAILED)
    source = await active(store, "review", "VERIFY_INTEGRATE")
    output = proposal(source, outcome="CHANGES_REQUESTED", nodes=[
        node("redo-a", "EXECUTE", replaces_task_id="failed-execute", reason="Fix part A"),
        node("redo-b", "EXECUTE", replaces_task_id="failed-execute", reason="Fix part B"),
    ])
    assert not (await dispatcher.handle(envelope("result", source, output)))["accepted"]


async def test_finalize_closes_multihop_rework_history_only_after_final_replacement_accepted():
    store, dispatcher = await setup_dispatcher(clock=lambda: 1000.)
    await seed(store, "original", "EXECUTE", status=Status.DONE, outcome=Outcome.FAILED,
               replaced_by_task_id="redo-1")
    await seed(store, "redo-1", "EXECUTE", status=Status.DONE, outcome=Outcome.CHANGES_REQUESTED,
               replaced_by_task_id="redo-2", reason="First attempted repair")
    await seed(store, "redo-2", "EXECUTE", status=Status.DONE, outcome=Outcome.SUCCESS,
               reason="Second repair passes all acceptance tests", integrated=True)
    source = await active(store, "final", "FINALIZE")
    output = proposal(source)
    output["source_result"].update(proposed_status="CLOSED",
                                  close_task_ids=["original", "redo-1", "redo-2", "final"])
    assert (await dispatcher.handle(envelope("result", source, output)))["accepted"]
    tasks = await dispatcher.tasks(PROJECT)
    assert all(t.status == Status.CLOSED for t in tasks.values())
    assert tasks["original"].outcome == Outcome.FAILED
    assert tasks["redo-1"].outcome == Outcome.CHANGES_REQUESTED


async def test_finalize_rejects_cyclic_rework_history():
    store, dispatcher = await setup_dispatcher(clock=lambda: 1000.)
    await seed(store, "original", "EXECUTE", status=Status.DONE, outcome=Outcome.FAILED,
               replaced_by_task_id="redo")
    await seed(store, "redo", "EXECUTE", status=Status.DONE, outcome=Outcome.FAILED,
               reason="Repair", replaced_by_task_id="original")
    source = await active(store, "final", "FINALIZE")
    output = proposal(source)
    output["source_result"].update(proposed_status="CLOSED", close_task_ids=["original", "redo", "final"])
    assert not (await dispatcher.handle(envelope("result", source, output)))["accepted"]


@pytest.mark.parametrize("predecessor", ["original", "downstream"])
async def test_replacement_cannot_create_cycle_through_existing_dependency_alias(predecessor):
    store, dispatcher = await setup_dispatcher(clock=lambda: 1000.)
    await seed(store, "original", "EXECUTE", status=Status.DONE, outcome=Outcome.FAILED)
    await seed(store, "downstream", "VERIFY_INTEGRATE", depends_on=["original"])
    source = await active(store, "review", "VERIFY_INTEGRATE")
    output = proposal(source, outcome="CHANGES_REQUESTED", nodes=[node("redo", "EXECUTE",
        replaces_task_id="original", reason="Repair original")],
        edges=[{"from": predecessor, "to": "redo"}])
    receipt = await dispatcher.handle(envelope("result", source, output))
    assert not receipt["accepted"]
    assert (await dispatcher.tasks(PROJECT))["original"].replaced_by_task_id is None


@pytest.mark.parametrize("target", ["ANALYZE", "EXECUTE", "VERIFY_INTEGRATE", "FINALIZE"])
async def test_analyze_cannot_bypass_route_policy_or_partially_create_plan(target):
    store, dispatcher = await setup_dispatcher(clock=lambda: 1000.)
    source = await active(store)
    output = proposal(source, nodes=[node("valid", "PLAN_DELEGATE"), node("invalid", target)])
    receipt = await dispatcher.handle(envelope("result", source, output))
    assert not receipt["accepted"] and "only to COMMUNICATOR or PLAN_DELEGATE" in receipt["reason"]
    tasks = await dispatcher.tasks(PROJECT)
    assert list(tasks) == [source.task_id]
    assert tasks[source.task_id].status == Status.DOING
    assert await store.search(("coding", PROJECT, "plans")) == []


@pytest.mark.parametrize("target", ["COMMUNICATOR", "PLAN_DELEGATE"])
async def test_analyze_can_delegate_to_each_allowed_role(target):
    store, dispatcher = await setup_dispatcher(clock=lambda: 1000.)
    source = await active(store)
    output = proposal(source, nodes=[node("next", target, reason="Clarify scope" if target == "COMMUNICATOR" else None)],
                      outcome="NEED_ANALYSIS" if target == "COMMUNICATOR" else "SUCCESS")
    receipt = await dispatcher.handle(envelope("result", source, output))
    assert receipt["accepted"]
    tasks = await dispatcher.tasks(PROJECT)
    assert tasks[receipt["task_ids"][0]].agent == target


async def test_gitlab_token_cannot_be_promoted_and_is_redacted_in_artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-test-secret-value")
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    store, dispatcher = await setup_dispatcher(clock=lambda: 1000.)
    source = await active(store)
    output = proposal(source, shared_updates=[{"key": "gitlab-report", "value": "glpat-test-secret-value", "reason": "Share findings"}])
    assert not (await dispatcher.handle(envelope("result", source, output)))["accepted"]
    artifact_dir = tmp_path / "artifacts" / PROJECT / "reports"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "analysis.txt").write_text("glpat-test-secret-value")
    output = proposal(source)
    output["source_result"]["artifact_refs"] = [f"artifact://{PROJECT}/reports/analysis.txt"]
    assert (await dispatcher.handle(envelope("result", source, output)))["accepted"]
    artifact = await store.get(("coding", PROJECT, "shared", "artifacts"), f"{PROJECT}/reports/analysis.txt")
    assert artifact["content"] == "[REDACTED]"
