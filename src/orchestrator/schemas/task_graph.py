from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Agent(StrEnum):
    COMMUNICATOR = "COMMUNICATOR"
    ANALYZE = "ANALYZE"
    PLAN_DELEGATE = "PLAN_DELEGATE"
    EXECUTE = "EXECUTE"
    VERIFY_INTEGRATE = "VERIFY_INTEGRATE"
    FINALIZE = "FINALIZE"


class Status(StrEnum):
    TODO = "TODO"
    DOING = "DOING"
    IN_REVIEW = "IN_REVIEW"
    DONE = "DONE"
    CLOSED = "CLOSED"


class Outcome(StrEnum):
    SUCCESS = "SUCCESS"
    NEED_ANALYSIS = "NEED_ANALYSIS"
    NEED_REPLAN = "NEED_REPLAN"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    FAILED = "FAILED"


class SourceResult(StrictModel):
    outcome: Outcome
    proposed_status: Literal["DONE", "CLOSED"] = "DONE"
    artifact_refs: list[str] = Field(default_factory=list, max_length=100)
    close_task_ids: list[str] = Field(default_factory=list, max_length=1000)
    commit_sha: str | None = None
    baseline_commit: str | None = None
    integrated_task_ids: list[str] = Field(default_factory=list, max_length=100)


class GraphNode(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    node_type: Literal["TASK", "HUMAN_APPROVAL", "NOTIFICATION"] = "TASK"
    target_agent: Agent
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=50000)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=100)
    priority: int = Field(default=2, ge=0, le=10)
    context_refs: list[str] = Field(default_factory=list, max_length=100)
    artifact_refs: list[str] = Field(default_factory=list, max_length=100)
    reason: str | None = None
    requires_integration: bool = False
    replaces_task_id: str | None = None


class GraphEdge(StrictModel):
    from_id: str = Field(alias="from")
    to_id: str = Field(alias="to")
    type: Literal["DEPENDS_ON"] = "DEPENDS_ON"
    condition: Literal["SUCCESS"] = "SUCCESS"


class HumanRequest(StrictModel):
    question: str = Field(min_length=1, max_length=10000)
    reason: str = "clarification"
    options: list[str] = Field(default_factory=list, max_length=20)


class Update(StrictModel):
    key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$")
    value: Any
    reason: str | None = None


class Notification(StrictModel):
    message: str = Field(min_length=1, max_length=10000)


class TaskGraphOutput(StrictModel):
    """The only decision protocol, for all six agents (including Communicator)."""

    schema_version: Literal["1.0"] = "1.0"
    project_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    source_task_id: str
    producer_agent: Agent
    execution_chain_id: str = Field(min_length=1, max_length=128)
    summary: str = Field(default="", max_length=10000)
    source_result: SourceResult | None = None
    nodes: list[GraphNode] = Field(default_factory=list, max_length=100)
    edges: list[GraphEdge] = Field(default_factory=list, max_length=1000)
    human_request: HumanRequest | None = None
    shared_updates: list[Update] = Field(default_factory=list, max_length=100)
    private_updates: list[Update] = Field(default_factory=list, max_length=100)
    notification: Notification | None = None

    @model_validator(mode="after")
    def unique_nodes(self):
        ids = [n.id for n in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate node IDs")
        return self
