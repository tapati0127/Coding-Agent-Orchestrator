"""Single writer with deterministic IDs and replayable Store write journal.

This is recoverable at-least-once application, not multi-record transactions or
exactly-once execution. Run exactly one Dispatcher for an Agent Server.
"""
import asyncio
import copy
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path

from orchestrator.schemas.task import Task
from orchestrator.schemas.task_graph import Agent, Outcome, Status, TaskGraphOutput
from .dag_scheduler import ready_tasks, resolved_task
from .transitions import transition
from .validator import backward_request, validate_dag, validate_authoritative_dag


def identifier(request_id, suffix):
    return uuid.uuid5(uuid.NAMESPACE_URL, f"qwen-orchestrator:{request_id}:{suffix}").hex


class Dispatcher:
    def __init__(self, store, *, max_qwen_sessions=5, max_execute_workers=5,
                 max_backward=3, max_attempts=3, lease_seconds=600, clock=time.time):
        if not 1 <= max_qwen_sessions <= 5 or not 1 <= max_execute_workers <= 5:
            raise ValueError("Qwen and Execute limits must be 1..5")
        self.store, self.clock = store, clock
        self.max_qwen_sessions, self.max_execute_workers = max_qwen_sessions, max_execute_workers
        self.max_backward, self.max_attempts = max_backward, max_attempts
        self.lease_seconds = lease_seconds
        self.lock = asyncio.Lock()

    async def tasks(self, project_id):
        return {v["task_id"]: Task.model_validate(v)
                for v in await self.store.search(("coding", project_id, "tasks"))}

    async def _apply(self, journal):
        for write in journal["writes"]:
            await self.store.put(tuple(write["ns"]), write["key"], write["value"])
        journal = {**journal, "applied": True}
        await self.store.put(("coding", journal["project_id"], "operations"),
                             journal["request_id"], journal)

    async def replay(self, project_id):
        for journal in await self.store.search(("coding", project_id, "operations")):
            if not journal["applied"]:
                await self._apply(journal)

    async def handle(self, envelope):
        async with self.lock:
            return await self._handle(envelope)

    async def _handle(self, envelope):
        project = envelope.get("project_id", "")
        if not project or len(project) > 100 or any(c not in
                "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in project):
            return {"accepted": False, "reason": "invalid project_id"}
        rid = envelope.get("request_id")
        if not isinstance(rid, str) or not 1 <= len(rid) <= 128:
            return {"accepted": False, "reason": "invalid request_id"}
        agent = str(envelope.get("agent", "COMMUNICATOR"))
        try:
            Agent(agent)
        except ValueError:
            return {"accepted": False, "reason": "invalid agent"}
        await self.replay(project)
        digest = hashlib.sha256(json.dumps(envelope, sort_keys=True).encode()).hexdigest()
        response_ns = ("coding", project, "responses", agent)
        operation = await self.store.get(("coding", project, "operations"), rid)
        if operation and operation.get("fingerprint") != digest:
            rejection = {"accepted": False, "reason": "request_id reused with different content",
                         "request_id": rid, "fingerprint": digest}
            if operation.get("agent") != agent:
                await self.store.put(response_ns, rid, rejection)
            return rejection
        old = await self.store.get(response_ns, rid)
        if old:
            if old.get("fingerprint") != digest:
                return {"accepted": False, "reason": "request_id reused with different content"}
            return {**old, "duplicate": True}
        writes = []

        def put(ns, key, value):
            writes.append({"ns": list(ns), "key": key, "value": copy.deepcopy(value)})

        try:
            receipt = await self._dispatch(envelope, put)
            receipt = {"accepted": True, **receipt}
        except (ValueError, KeyError, TypeError) as exc:
            writes = []
            receipt = {"accepted": False, "reason": str(exc)[:1000]}
        receipt.update(request_id=rid, fingerprint=digest, recorded_at=self.clock())
        # Receipt is last, after every authoritative write. A pending journal is
        # replayed before handling new work after process restart.
        put(response_ns, rid, receipt)
        event = {"event_id": rid, "project_id": project,
                 "kind": envelope.get("kind"), "accepted": receipt["accepted"],
                 "task_id": envelope.get("task_id"), "timestamp": self.clock(),
                 "reason": receipt.get("reason")}
        put(("coding", project, "events"), rid, event)
        journal = {"request_id": rid, "project_id": project,
                   "fingerprint": digest, "agent": agent, "applied": False, "writes": writes}
        await self.store.put(("coding", project, "operations"), rid, journal)
        await self._apply(journal)
        return receipt

    async def _dispatch(self, e, put):
        pid, rid = e["project_id"], e["request_id"]
        kind, payload = e.get("kind"), e.get("payload") or {}
        agent = Agent(e.get("agent", "COMMUNICATOR"))
        ns = ("coding", pid)
        if kind == "register_project":
            if agent != Agent.COMMUNICATOR:
                raise ValueError("only Communicator registers projects")
            if await self.store.get(("coding", "projects"), pid):
                raise ValueError("project already exists")
            path = payload.get("repository_path")
            if path:
                root = Path(os.getenv("WORKSPACE_ROOT", "/workspace")).resolve()
                path = Path(path).resolve()
                if not path.is_relative_to(root) or not (path / ".git").exists():
                    raise ValueError("repository must be an existing Git checkout below WORKSPACE_ROOT")
                path = str(path)
            put(("coding", "projects"), pid, {"project_id": pid,
                "title": str(payload.get("title", pid))[:500], "repository_path": path,
                "created_at": self.clock()})
            return {}
        if not await self.store.get(("coding", "projects"), pid):
            raise ValueError("unknown project")
        tasks = await self.tasks(pid)
        if kind == "human_message":
            if agent != Agent.COMMUNICATOR:
                raise ValueError("only Communicator accepts human messages")
            message = payload.get("message", "").strip()
            if not message or len(message) > 50000:
                raise ValueError("message must be 1..50000 characters")
            graph = TaskGraphOutput(project_id=pid, source_task_id="human",
                producer_agent=Agent.COMMUNICATOR, execution_chain_id=identifier(rid, "chain"),
                summary=message, nodes=[{"id": "analyze", "target_agent": "ANALYZE",
                    "title": "Analyze human request", "description": message}])
            return await self._graph(e, graph, tasks, put)
        if kind == "approval":
            if agent != Agent.COMMUNICATOR:
                raise ValueError("only Communicator submits human decisions")
            approval_id = payload.get("approval_id")
            approval = await self.store.get((*ns, "approvals"), approval_id)
            if not approval or approval["status"] != "PENDING":
                raise ValueError("approval is missing or already resolved")
            decision = payload.get("decision")
            if decision not in ("approve", "reject"):
                raise ValueError("invalid approval decision")
            approval.update(status="APPROVED" if decision == "approve" else "REJECTED",
                            answer=str(payload.get("answer", ""))[:10000])
            put((*ns, "approvals"), approval_id, approval)
            if decision == "reject":
                return {"approval_id": approval_id}
            # Human approval starts a new chain, preserving the prior budget and
            # history; the previous chain is never silently reset.
            graph = TaskGraphOutput(project_id=pid, source_task_id="human",
                producer_agent=Agent.COMMUNICATOR, execution_chain_id=identifier(rid, "chain"),
                summary="Human clarification", nodes=[{"id": "resume",
                    "target_agent": "ANALYZE", "title": "Analyze human decision",
                    "description": f"Original request: {approval.get('context', '')}\n"
                                   f"Question: {approval['question']}\nAnswer: {approval['answer']}"}])
            return await self._graph(e, graph, tasks, put)
        if kind == "claim":
            worker = e.get("worker_id")
            if not isinstance(worker, str) or not worker:
                raise ValueError("worker_id required")
            all_tasks = []
            for project in await self.store.search(("coding", "projects")):
                all_tasks.extend((await self.tasks(project["project_id"])).values())
            active = [t for t in all_tasks if t.status in (Status.DOING, Status.IN_REVIEW)]
            if any(t.assigned_worker == worker for t in active):
                raise ValueError("worker already owns a task")
            if len(active) >= self.max_qwen_sessions:
                raise ValueError("global Qwen capacity exhausted")
            if agent == Agent.EXECUTE and sum(t.agent == Agent.EXECUTE for t in active) >= self.max_execute_workers:
                raise ValueError("Execute capacity exhausted")
            ready = ready_tasks(tasks, agent)
            if not ready:
                raise ValueError("no_ready_task")
            task = ready[0]
            transition(task, Status.DOING)
            task.assigned_worker, task.lease_expires_at = worker, self.clock() + self.lease_seconds
            task.attempt += 1
            put((*ns, "tasks"), task.task_id, task.model_dump(mode="json"))
            put((*ns, "workers"), worker, {"worker_id": worker, "agent": agent.value,
                "task_id": task.task_id, "status": "BUSY", "last_heartbeat": self.clock()})
            return {"task": task.model_dump(mode="json")}
        if kind not in ("heartbeat", "result", "failure"):
            raise ValueError("unsupported request kind")
        task = tasks.get(e.get("task_id"))
        if (not task or task.agent != agent or task.assigned_worker != e.get("worker_id")
                or task.status not in (Status.DOING, Status.IN_REVIEW)):
            raise ValueError("task is not active and owned by this worker/agent")
        if task.lease_expires_at is not None and task.lease_expires_at <= self.clock():
            raise ValueError("task lease expired; recovery required")
        if kind == "heartbeat":
            task.lease_expires_at = self.clock() + self.lease_seconds
            for name in ("worktree_path", "base_commit", "branch", "thread_id", "qwen_session_id"):
                if name in payload:
                    setattr(task, name, payload[name])
            if agent == Agent.VERIFY_INTEGRATE and task.status == Status.DOING:
                transition(task, Status.IN_REVIEW)
            task.version += 1
            put((*ns, "tasks"), task.task_id, task.model_dump(mode="json"))
            put((*ns, "workers"), task.assigned_worker, {"worker_id": task.assigned_worker,
                "agent": agent.value, "task_id": task.task_id, "status": "BUSY",
                "last_heartbeat": self.clock(), "worktree_path": task.worktree_path,
                "progress": payload.get("progress")})
            return {}
        if kind == "failure":
            # Do not release immediately: the failed process may still be alive.
            # Keep the lease until expiry, then retry through recovery.
            put((*ns, "workers"), task.assigned_worker, {"worker_id": task.assigned_worker,
                "agent": agent.value, "task_id": task.task_id, "status": "FAILED",
                "last_heartbeat": self.clock(), "error": "worker run failed; awaiting lease recovery"})
            return {}
        graph = TaskGraphOutput.model_validate(payload)
        if (graph.project_id != pid or graph.source_task_id != task.task_id
                or graph.producer_agent != agent or graph.execution_chain_id != task.execution_chain_id):
            raise ValueError("result identity does not match claimed task")
        return await self._graph(e, graph, tasks, put)

    async def _graph(self, e, graph, tasks, put):
        pid, rid = graph.project_id, e["request_id"]
        ns = ("coding", pid)
        source = tasks.get(graph.source_task_id)
        if source is None and not (graph.producer_agent == Agent.COMMUNICATOR and graph.source_task_id == "human"):
            raise ValueError("unknown source task")
        if source and graph.source_result is None:
            raise ValueError("source result required")
        validate_dag(graph, tasks)
        back = backward_request(graph)
        chain = await self.store.get((*ns, "chains"), graph.execution_chain_id) or {
            "execution_chain_id": graph.execution_chain_id, "backward_count": 0}
        overflow = back and chain["backward_count"] >= self.max_backward
        if back and not overflow:
            chain["backward_count"] += 1
        if graph.shared_updates or graph.private_updates:
            self._validate_updates(graph)
        if source:
            result = graph.source_result
            if (result.proposed_status == "CLOSED" or result.close_task_ids) and graph.producer_agent != Agent.FINALIZE:
                raise ValueError("only Finalize may request closure")
            if result.outcome == Outcome.SUCCESS and source.agent == Agent.EXECUTE and not result.commit_sha:
                raise ValueError("successful Execute requires a captured commit")
            if source.agent == Agent.EXECUTE and result.outcome == Outcome.SUCCESS:
                await self._validate_commit(pid, result.commit_sha)
            if result.integrated_task_ids or result.baseline_commit:
                if source.agent != Agent.VERIFY_INTEGRATE or result.outcome != Outcome.SUCCESS:
                    raise ValueError("only successful Verify may report integration")
                await self._validate_integration(pid, result, tasks)
                for task_id in result.integrated_task_ids:
                    t = tasks[task_id]
                    t.integrated = True
                    t.version += 1
                    put((*ns, "tasks"), t.task_id, t.model_dump(mode="json"))
                project = await self.store.get(("coding", "projects"), pid)
                project["baseline_commit"] = result.baseline_commit
                put(("coding", "projects"), pid, project)
            if source.agent == Agent.VERIFY_INTEGRATE and source.status == Status.DOING:
                transition(source, Status.IN_REVIEW)
            transition(source, Status.DONE)
            source.outcome, source.result_refs = result.outcome, result.artifact_refs
            source.commit_sha = result.commit_sha
            source.lease_expires_at = None
            if source.assigned_worker:
                put((*ns, "workers"), source.assigned_worker, {"worker_id": source.assigned_worker,
                    "agent": source.agent.value, "status": "IDLE", "task_id": None,
                    "last_heartbeat": self.clock()})
            if result.proposed_status == "CLOSED" or result.close_task_ids:
                if result.outcome != Outcome.SUCCESS or graph.nodes:
                    raise ValueError("closure requires successful final acceptance and no new tasks")
                close = set(result.close_task_ids) | {source.task_id}
                chain_tasks = {t.task_id for t in tasks.values()
                               if t.execution_chain_id == source.execution_chain_id}
                if close != chain_tasks:
                    raise ValueError("Finalize must close the entire accepted execution chain")
                for tid in close:
                    t = tasks.get(tid)
                    if not t or t.status != Status.DONE:
                        raise ValueError("only accepted DONE tasks may close")
                    superseded = t.outcome != Outcome.SUCCESS and any(
                        (replacement.caused_by_task_id == tid or t.replaced_by_task_id == replacement.task_id) and replacement.reason
                        and replacement.outcome == Outcome.SUCCESS
                        and replacement.status == Status.DONE
                        and (replacement.agent != Agent.EXECUTE or replacement.integrated)
                        for replacement in tasks.values() if replacement.task_id in close)
                    resolved = resolved_task(t, tasks)
                    if t.replaced_by_task_id and resolved:
                        superseded = (resolved.task_id in close and resolved.reason
                            and resolved.outcome == Outcome.SUCCESS and resolved.status == Status.DONE
                            and (resolved.agent != Agent.EXECUTE or resolved.integrated))
                    if t.outcome != Outcome.SUCCESS and not superseded:
                        raise ValueError("non-successful task requires an accepted rework replacement")
                    if t.agent == Agent.EXECUTE and t.outcome == Outcome.SUCCESS and not t.integrated:
                        raise ValueError("Execute task must be integrated before closure")
                approvals = await self.store.search((*ns, "approvals"))
                if any(a.get("execution_chain_id") == source.execution_chain_id
                       and a.get("status") == "PENDING" for a in approvals):
                    raise ValueError("execution chain has pending human decisions")
                for tid in close:
                    t = tasks[tid]
                    transition(t, Status.CLOSED, finalize=True)
                    put((*ns, "tasks"), t.task_id, t.model_dump(mode="json"))
                put((*ns, "notifications"), rid, {"message": graph.summary or "Execution chain complete",
                    "execution_chain_id": graph.execution_chain_id, "timestamp": self.clock()})
            put((*ns, "tasks"), source.task_id, source.model_dump(mode="json"))
            self._publish_artifacts(put, ns, source, result.artifact_refs)
        put((*ns, "chains"), graph.execution_chain_id, chain)
        created = []
        if not overflow:
            plans = await self.store.search((*ns, "plans"))
            version = max((p["plan_version"] for p in plans
                           if p["execution_chain_id"] == graph.execution_chain_id), default=0) + 1
            plan_id = identifier(graph.execution_chain_id, "plan")
            mapping = {node.id: identifier(rid, node.id) for node in graph.nodes if node.node_type == "TASK"}
            replacements = set()
            for node in graph.nodes:
                if node.replaces_task_id:
                    old = tasks.get(node.replaces_task_id)
                    if (node.node_type != "TASK" or not old or old.status != Status.DONE
                            or old.outcome == Outcome.SUCCESS or old.replaced_by_task_id
                            or old.agent != node.target_agent
                            or old.execution_chain_id != graph.execution_chain_id
                            or not node.reason or old.task_id in replacements):
                        raise ValueError("replacement must identify an unresolved DONE non-success task of same role/chain, with reason")
                    if any(old.task_id in t.depends_on and t.status in (Status.DOING, Status.IN_REVIEW)
                           for t in tasks.values()):
                        raise ValueError("replacement cannot alter active task dependencies")
                    replacements.add(old.task_id)
                    old.replaced_by_task_id = mapping[node.id]
                    old.version += 1
                    put((*ns, "tasks"), old.task_id, old.model_dump(mode="json"))
            for node in graph.nodes:
                if node.node_type != "TASK":
                    if node.node_type == "HUMAN_APPROVAL":
                        self._approval(put, ns, rid + node.id, graph, node.description or node.title)
                    else:
                        put((*ns, "notifications"), identifier(rid, node.id), {"message": node.description or node.title,
                            "timestamp": self.clock()})
                    continue
                deps = [mapping.get(edge.from_id, edge.from_id) for edge in graph.edges if edge.to_id == node.id]
                # Source result must succeed before its proposed forward work can
                # execute, except explicit backward/rework tasks.
                if source and not back and not deps:
                    deps.append(source.task_id)
                task = Task(task_id=mapping[node.id], project_id=pid, plan_id=plan_id,
                    plan_version=version, agent=node.target_agent, title=node.title,
                    description=node.description, acceptance_criteria=node.acceptance_criteria,
                    context_refs=node.context_refs, parent_task_id=source.task_id if source else None,
                    caused_by_task_id=source.task_id if source else None,
                    root_task_id=source.root_task_id if source else mapping[node.id],
                    execution_chain_id=graph.execution_chain_id, depends_on=deps,
                    priority=node.priority, max_attempts=self.max_attempts,
                    backward_count=chain["backward_count"], requires_integration=node.requires_integration,
                    result_refs=node.artifact_refs, reason=node.reason)
                put((*ns, "tasks"), task.task_id, task.model_dump(mode="json"))
                tasks[task.task_id] = task
                self._publish_artifacts(put, ns, task, node.artifact_refs)
                created.append(task.task_id)
            validate_authoritative_dag(tasks)
            if mapping:
                put((*ns, "plans"), f"{plan_id}:{version}", {"plan_id": plan_id,
                    "plan_version": version, "execution_chain_id": graph.execution_chain_id,
                    "summary": graph.summary, "task_ids": created,
                    "edges": [{"from": mapping.get(x.from_id, x.from_id), "to": mapping[x.to_id],
                               "condition": x.condition} for x in graph.edges]})
        if overflow or graph.human_request:
            self._approval(put, ns, rid, graph,
                "Backward delegation budget exceeded. Decide next action." if overflow else graph.human_request.question)
        for update in graph.shared_updates:
            if any(p in (".", "..") for p in update.key.split("/")):
                raise ValueError("shared promotion key has invalid path components")
            put((*ns, "shared", "context"), update.key, {"key": update.key, "value": update.value,
                "source_task_id": graph.source_task_id, "reason": update.reason})
        for update in graph.private_updates:
            if not source or not source.thread_id:
                raise ValueError("private update requires owned thread")
            private_ns = (*ns, "private", graph.producer_agent.value)
            if graph.producer_agent == Agent.EXECUTE:
                private_ns += (source.assigned_worker,)
            put((*private_ns, source.thread_id), update.key, {"key": update.key, "value": update.value})
        if graph.notification:
            put((*ns, "notifications"), rid, {"message": graph.notification.message, "timestamp": self.clock()})
        return {"task_ids": created, "human_intervention": bool(overflow),
                "delegation_accepted": not bool(overflow)}

    def _approval(self, put, ns, key, graph, question):
        aid = identifier(key, "approval")
        put((*ns, "approvals"), aid, {"approval_id": aid, "project_id": graph.project_id,
            "question": question, "status": "PENDING", "source_task_id": graph.source_task_id,
            "execution_chain_id": graph.execution_chain_id, "context": graph.summary,
            "created_at": self.clock()})

    @staticmethod
    def _validate_updates(graph):
        text = json.dumps([u.model_dump() for u in graph.shared_updates + graph.private_updates])
        if len(text.encode()) > 65536:
            raise ValueError("large updates must use artifact references")
        for name in ("QWEN_API_KEY", "ORCHESTRATOR_TOKEN", "DASHBOARD_TOKEN"):
            secret = os.getenv(name)
            if secret and secret in text:
                raise ValueError("secrets cannot be stored in context")
        for update in graph.shared_updates:
            if any(word in update.key.lower() for word in ("secret", "token", "api_key", "private")):
                raise ValueError("shared promotion rejected by scope policy")
            if not update.reason:
                raise ValueError("shared promotion requires an explicit reason")

    @staticmethod
    def _publish_artifacts(put, ns, task, refs):
        root = Path(os.getenv("WORKSPACE_ROOT", "/workspace")).resolve() / "artifacts"
        for ref in refs:
            key = ref.removeprefix("artifact://")
            parts = key.split("/")
            if (not ref.startswith("artifact://") or len(parts) < 3
                    or parts[0] != task.project_id or parts[1] not in ("patches", "reports", "plans")
                    or any(x in ("", ".", "..") for x in parts)):
                raise ValueError("only project-scoped public artifact references may be published")
            record = {"artifact_id": key, "ref": ref, "task_id": task.task_id,
                      "kind": parts[1], "title": parts[-1], "commit_sha": task.commit_sha}
            path = root.joinpath(*parts)
            if path.is_file() and path.resolve().is_relative_to(root / task.project_id) and path.stat().st_size <= 65536:
                content = path.read_text(encoding="utf-8", errors="replace")
                for name in ("QWEN_API_KEY", "ORCHESTRATOR_TOKEN", "DASHBOARD_TOKEN"):
                    secret = os.getenv(name)
                    if secret:
                        content = content.replace(secret, "[REDACTED]")
                record.update(content=content, media_type="text/plain")
            put((*ns, "shared", "artifacts"), key, record)

    async def _validate_commit(self, pid, commit):
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
            raise ValueError("Execute commit must be a full Git commit SHA")
        project = await self.store.get(("coding", "projects"), pid)
        path = project.get("repository_path")
        if not path:
            raise ValueError("Execute requires a registered repository")
        proc = await asyncio.create_subprocess_exec("git", "-C", path, "cat-file", "-e", commit + "^{commit}",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        if await proc.wait() != 0:
            raise ValueError("Execute commit is not present in the registered repository")

    async def _validate_integration(self, pid, result, tasks):
        project = await self.store.get(("coding", "projects"), pid)
        path = project.get("repository_path")
        if not path or not result.baseline_commit:
            raise ValueError("integration requires a registered repository and baseline commit")
        for tid in result.integrated_task_ids:
            t = tasks.get(tid)
            if not t or t.agent != Agent.EXECUTE or t.outcome != Outcome.SUCCESS or not t.commit_sha:
                raise ValueError("integration references an unsuccessful Execute task")
            # Cherry-pick changes SHA; compare stable patch IDs rather than
            # requiring the original commit to be an ancestor.
            from orchestrator.runtime.worktree import WorkspaceManager
            checker = getattr(WorkspaceManager, "verify_integrated", None)
            if checker is not None:
                if not await asyncio.to_thread(checker, path, t.commit_sha, result.baseline_commit):
                    raise ValueError("commit not proven integrated into baseline")
            else:
                proc = await asyncio.create_subprocess_exec("git", "-C", path, "merge-base",
                    "--is-ancestor", t.commit_sha, result.baseline_commit,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                if await proc.wait() != 0:
                    raise ValueError("commit not proven integrated into baseline")

    async def recover(self, project_id):
        async with self.lock:
            await self.replay(project_id)
            for task in (await self.tasks(project_id)).values():
                if (task.status in (Status.DOING, Status.IN_REVIEW)
                        and (task.lease_expires_at or 0) <= self.clock()):
                    # Reuse the journal mechanism, preserving counters/history.
                    rid = f"recovery-{task.task_id}-{task.attempt}"
                    writes = []
                    if task.attempt < task.max_attempts:
                        transition(task, Status.TODO, recovery=True)
                    else:
                        transition(task, Status.DONE)
                        task.outcome = Outcome.FAILED
                    worker = task.assigned_worker
                    task.assigned_worker, task.lease_expires_at = None, None
                    writes.append({"ns": ["coding", project_id, "tasks"], "key": task.task_id,
                                   "value": task.model_dump(mode="json")})
                    if worker:
                        writes.append({"ns": ["coding", project_id, "workers"], "key": worker,
                            "value": {"worker_id": worker, "agent": task.agent.value,
                                      "status": "RECOVERING", "task_id": None}})
                    writes.append({"ns": ["coding", project_id, "events"], "key": rid,
                        "value": {"event_id": rid, "kind": "lease_recovery", "task_id": task.task_id,
                                  "timestamp": self.clock(), "attempt": task.attempt,
                                  "status": task.status.value}})
                    journal = {"request_id": rid, "project_id": project_id, "applied": False, "writes": writes}
                    await self.store.put(("coding", project_id, "operations"), rid, journal)
                    await self._apply(journal)
