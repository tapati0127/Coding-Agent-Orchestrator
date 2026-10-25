"""Async Store boundary for Dispatcher and Communicator's progress reads.

Namespaces are logical partitions, not security boundaries. Keep this adapter out
of Qwen's tool environment. InMemoryStore is for tests, never a runtime fallback.
"""
import copy
import os


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


class SDKStore:
    def __init__(self, url=None, token=None):
        from langgraph_sdk import get_client
        self.client = get_client(
            url=url or os.getenv("LANGGRAPH_URL", "http://127.0.0.1:2024"),
            api_key=token or os.getenv("ORCHESTRATOR_TOKEN", ""),
        )

    async def get(self, ns, key):
        item = await self.client.store.get_item(tuple(ns), key)
        return item["value"] if item else None

    async def put(self, ns, key, value):
        await self.client.store.put_item(tuple(ns), key, value)

    async def delete(self, ns, key):
        await self.client.store.delete_item(tuple(ns), key)

    async def search(self, ns, limit=10000):
        result = []
        while len(result) < limit:
            page_size = min(100, limit - len(result))
            batch = await self.client.store.search_items(
                tuple(ns), limit=page_size, offset=len(result))
            items = batch["items"]
            result.extend(item["value"] for item in items)
            if len(items) < page_size:
                break
        return result
