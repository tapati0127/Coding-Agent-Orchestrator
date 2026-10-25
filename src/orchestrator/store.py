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
