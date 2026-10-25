"""Invoke a graph locally in its worker process; no Store or SDK."""

import inspect

from langchain_core.messages import messages_from_dict


async def run_graph(agent, task, context, cwd, on_progress=None):
    if task.get("agent") != agent:
        raise ValueError("Task agent does not match worker role")
    if agent == "COMMUNICATOR":
        from orchestrator.graphs.communicator import graph
    else:
        from orchestrator.graphs.common import build_agent_graph

        graph = build_agent_graph(agent)
    state = {"task": task, "context": context, "cwd": cwd}
    if agent == "COMMUNICATOR":
        state["messages"] = messages_from_dict(context.get("Messages", []))
    result = None
    async for mode, data in graph.astream(state, stream_mode=["values", "custom"]):
        if mode == "values":
            result = data.get("result", result)
        elif mode == "custom" and on_progress:
            callback = on_progress(data)
            if inspect.isawaitable(callback):
                await callback
    if not isinstance(result, dict):
        raise RuntimeError("Graph completed without a TaskGraphOutput")
    return result
