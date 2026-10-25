"""Application entrypoint: Communicator in this process, other graphs in children.

All inter-process queues are created here using Python's multiprocessing.Queue.
Importing this module never starts a process or allocates a queue.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import multiprocessing as mp
import os
import signal
from queue import Empty
import time
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from orchestrator.config import Settings
from orchestrator.dispatcher.main import run_dispatcher_records
from orchestrator.graphs.communicator import graph
from orchestrator.queues import configure_dispatcher
from orchestrator.store import QueueStoreRecords, StoreRecords, serve_store
from orchestrator.workers.main import Worker

log = logging.getLogger(__name__)


def worker_specs(settings):
    roles = {"communicator-01": "COMMUNICATOR", "analyze-01": "ANALYZE",
             "plan_delegate-01": "PLAN_DELEGATE", "verify_integrate-01": "VERIFY_INTEGRATE",
             "finalize-01": "FINALIZE"}
    roles.update({f"execute-{i:02d}": "EXECUTE" for i in range(1, settings.max_execute_workers + 1)})
    return roles


async def agent_loop(agent, worker_id, inbox, events, settings):
    """Run each assigned graph once, then wait for its next queue message."""
    worker = Worker(agent, worker_id, events, data_root=settings.data_root)
    while True:
        try:
            job = await asyncio.to_thread(inbox.get, True, .2)
        except Empty:
            continue
        if job is None:
            return
        if job["task"]["lease_expires_at"] <= time.time():
            continue
        await worker.execute(job)


def agent_process(agent, worker_id, inbox, events, settings):
    for name in settings.__dataclass_fields__:
        os.environ[name.upper()] = str(getattr(settings, name))
    # Own a process group; SIGTERM cancels asyncio so Qwen cleanup can run.
    if hasattr(os, "setsid"):
        os.setsid()
    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        asyncio.run(agent_loop(agent, worker_id, inbox, events, settings))
    except KeyboardInterrupt:
        pass


class DispatcherState(TypedDict):
    pass


def dispatcher_process(events, inboxes, roles, requests, responses, ready, settings):
    for name in settings.__dataclass_fields__:
        os.environ[name.upper()] = str(getattr(settings, name))
    records = QueueStoreRecords(requests, responses)

    async def dispatch(state):
        # DispatcherLoop owns the while True queue reader and scheduler.
        await run_dispatcher_records(records, events, inboxes, roles, settings, on_ready=ready.set)
        return {}

    builder = StateGraph(DispatcherState)
    builder.add_node("dispatch", dispatch)
    builder.add_edge(START, "dispatch")
    builder.add_edge("dispatch", END)
    asyncio.run(builder.compile().ainvoke({}))


class GraphRuntime:
    """Own queues and child graph processes for one Communicator server."""

    def __init__(self, store, settings):
        if store is None:
            raise RuntimeError("Graph runtime requires a LangGraph runtime store")
        self.settings = settings
        self.context = mp.get_context("spawn")
        self.roles = worker_specs(settings)
        # Queue declarations live in the outer graph.py, never in model graphs.
        self.dispatcher_queue = self.context.Queue()
        self.agent_queues = {worker_id: self.context.Queue() for worker_id in self.roles}
        self.store_requests = self.context.Queue()
        self.store_responses = self.context.Queue()
        self.ready = self.context.Event()
        self.store = StoreRecords(store)
        self.processes = {}
        self.restarts = {}
        self.started = {}
        self.closing = False

    def spawn_agent(self, worker_id):
        process = self.context.Process(name=worker_id, target=agent_process,
            args=(self.roles[worker_id], worker_id, self.agent_queues[worker_id],
                  self.dispatcher_queue, self.settings))
        process.start()
        self.processes[worker_id] = process
        self.started[worker_id] = time.monotonic()

    async def start(self):
        configure_dispatcher(self.dispatcher_queue)
        self.store_task = asyncio.create_task(serve_store(self.store, self.store_requests, self.store_responses))
        dispatcher = self.context.Process(name="dispatcher", target=dispatcher_process,
            args=(self.dispatcher_queue, self.agent_queues, self.roles, self.store_requests,
                  self.store_responses, self.ready, self.settings))
        await asyncio.to_thread(dispatcher.start)
        self.processes["dispatcher"] = dispatcher
        async with asyncio.timeout(90):
            while not self.ready.is_set():
                if not dispatcher.is_alive():
                    raise RuntimeError("Dispatcher stopped during startup")
                if self.store_task.done():
                    await self.store_task
                await asyncio.sleep(.05)
        # Communicator consumes its queue in the main process, alongside SDK chat.
        self.communicator_task = asyncio.create_task(agent_loop("COMMUNICATOR", "communicator-01",
            self.agent_queues["communicator-01"], self.dispatcher_queue, self.settings))
        for worker_id, agent in self.roles.items():
            if agent != "COMMUNICATOR":
                await asyncio.to_thread(self.spawn_agent, worker_id)
        self.monitor_task = asyncio.create_task(self.monitor())

    async def monitor(self):
        while True:
            await asyncio.sleep(.25)
            if self.store_task.done() or self.communicator_task.done() or not self.processes["dispatcher"].is_alive():
                log.error("A required graph runtime component stopped")
                os.kill(os.getpid(), signal.SIGTERM)
                return
            for worker_id, process in list(self.processes.items()):
                if worker_id == "dispatcher" or process.is_alive():
                    continue
                await asyncio.to_thread(process.join)
                window = float(os.getenv("WORKER_RESTART_WINDOW_SECONDS", "300"))
                count = 0 if time.monotonic() - self.started[worker_id] >= window else self.restarts.get(worker_id, 0)
                if count >= int(os.getenv("WORKER_MAX_RESTARTS", "3")):
                    log.error("Worker %s exceeded restart budget", worker_id)
                    os.kill(os.getpid(), signal.SIGTERM)
                    return
                self.restarts[worker_id] = count + 1
                await asyncio.to_thread(self.spawn_agent, worker_id)

    async def stop(self):
        if self.closing:
            return
        self.closing = True
        if hasattr(self, "monitor_task"):
            self.monitor_task.cancel()
            await asyncio.gather(self.monitor_task, return_exceptions=True)
        for inbox in self.agent_queues.values():
            await asyncio.to_thread(inbox.put, None)
        # Keep serving Store while workers flush results and Dispatcher applies them.
        deadline = time.monotonic() + 10
        for name, process in self.processes.items():
            if name != "dispatcher":
                await asyncio.to_thread(process.join, max(0, deadline - time.monotonic()))
                await self.stop_process(process)
        if hasattr(self, "communicator_task"):
            try:
                await asyncio.wait_for(asyncio.shield(self.communicator_task), max(.1, deadline - time.monotonic()))
            except TimeoutError:
                self.communicator_task.cancel()
                await asyncio.gather(self.communicator_task, return_exceptions=True)
        await asyncio.to_thread(self.dispatcher_queue.put, None)
        dispatcher = self.processes.get("dispatcher")
        if dispatcher:
            await asyncio.to_thread(dispatcher.join, 10)
            await self.stop_process(dispatcher)
        await asyncio.to_thread(self.store_requests.put, None)
        if hasattr(self, "store_task"):
            await self.store_task
        configure_dispatcher(None)
        for queue in [self.dispatcher_queue, *self.agent_queues.values(), self.store_requests, self.store_responses]:
            queue.cancel_join_thread()
            queue.close()
        self.processes.clear()

    @staticmethod
    async def stop_process(process):
        if process.is_alive():
            await asyncio.to_thread(process.terminate)
            await asyncio.to_thread(process.join, 5)
        if process.is_alive():
            await asyncio.to_thread(process.kill)
        await asyncio.to_thread(process.join)


@asynccontextmanager
async def graph_lifespan(runtime, settings):
    application = await asyncio.to_thread(GraphRuntime, runtime.store, settings)
    try:
        await application.start()
        yield application
    finally:
        await application.stop()


if __name__ == "__main__":
    from orchestrator.supervisor import main
    main()
