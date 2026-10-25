"""User-facing LLM graph. Dispatcher owns delegation and conversation persistence."""
from typing import Annotated

from langchain_core.messages import AIMessage, AnyMessage, messages_to_dict
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from orchestrator.graphs.common import AgentState
from orchestrator.runtime.qwen import QwenRunner


class CommunicatorState(AgentState):
    Messages: Annotated[list[AnyMessage], add_messages]


async def communicate(state: CommunicatorState) -> dict:
    task = state["task"]
    if task.get("agent") != "COMMUNICATOR":
        raise ValueError("Communicator cannot run a task assigned to another agent")
    context = {**state.get("context", {}), "Messages": messages_to_dict(state.get("Messages", []))}
    output = await QwenRunner().run(task=task, context=context, cwd=state["cwd"])
    # Checkpoints contain the current turn. The Dispatcher journals accepted
    # messages in the conversation so later tasks/threads see the same history.
    human_request = output.get("human_request")
    notification = output.get("notification")
    approval = next((node for node in output.get("nodes", [])
                     if node.get("node_type") == "HUMAN_APPROVAL"), {})
    reply = ((human_request or {}).get("question") or approval.get("description") or approval.get("title")
             or (notification or {}).get("message") or output.get("summary"))
    return {"result": output, "Messages": [AIMessage(content=reply, id=task["task_id"])] if reply else []}


builder = StateGraph(CommunicatorState)
builder.add_node("communicate", communicate)
builder.add_edge(START, "communicate")
builder.add_edge("communicate", END)
graph = builder.compile()
