"""Agent Server starts the topology defined in the outer graph.py."""
from contextlib import asynccontextmanager

import graph
from orchestrator import server


async def test_server_lifespan_starts_outer_graph_with_injected_runtime(monkeypatch):
    runtime = object()
    calls = []

    @asynccontextmanager
    async def lifespan(supplied, settings):
        assert supplied is runtime
        calls.append("started")
        yield
        calls.append("stopped")

    monkeypatch.setattr(server, "get_runtime", lambda: runtime)
    monkeypatch.setattr(graph, "graph_lifespan", lifespan)
    async with server.lifespan(server.app):
        assert calls == ["started"]
    assert calls == ["started", "stopped"]
