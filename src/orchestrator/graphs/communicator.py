"""The Communicator uses deterministic Python and never starts Qwen Code."""

from typing import Any

from langgraph.graph import END, START, StateGraph

from orchestrator.graphs.common import AgentState


def communicator_output(
    project_id: str,
    message: str,
    execution_chain_id: str,
    source_task_id: str = "human",
    target_agent: str = "ANALYZE",
) -> dict[str, Any]:
    if target_agent not in {"ANALYZE", "PLAN_DELEGATE"}:
        raise ValueError("Human intake can delegate only to Analyze or Plan")
    if not message.strip():
        raise ValueError("A human message is required")
    return {
        "schema_version": "1.0",
        "project_id": project_id,
        "source_task_id": source_task_id,
        "producer_agent": "COMMUNICATOR",
        "execution_chain_id": execution_chain_id,
        "summary": message,
        "source_result": None,
        "nodes": [
            {
                "id": "human-request",
                "node_type": "TASK",
                "target_agent": target_agent,
                "title": "Analyze the human request" if target_agent == "ANALYZE" else "Plan the approved request",
                "description": message,
                "acceptance_criteria": ["Address the human request and identify acceptance criteria"],
                "priority": 2,
                "context_refs": [],
                "artifact_refs": [],
            }
        ],
        "edges": [],
        "human_request": None,
        "shared_updates": [],
        "private_updates": [],
        "notification": None,
    }


def communicate(state: AgentState) -> dict[str, Any]:
    task, context = state.get("task", {}), state.get("context", {})
    return {
        "result": communicator_output(
            project_id=task["project_id"],
            message=context.get("message", task.get("description", "")),
            execution_chain_id=task["execution_chain_id"],
            source_task_id=task.get("task_id", "human"),
            target_agent=context.get("target_agent", "ANALYZE"),
        )
    }


builder = StateGraph(AgentState)
builder.add_node("communicate", communicate)
builder.add_edge(START, "communicate")
builder.add_edge("communicate", END)
graph = builder.compile()
