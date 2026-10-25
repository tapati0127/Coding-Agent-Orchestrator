"""Dispatcher runs in Agent Server's lifespan using the injected Runtime Store."""

import asyncio
import fcntl
import logging
from contextlib import asynccontextmanager

from langgraph.runtime import Runtime

from orchestrator.config import Settings
from orchestrator.store import StoreRecords
from orchestrator.queues import connect_bridge
from .engine import Dispatcher
from .process import DispatcherLoop


def _acquire_lock(settings):
    settings.data_root.mkdir(parents=True, exist_ok=True)
    lock = (settings.data_root / "dispatcher.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        lock.close()
        raise
    return lock


async def run_dispatcher(runtime: Runtime, events, inboxes, roles, replies,
                         settings: Settings, *, on_ready=None):
    if runtime.store is None:
        raise RuntimeError("Dispatcher requires a LangGraph runtime store")
    lock = await asyncio.to_thread(_acquire_lock, settings)
    try:
        engine = Dispatcher(StoreRecords(runtime.store),
            max_qwen_sessions=settings.max_qwen_sessions, max_execute_workers=settings.max_execute_workers,
            max_backward=settings.max_backward_delegations, max_attempts=settings.max_technical_retries,
            lease_seconds=settings.task_lease_seconds,
            human_response_timeout_seconds=settings.human_response_timeout_seconds)
        await DispatcherLoop(engine, events, inboxes, roles, replies,
                             interval=settings.dispatch_poll_seconds).run(on_ready=on_ready)
    finally:
        await asyncio.to_thread(lock.close)


@asynccontextmanager
async def dispatcher_lifespan(runtime: Runtime, settings: Settings):
    if runtime.store is None:
        raise RuntimeError("Dispatcher requires a LangGraph runtime store")

    def connect():
        bridge = connect_bridge()
        roles = dict(bridge.roles())
        return (bridge, bridge.events(), {wid: bridge.inbox(wid) for wid in roles},
                roles, bridge.replies(), bridge.status())

    bridge, events, inboxes, roles, replies, status = await asyncio.to_thread(connect)
    ready = asyncio.Event()
    task = asyncio.create_task(run_dispatcher(runtime, events, inboxes, roles, replies, settings,
                                              on_ready=ready.set), name="dispatcher")

    async def report_exit():
        try:
            await task
        except asyncio.CancelledError:
            await asyncio.to_thread(status.__setitem__, "state", "stopped")
        except Exception:
            logging.getLogger(__name__).exception("Dispatcher stopped unexpectedly")
            await asyncio.to_thread(status.__setitem__, "state", "failed")
        else:
            await asyncio.to_thread(status.__setitem__, "state", "stopped")

    observer = asyncio.create_task(report_exit())
    startup = asyncio.create_task(ready.wait())
    try:
        done, _ = await asyncio.wait((startup, task), return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task
            raise RuntimeError("Dispatcher stopped during startup")
        await asyncio.to_thread(status.__setitem__, "state", "running")
        yield
    finally:
        startup.cancel()
        await asyncio.gather(startup, return_exceptions=True)
        if not task.done():
            await asyncio.to_thread(events.put, None)
        try:
            await asyncio.wait_for(asyncio.shield(observer), timeout=10)
        except TimeoutError:
            task.cancel()
            await observer
