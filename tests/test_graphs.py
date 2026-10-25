from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from orchestrator.graphs import common
from orchestrator.graphs.communicator import communicator_output, graph as communicator
from orchestrator.schemas.task_graph import TaskGraphOutput
from orchestrator.workers.graph_runner import run_graph
from orchestrator.workers.polling import ReceiptTimeout, submit_request
from orchestrator.workers.main import Worker
from orchestrator.store import InMemoryStore


def test_communicator_protocol_requires_no_qwen(monkeypatch):
    runner = Mock(side_effect=AssertionError("Communicator must never use Qwen"))
    monkeypatch.setattr(common, "QwenRunner", runner)
    output = communicator_output("demo", "Implement an API", "chain-1")
    checked = TaskGraphOutput.model_validate(output)
    assert checked.producer_agent == "COMMUNICATOR"
    assert checked.source_result is None
    assert checked.nodes[0].target_agent == "ANALYZE"
    runner.assert_not_called()


@pytest.mark.asyncio
async def test_communicator_graph_is_deterministic_and_single_role(monkeypatch):
    monkeypatch.setattr(common, "QwenRunner", Mock(side_effect=AssertionError("No model")))
    output = await communicator.ainvoke({
        "task": {"project_id": "demo", "execution_chain_id": "chain-1"},
        "context": {"message": "Add health check"},
    })
    assert output["result"]["nodes"][0]["description"] == "Add health check"
    assert set(communicator.get_graph().nodes) == {"__start__", "communicate", "__end__"}


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", ["ANALYZE", "PLAN_DELEGATE", "EXECUTE", "VERIFY_INTEGRATE", "FINALIZE"])
async def test_each_model_graph_calls_two_pass_runtime_and_emits_one_protocol(monkeypatch, tmp_path, agent):
    expected = {
        "schema_version": "1.0", "project_id": "demo", "source_task_id": "task-1",
        "execution_chain_id": "chain-1", "producer_agent": agent,
        "source_result": {"outcome": "SUCCESS", "proposed_status": "DONE"},
        "nodes": [], "edges": [],
    }
    runtime = SimpleNamespace(run=AsyncMock(return_value=expected))
    monkeypatch.setattr(common, "QwenRunner", lambda: runtime)
    graph = common.build_agent_graph(agent)
    task = {"task_id": "task-1", "agent": agent}
    state = await graph.ainvoke({"task": task, "context": {"shared": []}, "cwd": str(tmp_path)})
    TaskGraphOutput.model_validate(state["result"])
    runtime.run.assert_awaited_once()
    assert runtime.run.call_args.kwargs["task"] == task
    assert set(graph.get_graph().nodes) == {"__start__", "work", "__end__"}


@pytest.mark.asyncio
async def test_graph_rejects_another_role_before_model_call(monkeypatch, tmp_path):
    runner = Mock(side_effect=AssertionError("Must reject before Qwen"))
    monkeypatch.setattr(common, "QwenRunner", runner)
    graph = common.build_agent_graph("EXECUTE")
    with pytest.raises(ValueError, match="another agent"):
        await graph.ainvoke({"task": {"agent": "FINALIZE"}, "cwd": str(tmp_path)})
    runner.assert_not_called()


@pytest.mark.asyncio
async def test_sdk_stream_requires_final_graph_output():
    async def stream(*args, **kwargs):
        yield SimpleNamespace(event="values", data={"task": {"task_id": "task-1"}})
        yield SimpleNamespace(event="custom", data={"type": "qwen_progress", "chunks": 1})
        yield SimpleNamespace(event="values", data={"result": {"nodes": []}})

    progress = AsyncMock()
    client = SimpleNamespace(runs=SimpleNamespace(stream=stream))
    result = await run_graph(client, "EXECUTE", "thread-1", {}, {}, "/tmp", progress)
    assert result == {"nodes": []}
    progress.assert_awaited_once()


@pytest.mark.asyncio
async def test_worker_waits_for_dispatcher_receipt_and_never_writes_task_state():
    store = InMemoryStore()
    await store.put(("coding", "demo", "responses", "EXECUTE"), "request-1", {"accepted": True, "task": {"task_id": "task-1"}})
    receipt = await submit_request(store, "demo", "EXECUTE", "execute-01", "claim", request_id="request-1")
    assert receipt["accepted"]
    assert await store.get(("coding", "demo", "tasks"), "task-1") is None
    request = await store.get(("coding", "demo", "requests", "EXECUTE"), "request-1")
    assert request["kind"] == "claim"
    assert request["worker_id"] == "execute-01"


@pytest.mark.asyncio
async def test_request_timeout_does_not_issue_another_claim():
    store = InMemoryStore()
    with pytest.raises(ReceiptTimeout):
        await submit_request(store, "demo", "EXECUTE", "execute-01", "claim", timeout=0.01, request_id="pending")
    assert len(await store.search(("coding", "demo", "requests", "EXECUTE"))) == 1


def test_worker_constructs_real_sdk_client_with_auth(monkeypatch, tmp_path):
    # The SDK reserves x-api-key in headers; api_key= is the supported route.
    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "unit-test-internal-token")
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data"))
    worker = Worker("ANALYZE", "analyze-01")
    assert worker.client is not None
    assert worker.store.client is not None


@pytest.mark.asyncio
async def test_repository_free_analysis_gets_isolated_session_and_full_chain_context(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    store = InMemoryStore()
    current = {"task_id": "analyze-1", "project_id": "demo", "agent": "ANALYZE", "execution_chain_id": "chain-1"}
    earlier = {"task_id": "prior-1", "project_id": "demo", "agent": "ANALYZE", "execution_chain_id": "chain-1", "status": "DONE"}
    other = {"task_id": "other-1", "project_id": "demo", "execution_chain_id": "chain-2"}
    for task in (current, earlier, other):
        await store.put(("coding", "demo", "tasks"), task["task_id"], task)
    client = SimpleNamespace(threads=SimpleNamespace(create=AsyncMock(return_value={"thread_id": "thread-1"})))
    worker = Worker("ANALYZE", "analyze-01", store=store, client=client)
    worker.request = AsyncMock(return_value={"accepted": True})
    runner = AsyncMock(return_value={"source_result": {"outcome": "SUCCESS"}})
    monkeypatch.setattr("orchestrator.workers.main.run_graph", runner)
    await worker.execute({"project_id": "demo", "repository_path": None}, current)
    context = runner.call_args.args[4]
    assert {task["task_id"] for task in context["chain_tasks"]} == {"analyze-1", "prior-1"}
    assert (tmp_path / "sessions" / "demo" / "analyze-1").is_dir()
    assert current["worktree_path"] == str(tmp_path / "sessions" / "demo" / "analyze-1")


@pytest.mark.asyncio
async def test_verify_integrates_transitive_execute_commits_only():
    store = InMemoryStore()
    execute = {"task_id": "execute-1", "agent": "EXECUTE", "depends_on": [], "commit_sha": "a" * 40}
    verified = {"task_id": "verify-1", "agent": "VERIFY_INTEGRATE", "depends_on": ["execute-1"], "commit_sha": "b" * 40}
    current = {"task_id": "verify-2", "project_id": "demo", "depends_on": ["verify-1"]}
    for task in (execute, verified):
        await store.put(("coding", "demo", "tasks"), task["task_id"], task)
    manager = SimpleNamespace(
        register=Mock(),
        integrate=Mock(return_value={"status": "SUCCESS", "worktree_path": "/integration", "base_commit": "c" * 40}),
    )
    worker = Worker("VERIFY_INTEGRATE", "verify_integrate-01", store=store, client=Mock(), workspace=manager)
    _, dependencies = await worker.prepare({"project_id": "demo", "repository_path": "/repo"}, current)
    manager.integrate.assert_called_once_with("demo", ["a" * 40])
    assert [task["task_id"] for task in dependencies] == ["execute-1", "verify-1"]


@pytest.mark.asyncio
async def test_graph_streams_progress_without_private_log_text(monkeypatch, tmp_path):
    async def run(**kwargs):
        await kwargs["on_log"]({"phase": "work", "line": "PRIVATE_SCRATCHPAD"})
        return {"nodes": []}

    monkeypatch.setattr(common, "QwenRunner", lambda: SimpleNamespace(run=run))
    graph = common.build_agent_graph("ANALYZE")
    events = [event async for event in graph.astream({"task": {"agent": "ANALYZE"}, "cwd": str(tmp_path)}, stream_mode="custom")]
    assert events[0]["chunks"] == 1
    assert events[0]["phase"] == "work"
    assert "PRIVATE_SCRATCHPAD" not in str(events)
