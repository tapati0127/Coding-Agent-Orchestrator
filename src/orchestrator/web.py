"""Small dashboard mounted inside Agent Server, never an authoritative task writer.

The browser receives a separate dashboard token.  It cannot use that token with
the Agent Server's built-in Store or graph APIs.  These routes publish only
shared projections and append human requests to the Communicator inbox.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator


PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
STATIC_ROOT = Path(__file__).resolve().parents[2] / "dashboard"
if not STATIC_ROOT.is_dir():
    STATIC_ROOT = Path(__file__).resolve().parent / "dashboard"


def project_id(value: str) -> str:
    if not PROJECT_ID.fullmatch(value):
        raise HTTPException(422, "Invalid project identifier")
    return value


async def authorize(request: Request) -> None:
    """Fail closed without a dashboard token, including local development."""
    expected = os.environ.get("DASHBOARD_TOKEN", "")
    supplied = request.headers.get("authorization", "")
    if not expected:
        raise HTTPException(503, "Set DASHBOARD_TOKEN to enable the dashboard API")
    if not supplied.startswith("Bearer ") or not hmac.compare_digest(
        supplied[7:], expected
    ):
        raise HTTPException(401, "Invalid dashboard token")


def get_store() -> Any:
    from orchestrator.store import SDKStore

    return SDKStore(url=os.environ.get("LANGGRAPH_URL", "http://127.0.0.1:2024"))


Store = Annotated[Any, Depends(get_store)]
app = FastAPI(title="Qwen Orchestrator dashboard", docs_url=None, redoc_url=None)


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class NewProject(StrictInput):
    project_id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=200)
    repository_path: str | None = Field(default=None, max_length=4096)

    @field_validator("project_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not PROJECT_ID.fullmatch(value):
            raise ValueError("Use letters, digits, underscores and hyphens")
        return value

    @field_validator("repository_path")
    @classmethod
    def validate_repository(cls, value: str | None) -> str | None:
        if value is None:
            return None
        root = Path(os.environ.get("WORKSPACE_ROOT", "/workspace")).resolve()
        try:
            resolved = Path(value).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError("Repository must be an existing local directory") from exc
        if not resolved.is_dir() or not resolved.is_relative_to(root):
            raise ValueError("Repository must stay inside WORKSPACE_ROOT")
        return str(resolved)


class Message(StrictInput):
    message: str = Field(min_length=1, max_length=20000)


class ApprovalDecision(StrictInput):
    decision: str = Field(pattern="^(approve|reject)$")
    answer: str = Field(default="", max_length=20000)


async def enqueue(store: Any, pid: str, kind: str, payload: dict) -> dict:
    request_id = str(uuid4())
    await store.put(
        ("coding", pid, "requests", "COMMUNICATOR"),
        request_id,
        {
            "request_id": request_id,
            "kind": kind,
            "project_id": pid,
            "agent": "COMMUNICATOR",
            "payload": payload,
        },
    )
    return {"request_id": request_id, "status": "submitted"}


def public(record: dict, keys: set[str]) -> dict:
    """Explicit allowlist: adding Store fields does not expand browser access."""
    return {key: value for key, value in record.items() if key in keys}


def latest_events(records: list[dict], limit: int = 200) -> list[dict]:
    # Store search order is not event chronology and may change across restarts.
    return sorted(records, key=lambda row: str(row.get("timestamp", row.get("created_at", ""))))[-limit:]


PROJECT_FIELDS = {"project_id", "title", "status", "created_at", "updated_at", "repository_path"}
TASK_FIELDS = {
    "task_id", "project_id", "title", "description", "agent", "status", "outcome",
    "depends_on", "ready", "blocking_reasons", "priority", "attempt", "max_attempts",
    "backward_count", "execution_chain_id", "assigned_worker", "lease_expires_at",
    "worktree_path", "base_commit", "branch", "commit_sha", "result_refs", "artifact_refs",
    "plan_id", "plan_version", "parent_task_id", "root_task_id", "caused_by_task_id",
    "acceptance_criteria", "summary", "review", "final_report", "version",
    "integrated", "requires_integration",
    "replaced_by_task_id",
}
PLAN_FIELDS = {"plan_id", "plan_version", "version", "summary", "nodes", "edges", "created_at"}
EVENT_FIELDS = {"event_id", "id", "kind", "type", "timestamp", "created_at", "task_id", "message", "summary", "request_id", "accepted", "reason", "agent", "worker_id", "notification", "artifact_refs"}
APPROVAL_FIELDS = {"approval_id", "project_id", "source_task_id", "question", "reason", "status", "options", "decision", "answer", "created_at", "resolved_at", "execution_chain_id", "expires_at", "producer_agent"}
WORKER_FIELDS = {"worker_id", "agent", "status", "task_id", "project_id", "worktree_path", "heartbeat_at", "last_heartbeat", "lease_expires_at", "updated_at", "progress"}
ARTIFACT_FIELDS = {"artifact_id", "ref", "title", "kind", "media_type", "summary", "content", "created_at", "task_id", "commit_sha"}


@app.get("/dashboard", include_in_schema=False)
@app.get("/dashboard/", include_in_schema=False)
async def dashboard() -> FileResponse:
    return FileResponse(STATIC_ROOT / "index.html", headers={
        "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
    })


@app.get("/api/projects", dependencies=[Depends(authorize)])
async def projects(store: Store) -> dict:
    rows = await store.search(("coding", "projects"))
    return {"projects": [public(row, PROJECT_FIELDS) for row in rows]}


@app.post("/api/projects", status_code=202, dependencies=[Depends(authorize)])
async def create_project(body: NewProject, store: Store) -> dict:
    return await enqueue(store, body.project_id, "register_project", body.model_dump())


@app.get("/api/projects/{pid}/snapshot", dependencies=[Depends(authorize)])
async def snapshot(pid: str, store: Store) -> dict:
    pid = project_id(pid)
    project = await store.get(("coding", "projects"), pid)
    if project is None:
        raise HTTPException(404, "Project not found")
    tasks, plans, events, approvals, workers, artifacts, notifications, conversations = await asyncio.gather(*(
        store.search(("coding", pid, *parts)) for parts in (
            ("tasks",), ("plans",), ("events",), ("approvals",), ("workers",),
            ("shared", "artifacts"),
            ("notifications",),
            ("conversations",),
        )
    ))
    by_id = {row.get("task_id"): row for row in tasks}
    projected_tasks = []
    for row in tasks:
        task = public(row, TASK_FIELDS)
        blockers = list(task.get("blocking_reasons") or [])
        for dep_id in task.get("depends_on", []):
            dep = by_id.get(dep_id)
            seen = set()
            while dep and dep.get("replaced_by_task_id"):
                if dep.get("task_id") in seen:
                    dep = None
                    break
                seen.add(dep.get("task_id"))
                dep = by_id.get(dep["replaced_by_task_id"])
            if not dep or dep.get("status") not in {"DONE", "CLOSED"} or dep.get("outcome") != "SUCCESS":
                replacement = f" (replacement {dep['task_id']})" if dep and dep.get("task_id") != dep_id else ""
                blockers.append(f"Dependency {dep_id}{replacement} has not succeeded")
            elif dep.get("agent") == "EXECUTE" and task.get("agent") != "VERIFY_INTEGRATE" and not dep.get("integrated"):
                blockers.append(f"Dependency {dep_id} has not been integrated")
        task["blocking_reasons"] = list(dict.fromkeys(blockers))
        # 'ready' is informational; Dispatcher remains the scheduling authority.
        task["ready"] = task.get("status") == "TODO" and not blockers
        projected_tasks.append(task)
    return {
        "project": public(project, PROJECT_FIELDS),
        "tasks": projected_tasks,
        "plans": [public(row, PLAN_FIELDS) for row in plans],
        "events": [public(row, EVENT_FIELDS) for row in latest_events(events)],
        "approvals": [public(row, APPROVAL_FIELDS) for row in approvals],
        "workers": [public(row, WORKER_FIELDS) for row in workers],
        "artifacts": [public(row, ARTIFACT_FIELDS - {"content"}) for row in artifacts],
        "notifications": [public(row, {"message", "timestamp"}) for row in notifications],
        # The authenticated human sees their conversation; this projection is
        # never supplied to agent workers as shared context.
        "conversations": [{"execution_chain_id": row["execution_chain_id"],
            "Messages": [{"type": m["type"], "content": m["data"]["content"]}
                         for m in row.get("Messages", []) if m.get("type") in {"human", "ai"}]}
            for row in sorted(conversations, key=lambda row: row.get("created_at", 0))],
    }


@app.post("/api/projects/{pid}/messages", status_code=202, dependencies=[Depends(authorize)])
async def message(pid: str, body: Message, store: Store) -> dict:
    return await enqueue(store, project_id(pid), "human_message", body.model_dump())


@app.post("/api/projects/{pid}/approvals/{approval_id}", status_code=202, dependencies=[Depends(authorize)])
async def approval(pid: str, approval_id: str, body: ApprovalDecision, store: Store) -> dict:
    pid = project_id(pid)
    project_id(approval_id)
    record = await store.get(("coding", pid, "approvals"), approval_id)
    if record is None:
        raise HTTPException(404, "Approval not found")
    return await enqueue(store, pid, "approval", {"approval_id": approval_id, **body.model_dump()})


@app.get("/api/projects/{pid}/artifacts/{artifact_id}", dependencies=[Depends(authorize)])
async def artifact(pid: str, artifact_id: str, store: Store) -> dict:
    pid = project_id(pid)
    project_id(artifact_id)
    record = await store.get(("coding", pid, "shared", "artifacts"), artifact_id)
    if record is None:
        raise HTTPException(404, "Published artifact not found")
    # Only explicitly shared content, never resolve a filesystem path supplied by a client.
    return public(record, ARTIFACT_FIELDS)


@app.get("/api/projects/{pid}/events", dependencies=[Depends(authorize)])
async def events(pid: str, request: Request, store: Store) -> StreamingResponse:
    pid = project_id(pid)

    async def stream():
        seen: set[str] = set()
        # A bounded connection avoids retaining memory on long-running browser sessions.
        for _ in range(300):
            if await request.is_disconnected():
                return
            rows = await store.search(("coding", pid, "events"))
            notifications = await store.search(("coding", pid, "notifications"))
            rows += [
                {"kind": "notification", **public(row, {"message", "timestamp"})}
                for row in notifications
            ]
            for row in latest_events(rows):
                projection = public(row, EVENT_FIELDS)
                key = json.dumps(projection, sort_keys=True, default=str)
                if key not in seen:
                    seen.add(key)
                    yield f"data: {json.dumps(projection, default=str)}\n\n"
            if len(seen) > 2000:
                seen.clear()
            yield ": heartbeat\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


if STATIC_ROOT.is_dir():
    app.mount("/dashboard/assets", StaticFiles(directory=STATIC_ROOT), name="dashboard-assets")
