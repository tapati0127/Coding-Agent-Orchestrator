"""Independent agent worker process; all task changes go through request inboxes."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
from pathlib import Path

from langgraph_sdk import get_client

from orchestrator.runtime.context_scope import ScopedContext, identifier
from orchestrator.runtime.worktree import WorkspaceManager
from orchestrator.store import SDKStore
from orchestrator.workers.graph_runner import run_graph
from orchestrator.workers.polling import ReceiptTimeout, submit_request

log = logging.getLogger(__name__)
AGENTS = ("COMMUNICATOR", "ANALYZE", "PLAN_DELEGATE", "EXECUTE", "VERIFY_INTEGRATE", "FINALIZE")


class Worker:
    def __init__(self, agent: str, worker_id: str, store=None, client=None, workspace=None):
        if agent not in AGENTS:
            raise ValueError("Unknown worker agent")
        self.agent, self.worker_id = agent, worker_id
        url = os.getenv("LANGGRAPH_URL", "http://127.0.0.1:2024")
        self.store = store or SDKStore(url=url)
        self.client = client or get_client(url=url, api_key=os.getenv("ORCHESTRATOR_TOKEN"))
        self.workspace = workspace or WorkspaceManager(
            os.getenv("WORKSPACE_ROOT", "/workspace"), os.getenv("DATA_ROOT", "/data")
        )
        self.poll_seconds = float(os.getenv("DISPATCH_POLL_SECONDS", "2"))
        self.heartbeat_seconds = min(10, max(0.1, float(os.getenv("TASK_LEASE_SECONDS", "600")) / 3))

    async def request(self, project_id, kind, task_id=None, payload=None, wait=True):
        return await submit_request(
            self.store, project_id, self.agent, self.worker_id,
            kind, task_id=task_id, payload=payload, wait=wait,
        )

    async def heartbeat(self, task: dict, metadata: dict):
        while True:
            receipt = await self.request(task["project_id"], "heartbeat", task["task_id"], metadata)
            if not receipt.get("accepted"):
                raise RuntimeError("Dispatcher rejected worker heartbeat ownership")
            await asyncio.sleep(self.heartbeat_seconds)

    async def prepare(self, project: dict, task: dict) -> tuple[dict, list[dict]]:
        project_id = project["project_id"]
        all_tasks = await self.store.search(("coding", project_id, "tasks"), limit=10000)
        by_id = {item["task_id"]: item for item in all_tasks}
        predecessors, visited = [], set()

        def visit(task_id):
            if task_id in visited:
                return
            visited.add(task_id)
            item = by_id.get(task_id)
            if item:
                for dependency in item.get("depends_on", []):
                    visit(dependency)
                predecessors.append(item)

        for task_id in task.get("depends_on", []):
            visit(task_id)
        repository = project.get("repository_path")
        if not repository:
            if self.agent not in {"COMMUNICATOR", "ANALYZE", "PLAN_DELEGATE"}:
                raise ValueError("This agent requires a registered project repository")
            data_root = Path(os.getenv("DATA_ROOT", "/data")).resolve()
            cwd = data_root / "sessions" / identifier(project_id) / identifier(task["task_id"])
            if not cwd.resolve().is_relative_to(data_root):
                raise ValueError("Session directory escapes DATA_ROOT")
            cwd.mkdir(parents=True, exist_ok=True)
            info = {"worktree_path": str(cwd), "base_commit": None}
            task.update(info)
            return info, predecessors
        await asyncio.to_thread(self.workspace.register, project_id, repository)
        if self.agent == "EXECUTE":
            info = await asyncio.to_thread(
                self.workspace.provision,
                project_id, self.worker_id,
                project.get("baseline_commit"), task_id=task["task_id"],
            )
        elif self.agent == "VERIFY_INTEGRATE":
            commits = list(dict.fromkeys(
                p["commit_sha"] for p in predecessors
                if p.get("agent") == "EXECUTE" and p.get("commit_sha")
            ))
            info = await asyncio.to_thread(self.workspace.integrate, project_id, commits)
            # A conflict is part of Verify's input; leave it intact for Qwen to
            # review/fix and report. A DIRTY baseline must also remain visible.
            if info.get("status") != "SUCCESS":
                task["integration_problem"] = {"status": info.get("status"), "conflicts": info.get("conflicts", [])}
        elif self.agent == "FINALIZE":
            info = await asyncio.to_thread(self.workspace.provision, project_id, "integration")
        else:
            info = {"worktree_path": repository, "base_commit": project.get("baseline_commit")}
        task.update({key: info[key] for key in ("worktree_path", "base_commit", "branch") if key in info})
        return info, predecessors

    async def execute(self, project: dict, task: dict):
        metadata = {"phase": "preparing"}
        heartbeat = asyncio.create_task(self.heartbeat(task, metadata))
        run = None
        try:
            info, predecessors = await self.prepare(project, task)
            thread = await self.client.threads.create()
            thread_id = thread["thread_id"]
            metadata.update({key: info[key] for key in ("worktree_path", "base_commit", "branch") if key in info})
            metadata.update({"thread_id": thread_id, "phase": "review" if self.agent == "VERIFY_INTEGRATE" else "running"})
            initial = await self.request(task["project_id"], "heartbeat", task["task_id"], metadata)
            if not initial.get("accepted"):
                raise RuntimeError("Dispatcher rejected task preparation metadata")
            context = await ScopedContext(self.store).build(
                project_id=task["project_id"], agent=self.agent, thread_id=thread_id,
                worker_id=self.worker_id, context_refs=task.get("context_refs", []),
            )
            # Dependency results are public task/artifact metadata, not private
            # graph state. Verify needs these to evaluate integration acceptance.
            context["predecessors"] = [
                {key: predecessor.get(key) for key in ("task_id", "outcome", "commit_sha", "result_refs", "title")}
                for predecessor in predecessors
            ]
            chain_tasks = await self.store.search(("coding", task["project_id"], "tasks"), limit=10000)
            context["chain_tasks"] = [
                {key: chain_task.get(key) for key in (
                    "task_id", "agent", "status", "outcome", "depends_on", "title",
                    "acceptance_criteria", "result_refs", "commit_sha", "integrated",
                    "reason", "replaced_by_task_id", "caused_by_task_id",
                )}
                for chain_task in chain_tasks
                if chain_task.get("execution_chain_id") == task["execution_chain_id"]
            ]

            async def progress(value):
                if isinstance(value, dict) and value.get("type") == "qwen_progress":
                    metadata["progress"] = {"chunks": value.get("chunks", 0), "bytes": value.get("bytes", 0)}

            run = asyncio.create_task(run_graph(
                self.client, self.agent, thread_id, task, context, info["worktree_path"], progress
            ))
            done, _ = await asyncio.wait({run, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
            if heartbeat in done:
                # Ownership/transport failure must stop the server run; SDK stream
                # uses on_disconnect=cancel, and Qwen runtime kills process groups.
                await heartbeat
                raise RuntimeError("Worker heartbeat stopped unexpectedly")
            output = await run
            if self.agent in {"EXECUTE", "VERIFY_INTEGRATE"}:
                capture = await asyncio.to_thread(
                    self.workspace.capture, task["project_id"],
                    self.worker_id if self.agent == "EXECUTE" else "integration", task["task_id"],
                )
                source_result = output.get("source_result")
                if isinstance(source_result, dict):
                    source_result["commit_sha"] = capture["commit_sha"]
                    source_result.setdefault("artifact_refs", []).append(capture["patch_ref"])
                    if self.agent == "VERIFY_INTEGRATE" and source_result.get("outcome") == "SUCCESS":
                        source_result["baseline_commit"] = capture["commit_sha"]
                        source_result["integrated_task_ids"] = [
                            p["task_id"] for p in predecessors
                            if p.get("agent") == "EXECUTE" and p.get("commit_sha")
                        ]
            receipt = await self.request(task["project_id"], "result", task["task_id"], output)
            if not receipt.get("accepted"):
                log.warning("Dispatcher rejected result for task %s", task["task_id"])
                await self.request(task["project_id"], "failure", task["task_id"], {"error": "result_rejected"})
        except asyncio.CancelledError:
            raise
        except ReceiptTimeout:
            # A claim/result may have been applied. Exit for lease reconciliation
            # rather than submitting a second, inconsistent result.
            raise
        except Exception as exc:
            log.warning("Worker task %s failed (%s)", task["task_id"], type(exc).__name__)
            if run is not None and not run.done():
                run.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await run
            if self.agent == "EXECUTE" and task.get("worktree_path"):
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self.workspace.capture, task["project_id"], self.worker_id, task["task_id"])
            await self.request(task["project_id"], "failure", task["task_id"], {"error": type(exc).__name__})
        finally:
            for future in (run, heartbeat):
                if future is not None and not future.done():
                    future.cancel()
                if future is not None:
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await future

    async def serve(self):
        while True:
            projects = await self.store.search(("coding", "projects"), limit=1000)
            claimed = False
            for project in projects:
                receipt = await self.request(project["project_id"], "claim")
                if receipt.get("accepted") and receipt.get("task"):
                    claimed = True
                    await self.execute(project, receipt["task"])
            if not claimed:
                await asyncio.sleep(self.poll_seconds)


def main():
    parser = argparse.ArgumentParser(description="Run one independent agent worker")
    parser.add_argument("--agent", required=True, choices=AGENTS)
    parser.add_argument("--worker-id", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    asyncio.run(Worker(args.agent, args.worker_id).serve())


if __name__ == "__main__":
    main()
