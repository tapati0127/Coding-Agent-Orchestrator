"""Inbox/receipt transport for the single-writer Dispatcher."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from uuid import uuid4


class ReceiptTimeout(RuntimeError):
    pass


async def submit_request(
    store: Any,
    project_id: str,
    agent: str,
    worker_id: str,
    kind: str,
    *,
    task_id: str | None = None,
    payload: dict | None = None,
    wait: bool = True,
    timeout: float = 60,
    request_id: str | None = None,
) -> dict:
    request_id = request_id or str(uuid4())
    request = {
        "request_id": request_id,
        "kind": kind,
        "project_id": project_id,
        "agent": agent,
        "worker_id": worker_id,
        "task_id": task_id,
        "payload": payload or {},
        "submitted_at": time.time(),
    }
    await store.put(("coding", project_id, "requests", agent), request_id, request)
    if not wait:
        return {"request_id": request_id}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        receipt = await store.get(("coding", project_id, "responses", agent), request_id)
        if receipt is not None:
            return receipt
        await asyncio.sleep(0.15)
    # Do not issue a different claim on uncertainty: process restart plus leases
    # reconciles the original request without claiming a second task in parallel.
    raise ReceiptTimeout(f"No Dispatcher receipt for {kind} request {request_id}")
