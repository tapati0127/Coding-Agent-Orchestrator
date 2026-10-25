"""Graph nodes contain agent work only; the Dispatcher owns task lifecycle."""

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from orchestrator.runtime.qwen import QwenRunner


class AgentState(TypedDict, total=False):
    task: dict[str, Any]
    context: dict[str, Any]
    cwd: str
    result: dict[str, Any]


def build_agent_node(agent: str):
    async def work(state: AgentState) -> dict[str, Any]:
        task = state.get("task", {})
        if task.get("agent") != agent:
            raise ValueError(f"{agent} graph cannot run a task assigned to another agent")
        if not state.get("cwd"):
            raise ValueError("A provisioned working directory is required")
        # Custom events intentionally contain phase metadata only. Raw Qwen output
        # may include private analysis and is kept in the runtime's private logs.
        try:
            from langgraph.config import get_stream_writer

            writer = get_stream_writer()
        except RuntimeError:
            writer = lambda _: None
        count = 0

        async def progress(chunk: dict | str) -> None:
            nonlocal count
            count += 1
            line = chunk.get("line", "") if isinstance(chunk, dict) else chunk
            phase = chunk.get("phase", "running") if isinstance(chunk, dict) else "running"
            writer({"type": "qwen_progress", "chunks": count, "bytes": len(line), "phase": phase})

        result = await QwenRunner().run(
            task=task,
            context=state.get("context", {}),
            cwd=state["cwd"],
            on_log=progress,
        )
        return {"result": result}

    return work


def build_agent_graph(agent: str):
    graph = StateGraph(AgentState)
    graph.add_node("work", build_agent_node(agent))
    graph.add_edge(START, "work")
    graph.add_edge("work", END)
    return graph.compile()
