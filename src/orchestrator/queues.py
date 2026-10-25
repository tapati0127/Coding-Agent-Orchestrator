"""Python queues for workers; a small manager bridge for the external web process."""

import asyncio
from functools import lru_cache
import json
from multiprocessing.managers import BaseManager, DictProxy
import os
import time
from uuid import uuid4


_events = None
_replies = None


def _initialize(events):
    global _events, _replies
    _events, _replies = events, {}


def _get_events():
    return _events


def _get_replies():
    return _replies


class QueueBridge(BaseManager):
    pass


QueueBridge.register("events", callable=_get_events)
QueueBridge.register("replies", callable=_get_replies, proxytype=DictProxy)


def start_bridge(events, context):
    bridge = QueueBridge(address=("127.0.0.1", 0), authkey=os.urandom(32), ctx=context)
    bridge.start(initializer=_initialize, initargs=(events,))
    return bridge


class DispatcherClient:
    """Dashboard requests go to Dispatcher; this client has no Store connection."""

    def __init__(self, bridge):
        self.bridge = bridge
        self.events, self.replies = bridge.events(), bridge.replies()

    async def submit(self, project_id, kind, payload, *, request_id=None):
        request_id = request_id or uuid4().hex
        await asyncio.to_thread(self.events.put, {
            "request_id": request_id, "project_id": project_id, "agent": "COMMUNICATOR",
            "kind": kind, "payload": payload,
        })
        return {"request_id": request_id, "status": "submitted"}

    def _read(self, operation, namespace, key=None, limit=10000):
        request_id, deadline = uuid4().hex, time.time() + 30
        self.events.put({"request_id": request_id, "kind": "dashboard_read", "deadline": deadline,
                         "payload": {"operation": operation, "namespace": list(namespace), "key": key, "limit": limit}})
        while time.time() < deadline:
            reply = self.replies.pop(request_id, None)
            if reply is not None:
                if not reply["accepted"]:
                    raise RuntimeError(reply["reason"])
                return reply["value"]
            time.sleep(0.02)
        raise TimeoutError("Dispatcher did not answer dashboard request")

    async def get(self, namespace, key):
        return await asyncio.to_thread(self._read, "get", namespace, key)

    async def search(self, namespace, limit=10000):
        return await asyncio.to_thread(self._read, "search", namespace, limit=limit)


@lru_cache(maxsize=1)
def get_dispatcher():
    address = tuple(json.loads(os.environ["ORCHESTRATOR_IPC_ADDRESS"]))
    bridge = QueueBridge(address=address, authkey=bytes.fromhex(os.environ["ORCHESTRATOR_IPC_KEY"]))
    bridge.connect()
    return DispatcherClient(bridge)
