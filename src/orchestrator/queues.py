"""Python worker queues shared with the Agent Server's Dispatcher loop."""

import asyncio
from functools import lru_cache
import json
from multiprocessing.managers import BaseManager, DictProxy
import os
import time
from uuid import uuid4


_events = None
_replies = None
_inboxes = None
_roles = None
_status = None


def _initialize(events, inboxes, roles):
    global _events, _replies, _inboxes, _roles, _status
    _events, _replies = events, {}
    _inboxes, _roles, _status = inboxes, roles, {"state": "starting"}


def _get_events():
    return _events


def _get_replies():
    return _replies


def _get_inbox(worker_id):
    return _inboxes[worker_id]


def _get_roles():
    return _roles


def _get_status():
    return _status


class QueueBridge(BaseManager):
    pass


QueueBridge.register("events", callable=_get_events)
QueueBridge.register("replies", callable=_get_replies, proxytype=DictProxy)
QueueBridge.register("inbox", callable=_get_inbox)
QueueBridge.register("roles", callable=_get_roles, proxytype=DictProxy)
QueueBridge.register("status", callable=_get_status, proxytype=DictProxy)


def start_bridge(events, context, inboxes=None, roles=None):
    bridge = QueueBridge(address=("127.0.0.1", 0), authkey=os.urandom(32), ctx=context)
    bridge.start(initializer=_initialize, initargs=(events, inboxes or {}, roles or {}))
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


def connect_bridge():
    address = tuple(json.loads(os.environ["ORCHESTRATOR_IPC_ADDRESS"]))
    bridge = QueueBridge(address=address, authkey=bytes.fromhex(os.environ["ORCHESTRATOR_IPC_KEY"]))
    bridge.connect()
    return bridge


@lru_cache(maxsize=1)
def get_dispatcher():
    bridge = connect_bridge()
    return DispatcherClient(bridge)
