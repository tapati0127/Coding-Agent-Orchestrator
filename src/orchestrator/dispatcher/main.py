"""Dispatcher runner; production receives a queue-backed Store adapter."""

import asyncio
import fcntl

from langgraph.runtime import Runtime

from orchestrator.config import Settings
from orchestrator.store import StoreRecords
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


async def run_dispatcher(runtime: Runtime, events, inboxes, roles,
                         settings: Settings, *, on_ready=None):
    if runtime.store is None:
        raise RuntimeError("Dispatcher requires a LangGraph runtime store")
    await run_dispatcher_records(StoreRecords(runtime.store), events, inboxes, roles, settings, on_ready=on_ready)


async def run_dispatcher_records(records, events, inboxes, roles, settings, *, on_ready=None):
    lock = await asyncio.to_thread(_acquire_lock, settings)
    try:
        engine = Dispatcher(records,
            max_qwen_sessions=settings.max_qwen_sessions, max_execute_workers=settings.max_execute_workers,
            max_backward=settings.max_backward_delegations, max_attempts=settings.max_technical_retries,
            lease_seconds=settings.task_lease_seconds,
            human_response_timeout_seconds=settings.human_response_timeout_seconds)
        await DispatcherLoop(engine, events, inboxes, roles,
                             interval=settings.dispatch_poll_seconds).run(on_ready=on_ready)
    finally:
        await asyncio.to_thread(lock.close)
