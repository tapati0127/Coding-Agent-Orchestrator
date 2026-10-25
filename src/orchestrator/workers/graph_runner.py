"""Run one independent graph through the Agent Server SDK."""

from __future__ import annotations

import inspect
from typing import Any, Callable


async def run_graph(
    client: Any,
    agent: str,
    thread_id: str,
    task: dict,
    context: dict,
    cwd: str,
    on_progress: Callable | None = None,
) -> dict:
    result = None
    async for chunk in client.runs.stream(
        thread_id,
        agent.lower(),
        input={"task": task, "context": context, "cwd": cwd},
        stream_mode=["values", "custom"],
        on_disconnect="cancel",
    ):
        if chunk.event == "error":
            # SDK error details can contain private graph state. Public failure
            # events expose only a fixed code; detailed server logs stay private.
            raise RuntimeError("Agent Server graph run failed; inspect private runtime logs")
        if chunk.event == "values" and isinstance(chunk.data, dict):
            result = chunk.data.get("result", result)
        if chunk.event == "custom" and on_progress:
            output = on_progress(chunk.data)
            if inspect.isawaitable(output):
                await output
    if not isinstance(result, dict):
        raise RuntimeError("Agent Server run completed without a TaskGraphOutput")
    return result
