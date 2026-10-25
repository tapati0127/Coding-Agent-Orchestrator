"""Communicator submits to the stdlib queue declared by the outer graph.py."""
import asyncio
from uuid import uuid4


class DispatcherClient:
    def __init__(self, events):
        self.events = events

    async def submit(self, project_id, kind, payload, *, request_id=None):
        request_id = request_id or uuid4().hex
        await asyncio.to_thread(self.events.put, {
            "request_id": request_id, "project_id": project_id, "agent": "COMMUNICATOR",
            "kind": kind, "payload": payload,
        })
        return {"request_id": request_id, "status": "submitted"}


_dispatcher_client = None


def configure_dispatcher(events):
    global _dispatcher_client
    _dispatcher_client = DispatcherClient(events) if events is not None else None


def get_dispatcher():
    if _dispatcher_client is None:
        raise RuntimeError("Start the application through graph.py / the Agent Server lifecycle")
    return _dispatcher_client
