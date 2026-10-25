import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from orchestrator.graphs import common
from orchestrator.graphs import communicator as communicator_module
from orchestrator.graphs.communicator import graph as communicator
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, messages_to_dict
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.runtime import Runtime
from orchestrator.schemas.task_graph import TaskGraphOutput
from orchestrator.workers.graph_runner import run_graph
from orchestrator.workers.main import Worker
from orchestrator.dispatcher.context import build_job
from queue import Queue
from orchestrator.store import InMemoryStore


@pytest.mark.parametrize("human_node", [False, True])
async def test_communicator_task_calls_chat_llm_with_typed_messages(monkeypatch, human_node):
    expected = {
        "project_id": "demo", "source_task_id": "task-1", "producer_agent": "COMMUNICATOR",
        "execution_chain_id": "chain-1", "source_result": {"outcome": "SUCCESS"},
        "summary": "Clarify the audience", "human_request": {"question": "Who will use it?"},
    }
    if human_node:
        expected.pop("human_request")
        expected["nodes"] = [{"id": "ask", "node_type": "HUMAN_APPROVAL", "target_agent": "COMMUNICATOR",
                              "title": "Who will use it?"}]
    runtime = SimpleNamespace(chat=AsyncMock(return_value=AIMessage(content="Who will use it?")),
                              ainvoke=AsyncMock(return_value=expected))
    monkeypatch.setattr(communicator_module, "DecisionRunner", lambda: runtime)
    monkeypatch.setattr(common, "QwenRunner", Mock(side_effect=AssertionError("No Qwen Code for Communicator")))
    output = await communicator.ainvoke({
        "task": {"project_id": "demo", "task_id": "task-1", "agent": "COMMUNICATOR",
                 "execution_chain_id": "chain-1"},
        "context": {"delegation": {"backward_count": 0, "max_backward_delegations": 3}},
        "messages": [HumanMessage(content="Build a portal", id="human-1")],
    })
    assert isinstance(output["messages"][0], HumanMessage)
    assert len(output["messages"]) == 2
    assert isinstance(output["messages"][-1], AIMessage)
    assert output["messages"][-1].content == "Who will use it?"
    runtime.ainvoke.assert_awaited_once()
    runtime.chat.assert_awaited_once()
    messages, schema = runtime.ainvoke.call_args.args
    assert isinstance(messages[0], SystemMessage)
    assert any(isinstance(message, HumanMessage) and message.content == "Build a portal" for message in messages)
    assert messages[-2].content == "Who will use it?"
    assert "Structure pass" in messages[-1].content
    assert schema["title"] == "TaskGraphOutput"
    TaskGraphOutput.model_validate(output["result"])


async def test_communicate_rejects_wrong_role_before_llm(monkeypatch):
    runner = Mock(side_effect=AssertionError("Must validate role first"))
    monkeypatch.setattr(communicator_module, "DecisionRunner", runner)
    with pytest.raises(ValueError, match="another agent"):
        await communicator_module.communicate({"task": {"agent": "EXECUTE"}}, Runtime())
    runner.assert_not_called()


async def test_communicator_accepts_messages_without_task_or_cwd(monkeypatch):
    from langgraph.store.memory import InMemoryStore as NativeStore
    store = NativeStore()
    await store.aput(("coding", "projects"), "portal", {"project_id": "portal", "title": "Portal"})
    runner = Mock(side_effect=AssertionError("Project menu needs no LLM"))
    monkeypatch.setattr(communicator_module, "DecisionRunner", runner)
    monkeypatch.setattr(common, "QwenRunner", Mock(side_effect=AssertionError("No Qwen Code for chat")))
    graph = communicator_module.builder.compile(store=store)
    chunks = [chunk async for chunk in graph.astream(
        {"messages": [{"role": "user", "content": "Xin chào"}]},
        stream_mode=["custom", "values"], subgraphs=True)]
    replies = [(namespace, data) for namespace, mode, data in chunks if mode == "custom"]
    assert replies[0][0]  # The reply originates inside the communicate subgraph.
    assert "Portal" in replies[0][1]["content"]
    assert "tạo project mới" in replies[0][1]["content"]
    output = [data for namespace, mode, data in chunks if mode == "values" and not namespace][-1]
    assert len(output["messages"]) == 2 and "result" not in output
    assert not output.get("project_id")
    runner.assert_not_called()


async def test_communicator_reads_previous_turns_in_the_same_thread(monkeypatch):
    from langgraph.store.memory import InMemoryStore as NativeStore
    store = NativeStore()
    await store.aput(("coding", "projects"), "portal", {"project_id": "portal", "title": "Portal"})
    runner = Mock()
    monkeypatch.setattr(communicator_module, "DecisionRunner", lambda: runner)
    follow = AsyncMock(side_effect=[
        {"messages": [AIMessage(content="Ai sẽ dùng portal?", id="ai-1")]},
        {"messages": [AIMessage(content="Đã ghi nhận khách hàng.", id="ai-2")]},
    ])
    monkeypatch.setattr(communicator_module, "follow_progress", follow)
    graph = communicator_module.builder.compile(checkpointer=InMemorySaver(), store=store)
    config = {"configurable": {"thread_id": "conversation-1"}}
    await graph.ainvoke({"messages": [("user", "Xin chào")]}, config)
    selected = await graph.ainvoke({"messages": [("user", "1")]}, config)
    assert selected["project_id"] == "portal"
    follow.assert_not_awaited()
    await graph.ainvoke({"messages": [("user", "Tạo portal")]}, config)
    output = await graph.ainvoke({"messages": [("user", "Cho khách hàng")]}, config)
    history = follow.call_args.args[2]
    assert [message.content for message in history[-3:]] == [
        "Tạo portal", "Ai sẽ dùng portal?", "Cho khách hàng"]
    assert len(output["messages"]) == 8
    assert output["project_id"] == "portal"


async def test_communicator_requires_a_user_message_before_calling_llm(monkeypatch):
    runner = Mock(side_effect=AssertionError("No LLM for an empty conversation"))
    monkeypatch.setattr(communicator_module, "DecisionRunner", runner)
    with pytest.raises(ValueError, match="user message"):
        await communicator.ainvoke({"messages": []})
    runner.assert_not_called()


async def test_communicator_rejects_spoofed_task_identity(monkeypatch):
    runtime = SimpleNamespace(chat=AsyncMock(return_value=AIMessage(content="Requirements ready")), ainvoke=AsyncMock(return_value={
        "project_id": "demo", "source_task_id": "other-task", "producer_agent": "COMMUNICATOR",
        "execution_chain_id": "chain-1", "source_result": {"outcome": "SUCCESS"},
    }))
    monkeypatch.setattr(communicator_module, "DecisionRunner", lambda: runtime)
    with pytest.raises(ValueError, match="identity"):
        await communicator.ainvoke({
            "task": {"project_id": "demo", "task_id": "task-1", "agent": "COMMUNICATOR",
                     "execution_chain_id": "chain-1"},
            "messages": [("user", "Build a portal")],
        })


def test_only_communicator_is_exposed():
    config = json.loads((Path(__file__).resolve().parents[1] / "langgraph.json").read_text())
    assert set(config["graphs"]) == {"communicator"}


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
    task = {"task_id": "task-1", "agent": agent}
    monkeypatch.setattr(communicator_module, "DecisionRunner", Mock(side_effect=AssertionError("Worker must run its own role")))
    state = await common.build_agent_graph(agent).ainvoke({"task": task, "context": {"shared": []}, "cwd": str(tmp_path)})
    TaskGraphOutput.model_validate(state["result"])
    runtime.run.assert_awaited_once()
    assert runtime.run.call_args.kwargs["task"] == task


@pytest.mark.asyncio
async def test_graph_rejects_another_role_before_model_call(monkeypatch, tmp_path):
    runner = Mock(side_effect=AssertionError("Must reject before Qwen"))
    monkeypatch.setattr(common, "QwenRunner", runner)
    graph = common.build_agent_graph("EXECUTE")
    with pytest.raises(ValueError, match="another agent"):
        await graph.ainvoke({"task": {"agent": "FINALIZE"}, "cwd": str(tmp_path)})
    runner.assert_not_called()


async def test_worker_cannot_run_a_task_assigned_to_another_role():
    with pytest.raises(ValueError, match="worker role"):
        await run_graph("ANALYZE", {"agent": "EXECUTE"}, {}, "/tmp")


def test_worker_has_no_store_or_sdk_client(tmp_path):
    worker = Worker("ANALYZE", "analyze-01", Queue(), data_root=tmp_path, workspace=Mock())
    assert not hasattr(worker, "store")
    assert not hasattr(worker, "client")


async def test_dispatcher_builds_full_chain_context_and_worker_uses_snapshot(monkeypatch, tmp_path):
    store = InMemoryStore()
    current = {"task_id": "analyze-1", "project_id": "demo", "agent": "ANALYZE",
               "execution_chain_id": "chain-1", "attempt": 1}
    earlier = {"task_id": "prior-1", "project_id": "demo", "agent": "ANALYZE",
               "execution_chain_id": "chain-1", "status": "DONE"}
    other = {"task_id": "other-1", "project_id": "demo", "execution_chain_id": "chain-2"}
    for task in (current, earlier, other):
        await store.put(("coding", "demo", "tasks"), task["task_id"], task)
    job = await build_job(store, {"project_id": "demo"}, current, "analyze-01", 3)
    runner = AsyncMock(return_value={"source_result": {"outcome": "SUCCESS"}})
    monkeypatch.setattr("orchestrator.workers.main.run_graph", runner)
    worker = Worker("ANALYZE", "analyze-01", Queue(), data_root=tmp_path, workspace=Mock())
    await worker.execute(job)
    context = runner.call_args.args[2]
    assert {task["task_id"] for task in context["chain_tasks"]} == {"analyze-1", "prior-1"}
    assert (tmp_path / "sessions" / "demo" / "analyze-1").is_dir()


async def test_verify_integrates_transitive_execute_commits_from_dispatcher_snapshot():
    store = InMemoryStore()
    execute = {"task_id": "execute-1", "agent": "EXECUTE", "depends_on": [], "commit_sha": "a" * 40}
    verified = {"task_id": "verify-1", "agent": "VERIFY_INTEGRATE", "depends_on": ["execute-1"], "commit_sha": "b" * 40}
    current = {"task_id": "verify-2", "project_id": "demo", "agent": "VERIFY_INTEGRATE",
               "execution_chain_id": "chain", "depends_on": ["verify-1"]}
    for task in (execute, verified, current):
        await store.put(("coding", "demo", "tasks"), task["task_id"], task)
    job = await build_job(store, {"project_id": "demo", "repository_path": "/repo"}, current, "verify-01", 3)
    manager = SimpleNamespace(
        register=Mock(),
        integrate=Mock(return_value={"status": "SUCCESS", "worktree_path": "/integration", "base_commit": "c" * 40}),
    )
    worker = Worker("VERIFY_INTEGRATE", "verify-01", Queue(), workspace=manager)
    await worker.prepare(job)
    manager.integrate.assert_called_once_with("demo", ["a" * 40])
    assert [task["task_id"] for task in job["predecessors"]] == ["execute-1", "verify-1"]


@pytest.mark.asyncio
async def test_graph_streams_progress_without_private_log_text(monkeypatch, tmp_path):
    async def run(**kwargs):
        await kwargs["on_log"]({"phase": "work", "line": "PRIVATE_SCRATCHPAD"})
        return {"nodes": []}

    monkeypatch.setattr(common, "QwenRunner", lambda: SimpleNamespace(run=run))
    events = []
    await run_graph("ANALYZE", {"agent": "ANALYZE"}, {}, str(tmp_path), events.append)
    assert events[0]["chunks"] == 1
    assert events[0]["phase"] == "work"
    assert "PRIVATE_SCRATCHPAD" not in str(events)
