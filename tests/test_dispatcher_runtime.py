"""Dispatcher uses the native Store from Runtime and serves the worker queues."""

import asyncio
import multiprocessing as mp
from queue import Queue

import pytest
from langgraph.runtime import Runtime
from langgraph.store.memory import InMemoryStore

from orchestrator.config import Settings
from orchestrator.dispatcher.main import dispatcher_lifespan, run_dispatcher
from orchestrator.queues import DispatcherClient, start_bridge
from orchestrator.store import StoreRecords


async def test_dispatcher_runtime_store_receives_authoritative_writes(tmp_path):
    store = InMemoryStore()
    events, inbox = Queue(), Queue()
    events.put({"kind": "register_project", "request_id": "register", "project_id": "p",
                "agent": "COMMUNICATOR", "payload": {"title": "Project"}})
    events.put({"kind": "human_message", "request_id": "human", "project_id": "p",
                "agent": "COMMUNICATOR", "payload": {"message": "Build API"}})
    events.put(None)
    await run_dispatcher(Runtime(store=store), events, {"chat": inbox}, {"chat": "COMMUNICATOR"}, {},
                         Settings(data_root=tmp_path))
    assert (await store.aget(("coding", "projects"), "p")).value["title"] == "Project"
    job = inbox.get_nowait()
    saved = (await store.aget(("coding", "p", "tasks"), job["task"]["task_id"])).value
    assert saved["status"] == "DOING" and saved["assigned_worker"] == "chat"


async def test_dispatcher_requires_runtime_store_without_fallback(tmp_path):
    with pytest.raises(RuntimeError, match="runtime store"):
        await run_dispatcher(Runtime(), Queue(), {}, {}, {}, Settings(data_root=tmp_path))


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


async def test_dispatcher_lifespan_uses_bridge_inbox_and_stops_before_store_closes(tmp_path, monkeypatch):
    context = mp.get_context("spawn")
    events, inbox = context.Queue(), context.Queue()
    bridge = start_bridge(events, context, {"chat": inbox}, {"chat": "COMMUNICATOR"})
    monkeypatch.setattr("orchestrator.dispatcher.main.connect_bridge", lambda: bridge)
    store = InMemoryStore()
    try:
        async with dispatcher_lifespan(Runtime(store=store), Settings(data_root=tmp_path, dispatch_poll_seconds=.02)):
            client = await asyncio.to_thread(DispatcherClient, bridge)
            assert bridge.status()["state"] == "running"
            await client.submit("p", "register_project", {"title": "Project"})
            await client.submit("p", "human_message", {"message": "Build API"})
            job = await asyncio.to_thread(inbox.get, True, 5)
            assert job["task"]["status"] == "DOING"
            assert (await store.aget(("coding", "projects"), "p")).value["title"] == "Project"
        assert bridge.status()["state"] == "stopped"
    finally:
        bridge.shutdown()
        for queue in (events, inbox):
            queue.close()
            queue.join_thread()
