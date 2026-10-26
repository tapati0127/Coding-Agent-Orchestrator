"""Validated contracts shared by role graphs and orchestration."""
from typing import Literal, TypedDict
from pydantic import BaseModel, ConfigDict, Field


class _RequiredTask(TypedDict):
    """Task payload shared by Plan, Execute and Reviewer."""

    instructions: str


class ExecuteTask(_RequiredTask, total=False):
    task_id: str
    context: str
    acceptance_criteria: str | list[str]
    constraints: str | list[str]
    related_files: list[str]


class Assessment(BaseModel):
    """Report-based assessment, not an independent code/test execution."""
    model_config = ConfigDict(extra="forbid", strict=True)
    status: Literal["pass", "fail"]
    notes: list[str]
    explanation: str = Field(min_length=1)
    blockers: list[str]


# Backwards-compatible name for existing callers.
ExecuteAssessment = Assessment


class ExecuteNode(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    instructions: str = Field(min_length=1)
    context: str = Field(min_length=1)
    related_files: list[str]
    acceptance_criteria: list[str] = Field(min_length=1)
    constraints: list[str]


class ExecuteEdge(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source: str
    target: str


class PlanGraph(BaseModel):
    """Structured plan; empty nodes signal a stop and fail execution validation."""
    model_config = ConfigDict(extra="forbid", strict=True)
    nodes: list[ExecuteNode]
    edges: list[ExecuteEdge]


def validate_plan_graph(plan: PlanGraph) -> dict[str, list[str]]:
    if not plan.nodes:
        raise ValueError("Plan has no executable tasks")
    parents = {node.id: [] for node in plan.nodes}
    if len(parents) != len(plan.nodes):
        raise ValueError("Duplicate node ID")
    for node in plan.nodes:
        if node.id in {"workspace", "results"} or not node.instructions.strip() or not node.context.strip():
            raise ValueError("Invalid node ID or blank task")
    seen = set()
    for edge in plan.edges:
        pair = (edge.source, edge.target)
        if edge.source not in parents or edge.target not in parents or pair in seen:
            raise ValueError("Unknown endpoint or duplicate edge")
        seen.add(pair)
        parents[edge.target].append(edge.source)
    pending, finished = set(parents), set()
    while pending:
        ready = {key for key in pending if set(parents[key]) <= finished}
        if not ready:
            raise ValueError("Cyclic Execute graph")
        finished.update(ready)
        pending -= ready
    return parents




class ClarificationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ready_for_plan: bool
    summary: str = Field(min_length=1)
    missing_information: list[str]
    explanation: str = Field(min_length=1)
