"""Dispatcher uses the native Store from Runtime and serves the worker queues."""

import asyncio
from queue import Queue

import pytest
from langgraph.runtime import Runtime
from langgraph.store.memory import InMemoryStore

from orchestrator.config import Settings
from orchestrator.dispatcher.main import run_dispatcher
from orchestrator.queues import get_dispatcher
from orchestrator.workers.main import Worker
from graph import graph_lifespan
import os
from orchestrator.store import StoreRecords


async def test_dispatcher_runtime_store_receives_authoritative_writes(tmp_path):
    store = InMemoryStore()
    events, inbox = Queue(), Queue()
    events.put({"kind": "register_project", "request_id": "register", "project_id": "p",
                "agent": "COMMUNICATOR", "payload": {"title": "Project"}})
    events.put({"kind": "human_message", "request_id": "human", "project_id": "p",
                "agent": "COMMUNICATOR", "payload": {"message": "Build API"}})
    events.put(None)
    await run_dispatcher(Runtime(store=store), events, {"chat": inbox}, {"chat": "COMMUNICATOR"},
                         Settings(data_root=tmp_path))
    assert (await store.aget(("coding", "projects"), "p")).value["title"] == "Project"
    job = inbox.get_nowait()
    saved = (await store.aget(("coding", "p", "tasks"), job["task"]["task_id"])).value
    assert saved["status"] == "DOING" and saved["assigned_worker"] == "chat"


async def test_dispatcher_requires_runtime_store_without_fallback(tmp_path):
    with pytest.raises(RuntimeError, match="runtime store"):
        await run_dispatcher(Runtime(), Queue(), {}, {}, Settings(data_root=tmp_path))


async def test_store_records_paginate_and_delete_native_items():
    store = InMemoryStore()
    records = StoreRecords(store)
    for index in range(105):
        await records.put(("coding", "p", "tasks"), str(index), {"index": index})
    assert len(await records.search(("coding", "p", "tasks"))) == 105
    assert len(await records.search(("coding", "p", "tasks"), limit=3)) == 3
    await records.delete(("coding", "p", "tasks"), "0")
    assert await records.get(("coding", "p", "tasks"), "0") is None
    assert await store.aget(("coding", "p", "tasks"), "0") is None


async def test_outer_graph_dispatcher_is_child_and_communicator_stays_in_main(tmp_path, monkeypatch):
    store = InMemoryStore()
    handled = []
    main_pid = os.getpid()

    async def communicate(self, job):
        assert self.agent == "COMMUNICATOR"
        handled.append((os.getpid(), job))

    monkeypatch.setattr(Worker, "execute", communicate)
    settings = Settings(data_root=tmp_path, max_execute_workers=1, dispatch_poll_seconds=.02)
    application = None
    async with graph_lifespan(Runtime(store=store), settings) as application:
        assert "communicator-01" not in application.processes
        assert set(application.processes) == {"dispatcher", "analyze-01", "plan_delegate-01",
                                              "execute-01", "verify_integrate-01", "finalize-01"}
        assert all(process.pid != main_pid and process.is_alive() for process in application.processes.values())
        client = get_dispatcher()
        await client.submit("p", "register_project", {"title": "Project"})
        await client.submit("p", "human_message", {"message": "Build API"})
        async with asyncio.timeout(10):
            while not handled:
                await asyncio.sleep(.02)
        pid, job = handled[0]
        assert pid == main_pid
        assert job["task"]["agent"] == "COMMUNICATOR"
        saved = (await store.aget(("coding", "p", "tasks"), job["task"]["task_id"])).value
        assert saved["status"] == "DOING" and saved["assigned_worker"] == "communicator-01"
        children = list(application.processes.values())
    assert all(not process.is_alive() and process.exitcode == 0 for process in children)
    with pytest.raises(RuntimeError, match="Start the application"):
        get_dispatcher()


async def test_queue_store_round_trip_errors_and_pagination():
    from orchestrator.store import QueueStoreRecords, serve_store

    native = InMemoryStore()
    requests, responses = Queue(), Queue()
    records = QueueStoreRecords(requests, responses)
    service = asyncio.create_task(serve_store(StoreRecords(native), requests, responses))
    try:
        for index in range(105):
            await records.put(("coding", "p", "tasks"), str(index), {"index": index})
        assert len(await records.search(("coding", "p", "tasks"))) == 105
        assert len(await records.search(("coding", "p", "tasks"), 3)) == 3
        await records.delete(("coding", "p", "tasks"), "0")
        assert await records.get(("coding", "p", "tasks"), "0") is None
        with pytest.raises(RuntimeError, match="KeyError"):
            await records._call("unsupported")
        assert await records.get(("coding", "p", "tasks"), "1") == {"index": 1}
    finally:
        requests.put(None)
        await service
