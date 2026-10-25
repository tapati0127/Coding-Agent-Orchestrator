"""Validate queue messages and dispatch task/context snapshots to worker processes."""

import asyncio
import logging
from queue import Empty
import time
from uuid import uuid4

from orchestrator.schemas.task import Task
from orchestrator.schemas.task_graph import Status
from .context import build_job
from .dag_scheduler import ready_tasks

log = logging.getLogger(__name__)


class DispatcherLoop:
    def __init__(self, engine, events, inboxes, roles, *, interval=2.):
        self.engine, self.events = engine, events
        self.inboxes, self.roles = inboxes, roles
        self.interval = interval

    async def handle(self, event):
        receipt = await self.engine.handle(event)
        if event.get("kind") == "result" and not receipt["accepted"]:
            log.warning("Task result rejected: task=%s worker=%s", event.get("task_id"), event.get("worker_id"))
            await self.engine.handle({**event, "request_id": uuid4().hex, "kind": "failure",
                                      "payload": {"error": "result_rejected"}})

    async def schedule(self):
        projects = await self.engine.store.search(("coding", "projects"))
        task_sets = {project["project_id"]: await self.engine.tasks(project["project_id"]) for project in projects}
        busy = {task.assigned_worker for tasks in task_sets.values() for task in tasks.values()
                if task.status in (Status.DOING, Status.IN_REVIEW)}
        for worker_id, agent in self.roles.items():
            if worker_id in busy:
                continue
            for project in projects:
                tasks = task_sets[project["project_id"]]
                ready = ready_tasks(tasks, agent)
                if not ready:
                    continue
                receipt = await self.engine.handle({"request_id": uuid4().hex,
                    "project_id": project["project_id"], "agent": agent, "worker_id": worker_id,
                    "kind": "claim", "task_id": ready[0].task_id, "payload": {}})
                if not receipt["accepted"]:
                    continue
                task = receipt["task"]
                tasks[task["task_id"]] = Task.model_validate(task)
                try:
                    job = await build_job(self.engine.store, project, task, worker_id, self.engine.max_backward)
                    await asyncio.to_thread(self.inboxes[worker_id].put, job)
                    log.info("Task dispatched: project=%s task=%s agent=%s worker=%s",
                             task["project_id"], task["task_id"], agent, worker_id)
                except Exception as exc:
                    log.warning("Cannot prepare task %s (%s)", task["task_id"], type(exc).__name__)
                    await self.engine.handle({"request_id": uuid4().hex, "project_id": task["project_id"],
                        "agent": agent, "worker_id": worker_id, "task_id": task["task_id"],
                        "task_attempt": task["attempt"], "kind": "failure", "payload": {"error": "context_failed"}})
                break

    async def recover(self):
        for project in await self.engine.store.search(("coding", "projects")):
            await self.engine.recover(project["project_id"])

    async def run(self, *, on_ready=None):
        await self.recover()
        await self.schedule()
        if on_ready:
            on_ready()
        recovered_at = time.monotonic()
        while True:
            try:
                event = await asyncio.to_thread(self.events.get, True, self.interval)
            except Empty:
                event = {}
            if event is None:
                return
            if event:
                await self.handle(event)
            if time.monotonic() - recovered_at >= self.interval:
                await self.recover()
                recovered_at = time.monotonic()
            await self.schedule()
