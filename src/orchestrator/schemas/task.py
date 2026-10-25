from pydantic import Field

from .task_graph import Agent, Outcome, Status, StrictModel


class Task(StrictModel):
    task_id: str
    project_id: str
    plan_id: str
    plan_version: int = 1
    agent: Agent
    title: str
    description: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    context_refs: list[str] = Field(default_factory=list)
    parent_task_id: str | None = None
    root_task_id: str
    execution_chain_id: str
    caused_by_task_id: str | None = None
    replaced_by_task_id: str | None = None
    reason: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    status: Status = Status.TODO
    outcome: Outcome | None = None
    priority: int = 2
    attempt: int = 0
    max_attempts: int = 3
    backward_count: int = 0
    assigned_worker: str | None = None
    lease_expires_at: float | None = None
    qwen_session_id: str | None = None
    thread_id: str | None = None
    worktree_path: str | None = None
    base_commit: str | None = None
    branch: str | None = None
    commit_sha: str | None = None
    integrated: bool = False
    requires_integration: bool = False
    result_refs: list[str] = Field(default_factory=list)
    version: int = 1
