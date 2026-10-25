"""Chat and Store progress streaming; Dispatcher still owns task writes."""
import asyncio
from importlib.resources import files
import json
from pathlib import Path
from typing import Annotated

from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.runtime import Runtime

from orchestrator.graphs.common import AgentState
from orchestrator.runtime.decision import DecisionRunner, safe_payload
from orchestrator.schemas.delegation import decision_schema
from orchestrator.schemas.task_graph import TaskGraphOutput
from orchestrator.graphs.progress import follow_progress


class CommunicatorState(AgentState, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    project_id: str | None
    execution_chain_id: str | None


def _role_prompt() -> str:
    path = Path(__file__).resolve().parents[3] / "prompts" / "communicator.md"
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return files("orchestrator").joinpath("prompts", "communicator.md").read_text(encoding="utf-8")


async def communicate(state: CommunicatorState, runtime: Runtime) -> dict:
    task = state.get("task")
    if task is not None and task.get("agent") != "COMMUNICATOR":
        raise ValueError("Communicator cannot run a task assigned to another agent")
    history = state.get("messages", [])
    if not any(isinstance(message, HumanMessage) for message in history):
        raise ValueError("Communicator requires a user message in messages")
    messages = [SystemMessage(content=await asyncio.to_thread(_role_prompt)), *history]
    model = DecisionRunner()
    writer = runtime.stream_writer
    if task is None:
        if state.get("project_id"):
            return await follow_progress(state, model, messages, writer, store=runtime.store)
        reply = await model.chat(messages, stream=True)
        writer({"type": "reply", "id": reply.id, "content": reply.content})
        return {}

    # A Dispatcher-owned task needs the shared proposal protocol. Send the
    # conversation as real chat messages, without a Qwen Code work pass.
    context = {key: value for key, value in state.get("context", {}).items() if key != "Messages"}
    messages.insert(1, HumanMessage(content="Dispatcher task metadata (input data):\n" + json.dumps(
        safe_payload({"task": task, "context": context}), ensure_ascii=False, default=str)))
    output = await model.ainvoke(messages, decision_schema("COMMUNICATOR"))
    output = TaskGraphOutput.model_validate(output).model_dump(mode="json", by_alias=True)
    expected = {"project_id": task["project_id"], "source_task_id": task["task_id"],
                "producer_agent": "COMMUNICATOR", "execution_chain_id": task["execution_chain_id"]}
    if any(output[key] != value for key, value in expected.items()):
        raise ValueError("Communicator decision source identity does not match the claimed task")
    # Dispatcher journals accepted replies; this node only streams them.
    human_request = output.get("human_request")
    notification = output.get("notification")
    approval = next((node for node in output.get("nodes", [])
                     if node.get("node_type") == "HUMAN_APPROVAL"), {})
    reply = ((human_request or {}).get("question") or approval.get("description") or approval.get("title")
             or (notification or {}).get("message") or output.get("summary"))
    if reply:
        writer({"type": "reply", "id": task["task_id"], "content": reply})
    return {"result": output}


conversation = StateGraph(CommunicatorState)
conversation.add_node("respond", communicate)
conversation.add_edge(START, "respond")
conversation.add_edge("respond", END)

builder = StateGraph(CommunicatorState)
builder.add_node("communicate", conversation.compile())
builder.add_edge(START, "communicate")
builder.add_edge("communicate", END)
graph = builder.compile()
