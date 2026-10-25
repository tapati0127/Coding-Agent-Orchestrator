"""LangGraph state contracts for phases, roles and the top-level workflow."""
from typing import Annotated, TypedDict
from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from .schemas import ExecuteTask


class PhaseState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    workspace: str
    prompt: str
    output: str
    cli_error: str
    status: str
    error: str


class TaskState(PhaseState, total=False):
    task: ExecuteTask
    assessment: dict
    execution_result: dict


class PlanState(PhaseState, total=False):
    natural_plan: str
    plan: dict
    replan_count: int
    execution_history: list[dict]
    approved_work: list[dict]


class WorkflowState(PlanState, total=False):
    results: dict[str, dict]
    clarification: dict
