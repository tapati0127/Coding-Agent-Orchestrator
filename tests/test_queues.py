"""Python queue handoffs; only Dispatcher sees a Store."""

import asyncio
import json
import logging
import multiprocessing as mp
from queue import Empty, Queue
import time
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from orchestrator.dispatcher.engine import Dispatcher
from orchestrator.dispatcher.process import DispatcherLoop
from orchestrator.queues import DispatcherClient
from orchestrator.runtime.decision import DecisionRunner
from orchestrator.runtime.qwen import QwenRunner
from orchestrator.schemas.task import Task
from orchestrator.schemas.task_graph import Agent, Status
from orchestrator.store import InMemoryStore
from orchestrator.workers.main import Worker


async def setup(roles=None, **options):
    store = InMemoryStore()
    await store.put(("coding", "projects"), "p1", {"project_id": "p1"})
    engine = Dispatcher(store, clock=lambda: 1000., **options)
    roles = roles or {"analyze": "ANALYZE"}
    jobs = {worker_id: Queue() for worker_id in roles}
    loop = DispatcherLoop(engine, Queue(), jobs, roles)
    return store, engine, jobs, loop


async def seed(store, task_id, agent="ANALYZE", **fields):
    task = Task(task_id=task_id, project_id="p1", plan_id="plan", agent=agent,
                title=task_id, root_task_id="root", execution_chain_id="chain", **fields)
    await store.put(("coding", "p1", "tasks"), task_id, task.model_dump(mode="json"))
    return task


def proposal(task, **fields):
    return {"project_id": "p1", "source_task_id": task.task_id, "producer_agent": task.agent.value,
            "execution_chain_id": task.execution_chain_id, "source_result": {"outcome": "SUCCESS"}, **fields}


def result_event(task, payload, worker="worker"):
    return {"request_id": uuid4().hex, "project_id": "p1", "agent": task.agent.value,
            "worker_id": worker, "task_id": task.task_id, "task_attempt": task.attempt,
            "kind": "result", "payload": payload}


async def test_rejected_proposal_never_reaches_a_worker_queue():
    store, engine, jobs, loop = await setup({"execute": "EXECUTE"})
    source = await seed(store, "source", status="DOING", assigned_worker="worker", lease_expires_at=2000.)
    await loop.handle(result_event(source, proposal(source, nodes=[
        {"id": "invalid", "target_agent": "EXECUTE", "title": "Skip planning"}])))
    await loop.schedule()
    with pytest.raises(Empty):
        jobs["execute"].get_nowait()
    assert (await engine.tasks("p1"))["source"].status == Status.DOING


async def test_dispatcher_claims_once_before_publishing_a_snapshot():
    store, engine, jobs, loop = await setup({"one": "EXECUTE", "two": "EXECUTE"})
    await seed(store, "task", "EXECUTE")
    await loop.schedule()
    job = jobs["one"].get_nowait()
    assert job["task"]["status"] == "DOING"
    assert job["task"]["assigned_worker"] == "one"
    assert "context" in job and "predecessors" in job
    with pytest.raises(Empty):
        jobs["two"].get_nowait()
    count = len(await store.search(("coding", "p1", "operations")))
    await loop.schedule()
    assert len(await store.search(("coding", "p1", "operations"))) == count
    assert (await engine.tasks("p1"))["task"].attempt == 1
    assert not any(ns[2] in {"ready", "requests", "proposals"} for ns, _ in store.records if len(ns) > 2)


async def test_dependencies_must_all_succeed_before_publishing():
    store, engine, jobs, loop = await setup({"plan": "PLAN_DELEGATE"})
    await seed(store, "first", status="DONE", outcome="SUCCESS")
    second = await seed(store, "second", status="DOING", assigned_worker="worker", lease_expires_at=2000.)
    await seed(store, "join", "PLAN_DELEGATE", depends_on=["first", "second"])
    await loop.schedule()
    with pytest.raises(Empty):
        jobs["plan"].get_nowait()
    await loop.handle(result_event(second, proposal(second)))
    await loop.schedule()
    assert jobs["plan"].get_nowait()["task"]["task_id"] == "join"


async def test_restart_recovers_expired_assignment_and_rejects_old_attempt():
    now = [1000.]
    store, engine, jobs, loop = await setup(lease_seconds=10)
    engine.clock = lambda: now[0]
    await seed(store, "task")
    await loop.schedule()
    old = jobs["analyze"].get_nowait()
    now[0] = 1011.
    await loop.recover()
    await loop.schedule()
    new = jobs["analyze"].get_nowait()
    assert new["task"]["attempt"] == 2
    late = result_event(Task.model_validate(old["task"]), proposal(Task.model_validate(old["task"])), worker="analyze")
    receipt = await engine.handle(late)
    assert not receipt["accepted"] and "attempt" in receipt["reason"]
    assert (await engine.tasks("p1"))["task"].status == Status.DOING


@pytest.mark.parametrize("busy,pending", [("EXECUTE", "COMMUNICATOR"), ("COMMUNICATOR", "ANALYZE")])
async def test_communicator_does_not_consume_technical_capacity(busy, pending):
    store, engine, jobs, loop = await setup({"next": pending}, max_qwen_sessions=1)
    await seed(store, "busy", busy, status="DOING", assigned_worker="busy", lease_expires_at=2000.)
    await seed(store, "pending", pending)
    await loop.schedule()
    assert jobs["next"].get_nowait()["task"]["task_id"] == "pending"


async def test_user_request_through_communicator_and_technical_worker_without_store_clients(tmp_path, monkeypatch, caplog):
    store, engine, jobs, loop = await setup({"chat": "COMMUNICATOR", "analyze": "ANALYZE", "plan": "PLAN_DELEGATE"})
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("QWEN_BASE_URL", "http://fixture.internal/v1")
    monkeypatch.setenv("QWEN_API_KEY", "fixture-key")
    monkeypatch.setenv("QWEN_MODEL", "fixture")
    for key in ("DECISION_BASE_URL", "DECISION_API_KEY", "DECISION_MODEL", "WORK_PASS_SKILLS_CONFIG"):
        monkeypatch.delenv(key, raising=False)
    caplog.set_level(logging.INFO)
    work_roles, model_roles = [], []

    async def work(self, *args, **kwargs):
        work_roles.append("ANALYZE")
        return {"result": "Analysis evidence"}

    async def decide(self, prompt, schema):
        if isinstance(prompt, list):
            data = json.loads(prompt[1].content.split("\n", 1)[1])
            assert any(isinstance(message, HumanMessage) and message.content == "PRIVATE_USER_REQUIREMENT"
                       for message in prompt)
        else:
            data = next(json.loads(line) for line in prompt.splitlines() if line.startswith('{"task":'))
        task = Task.model_validate(data["task"])
        model_roles.append(task.agent.value)
        target = "ANALYZE" if task.agent == Agent.COMMUNICATOR else "PLAN_DELEGATE"
        return proposal(task, summary="Requirements accepted", nodes=[
            {"id": "next", "target_agent": target, "title": "Process requirements brief"}])

    monkeypatch.setattr(QwenRunner, "_invoke", work)
    conversation = AsyncMock(return_value=AIMessage(content="Requirements understood"))
    monkeypatch.setattr(DecisionRunner, "chat", conversation)
    monkeypatch.setattr(DecisionRunner, "ainvoke", decide)
    monkeypatch.setattr("langgraph_sdk.get_client", Mock(side_effect=AssertionError("Agents must not open SDK clients")))
    await loop.handle({"request_id": uuid4().hex, "project_id": "p1", "agent": "COMMUNICATOR",
                       "kind": "human_message", "payload": {"message": "PRIVATE_USER_REQUIREMENT"}})
    await loop.schedule()
    for worker_id, agent in (("chat", "COMMUNICATOR"), ("analyze", "ANALYZE")):
        worker = Worker(agent, worker_id, loop.events, workspace=Mock(), data_root=tmp_path)
        assert not hasattr(worker, "store") and not hasattr(worker, "client")
        await worker.execute(jobs[worker_id].get_nowait())
        while True:
            try:
                await loop.handle(loop.events.get_nowait())
            except Empty:
                break
        await loop.schedule()
    assert model_roles == ["COMMUNICATOR", "ANALYZE"]
    conversation.assert_awaited_once()
    assert work_roles == ["ANALYZE"]
    assert jobs["plan"].get_nowait()["task"]["agent"] == "PLAN_DELEGATE"
    assert sum(task.status == Status.DONE for task in (await engine.tasks("p1")).values()) == 2
    assert "Task started:" in caplog.text and "result sent to Dispatcher" in caplog.text
    assert "PRIVATE_USER_REQUIREMENT" not in caplog.text


async def test_python_queue_submits_requests_to_dispatcher():
    context = mp.get_context("spawn")
    events = context.Queue()
    store, engine, _, _ = await setup()
    loop = DispatcherLoop(engine, events, {}, {}, interval=0.02)
    task = asyncio.create_task(loop.run())
    try:
        client = DispatcherClient(events)
        receipt = await client.submit("p1", "human_message", {"message": "Build API"})
        async with asyncio.timeout(5):
            while not await store.get(("coding", "p1", "responses", "COMMUNICATOR"), receipt["request_id"]):
                await asyncio.sleep(.01)
        assert len(await engine.tasks("p1")) == 1
    finally:
        events.put(None)
        await asyncio.wait_for(task, timeout=5)
        events.close()
        events.join_thread()
