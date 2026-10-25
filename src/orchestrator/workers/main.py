"""A worker blocks on a Python queue and executes local graphs, without Store access."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path
import signal
import time
from uuid import uuid4

from orchestrator.runtime.context_scope import identifier
from orchestrator.runtime.worktree import WorkspaceManager
from orchestrator.workers.graph_runner import run_graph

log = logging.getLogger(__name__)
AGENTS = ("COMMUNICATOR", "ANALYZE", "PLAN_DELEGATE", "EXECUTE", "VERIFY_INTEGRATE", "FINALIZE")


class Worker:
    def __init__(self, agent, worker_id, events, *, workspace=None, data_root=None, heartbeat_seconds=None):
        if agent not in AGENTS:
            raise ValueError("Unknown worker agent")
        self.agent, self.worker_id, self.events = agent, worker_id, events
        self.data_root = Path(data_root or os.getenv("DATA_ROOT", "/data"))
        self.workspace = workspace or WorkspaceManager(os.getenv("WORKSPACE_ROOT", "/workspace"), self.data_root)
        self.heartbeat_seconds = heartbeat_seconds or min(10, max(0.1, float(os.getenv("TASK_LEASE_SECONDS", "600")) / 3))

    def emit(self, kind, task, payload):
        self.events.put({"request_id": uuid4().hex, "kind": kind, "project_id": task["project_id"],
            "agent": self.agent, "worker_id": self.worker_id, "task_id": task["task_id"],
            "task_attempt": task["attempt"], "payload": payload})

    async def heartbeat(self, task, metadata):
        while True:
            self.emit("heartbeat", task, metadata.copy())
            await asyncio.sleep(self.heartbeat_seconds)

    async def prepare(self, job):
        project, task, predecessors = job["project"], job["task"], job["predecessors"]
        project_id, repository = project["project_id"], project.get("repository_path")
        if not repository or self.agent == "COMMUNICATOR":
            if self.agent not in {"COMMUNICATOR", "ANALYZE", "PLAN_DELEGATE"}:
                raise ValueError("This agent requires a registered project repository")
            cwd = self.data_root.resolve() / "sessions" / identifier(project_id) / identifier(task["task_id"])
            if not cwd.resolve().is_relative_to(self.data_root.resolve()):
                raise ValueError("Session directory escapes DATA_ROOT")
            cwd.mkdir(parents=True, exist_ok=True)
            info = {"worktree_path": str(cwd), "base_commit": None}
        else:
            await asyncio.to_thread(self.workspace.register, project_id, repository)
            if self.agent == "EXECUTE":
                info = await asyncio.to_thread(self.workspace.provision, project_id, self.worker_id,
                                                project.get("baseline_commit"), task_id=task["task_id"])
            elif self.agent == "VERIFY_INTEGRATE":
                commits = list(dict.fromkeys(p["commit_sha"] for p in predecessors
                                              if p.get("agent") == "EXECUTE" and p.get("commit_sha")))
                info = await asyncio.to_thread(self.workspace.integrate, project_id, commits)
                if info.get("status") != "SUCCESS":
                    task["integration_problem"] = {"status": info.get("status"), "conflicts": info.get("conflicts", [])}
            elif self.agent == "FINALIZE":
                info = await asyncio.to_thread(self.workspace.provision, project_id, "integration")
            else:
                info = {"worktree_path": repository, "base_commit": project.get("baseline_commit")}
        task.update({key: info[key] for key in ("worktree_path", "base_commit", "branch") if key in info})
        return info

    async def execute(self, job):
        task = job["task"]
        if task.get("agent") != self.agent:
            raise ValueError("Task agent does not match worker role")
        log.info("Task started: project=%s task=%s agent=%s worker=%s",
                 task["project_id"], task["task_id"], self.agent, self.worker_id)
        metadata = {"phase": "preparing", "thread_id": job["thread_id"]}
        heartbeat = asyncio.create_task(self.heartbeat(task, metadata))
        try:
            info = await self.prepare(job)
            metadata.update({key: info[key] for key in ("worktree_path", "base_commit", "branch") if key in info})
            metadata["phase"] = "review" if self.agent == "VERIFY_INTEGRATE" else "running"
            self.emit("heartbeat", task, metadata.copy())

            async def progress(value):
                if isinstance(value, dict) and value.get("type") == "qwen_progress":
                    metadata["progress"] = {"chunks": value.get("chunks", 0), "bytes": value.get("bytes", 0)}

            output = await run_graph(self.agent, task, job["context"], info["worktree_path"], progress)
            if self.agent in {"EXECUTE", "VERIFY_INTEGRATE"}:
                capture = await asyncio.to_thread(self.workspace.capture, task["project_id"],
                    self.worker_id if self.agent == "EXECUTE" else "integration", task["task_id"])
                source_result = output.get("source_result")
                if isinstance(source_result, dict):
                    source_result["commit_sha"] = capture["commit_sha"]
                    source_result.setdefault("artifact_refs", []).append(capture["patch_ref"])
                    if self.agent == "VERIFY_INTEGRATE" and source_result.get("outcome") == "SUCCESS":
                        source_result["baseline_commit"] = capture["commit_sha"]
                        source_result["integrated_task_ids"] = [
                            p["task_id"] for p in job["predecessors"]
                            if p.get("agent") == "EXECUTE" and p.get("commit_sha")]
            self.emit("result", task, output)
            log.info("Task finished; result sent to Dispatcher: task=%s agent=%s worker=%s",
                     task["task_id"], self.agent, self.worker_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Task failed: task=%s agent=%s error=%s", task["task_id"], self.agent, type(exc).__name__)
            if self.agent == "EXECUTE" and task.get("worktree_path"):
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self.workspace.capture, task["project_id"], self.worker_id, task["task_id"])
            self.emit("failure", task, {"error": type(exc).__name__})
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat


def run_worker(agent, worker_id, inbox, events, settings):
    for name in settings.__dataclass_fields__:
        os.environ[name.upper()] = str(getattr(settings, name))
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    if hasattr(os, "setsid"):
        os.setsid()

    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    worker = Worker(agent, worker_id, events)
    try:
        while True:
            log.info("Waiting for tasks: agent=%s worker=%s", agent, worker_id)
            job = inbox.get()
            if job is None:
                return
            if job["task"]["lease_expires_at"] <= time.time():
                log.warning("Ignoring expired assignment: task=%s worker=%s", job["task"]["task_id"], worker_id)
                continue
            asyncio.run(worker.execute(job))
    except KeyboardInterrupt:
        pass
