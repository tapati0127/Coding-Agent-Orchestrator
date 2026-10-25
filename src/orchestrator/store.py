"""Dictionary records backed by the Store supplied in LangGraph Runtime.

Namespaces are logical partitions, not security boundaries. Keep this adapter out
of Qwen's tool environment. InMemoryStore is for tests, never a runtime fallback.
"""
import copy
from langgraph.store.base import BaseStore


class InMemoryStore:
    def __init__(self):
        self.records = {}

    async def get(self, ns, key):
        return copy.deepcopy(self.records.get((tuple(ns), key)))

    async def put(self, ns, key, value):
        self.records[(tuple(ns), key)] = copy.deepcopy(value)

    async def delete(self, ns, key):
        self.records.pop((tuple(ns), key), None)

    async def search(self, ns, limit=10000):
        prefix = tuple(ns)
        return [copy.deepcopy(v) for (n, _), v in self.records.items()
                if n[:len(prefix)] == prefix][:limit]


class StoreRecords:
    """Unwrap native Store Items for Dispatcher's dictionary record protocol."""

    def __init__(self, store: BaseStore):
        self.store = store

    async def get(self, ns, key):
        item = await self.store.aget(tuple(ns), key)
        return item.value if item else None

    async def put(self, ns, key, value):
        await self.store.aput(tuple(ns), key, value)

    async def delete(self, ns, key):
        await self.store.adelete(tuple(ns), key)

    async def search(self, ns, limit=10000):
        result = []
        while len(result) < limit:
            page_size = min(100, limit - len(result))
            items = await self.store.asearch(
                tuple(ns), limit=page_size, offset=len(result))
            result.extend(item.value for item in items)
            if len(items) < page_size:
                break
        return result


class QueueStoreRecords:
    """Dispatcher-only RPC client. Native Store stays in Communicator's process."""

    def __init__(self, requests, responses):
        import asyncio
        self.requests, self.responses = requests, responses
        self.lock = asyncio.Lock()

    async def _call(self, method, *args):
        import asyncio
        from queue import Empty
        from uuid import uuid4

        async with self.lock:
            request_id = uuid4().hex
            await asyncio.to_thread(self.requests.put, (request_id, method, args))
            try:
                response_id, accepted, value = await asyncio.to_thread(self.responses.get, True, 30)
            except Empty:
                raise RuntimeError("Main-process Store did not respond within 30 seconds") from None
            if response_id != request_id:
                raise RuntimeError("Store queue response does not match its request")
            if not accepted:
                raise RuntimeError(f"Main-process Store operation failed ({value})")
            return value

    async def get(self, ns, key):
        return await self._call("get", ns, key)

    async def put(self, ns, key, value):
        return await self._call("put", ns, key, value)

    async def delete(self, ns, key):
        return await self._call("delete", ns, key)

    async def search(self, ns, limit=10000):
        return await self._call("search", ns, limit)


async def serve_store(records, requests, responses):
    """Execute only Dispatcher's record operations against the injected Store."""
    import asyncio
    from queue import Empty

    methods = {name: getattr(records, name) for name in ("get", "put", "delete", "search")}
    while True:
        try:
            request = await asyncio.to_thread(requests.get, True, .2)
        except Empty:
            continue
        if request is None:
            return
        request_id, method, args = request
        try:
            value = await methods[method](*args)
            response = (request_id, True, value)
        except Exception as exc:
            # Never send Store exception text (which may contain private data).
            response = (request_id, False, type(exc).__name__)
        await asyncio.to_thread(responses.put, response)
