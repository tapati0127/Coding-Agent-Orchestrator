"""The only application process that writes authoritative Store records."""

import asyncio
import fcntl
import logging

from orchestrator.config import Settings
from orchestrator.store import SDKStore
from .engine import Dispatcher
from .process import DispatcherLoop


def run_dispatcher(events, inboxes, roles, replies, settings: Settings):
    logging.basicConfig(level=logging.INFO)
    settings.data_root.mkdir(parents=True, exist_ok=True)
    with (settings.data_root / "dispatcher.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        engine = Dispatcher(SDKStore(url=settings.langgraph_url, token=settings.orchestrator_token),
            max_qwen_sessions=settings.max_qwen_sessions, max_execute_workers=settings.max_execute_workers,
            max_backward=settings.max_backward_delegations, max_attempts=settings.max_technical_retries,
            lease_seconds=settings.task_lease_seconds,
            human_response_timeout_seconds=settings.human_response_timeout_seconds)
        asyncio.run(DispatcherLoop(engine, events, inboxes, roles, replies,
                                  interval=settings.dispatch_poll_seconds).run())
