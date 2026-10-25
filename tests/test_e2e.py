"""Opt-in real Agent Server/supervisor smoke, with a deterministic CLI fixture.

Run: RUN_SERVER_TESTS=1 uv run pytest tests/test_e2e.py -q
This proves graceful same-volume restart, not abrupt-crash durability or a real
LLM's behavior. It needs a free local port 2024 and the installed pinned stack.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
from uuid import uuid4

import httpx
import pytest
from langgraph_sdk import get_sync_client

from fixtures.decision_fixture import decision_endpoint
from orchestrator.dispatcher.engine import identifier


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_SERVER_TESTS") != "1",
    reason="Opt-in smoke starts the real Agent Server on local port 2024",
)
ROOT = Path(__file__).resolve().parents[1]
URL = "http://127.0.0.1:2024"


def wait_for(check, *, timeout=45, description="condition", process=None, logfile=None):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise AssertionError(f"Supervisor exited {process.returncode}:\n{logfile.read_text()[-12000:]}")
        try:
            result = check()
            if result:
                return result
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            last_error = type(exc).__name__
        time.sleep(0.15)
    detail = logfile.read_text()[-12000:] if logfile and logfile.exists() else ""
    raise AssertionError(f"Timed out waiting for {description} ({last_error}):\n{detail}")


def descendants(parent):
    """Only used to clean up our own failed supervisor tree on Linux."""
    children = []
    try:
        pids = Path(f"/proc/{parent}/task/{parent}/children").read_text().split()
    except OSError:
        return children
    for pid in pids:
        children.extend(descendants(int(pid)))
        children.append(int(pid))
    return children


@contextmanager
def supervisor(cwd, environment, logfile):
    with logfile.open("w") as output:
        process = subprocess.Popen(
            [str(Path(sys.executable).parent / "langgraph"), "dev",
             "--host", "127.0.0.1", "--port", "2024", "--no-browser",
             "--no-reload", "--n-jobs-per-worker", "10"],
            cwd=cwd, env=environment, stdout=output, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            yield process
        finally:
            owned = descendants(process.pid)
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            # Supervisor's normal shutdown waits for children. On failure, the
            # children are separate process groups and need explicit cleanup.
            for pid in owned:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass


def test_real_server_parallel_dag_auth_integration_and_graceful_restart(tmp_path, decision_endpoint):
    decision_env, decision_requests = decision_endpoint
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 2024)) == 0:
            pytest.skip("Local port 2024 is already in use; no existing service was stopped")
    run_root = tmp_path / "server"
    run_root.mkdir()
    for name in ("src", "prompts"):
        (run_root / name).symlink_to(ROOT / name, target_is_directory=True)
    for name in ("langgraph.json", "pyproject.toml", "graph.py"):
        shutil.copy2(ROOT / name, run_root / name)
    fixture = run_root / "qwen-fixture"
    shutil.copy2(ROOT / "tests" / "fixtures" / "qwen_fixture.py", fixture)
    fixture.chmod(0o700)
    workspace, data = tmp_path / "workspace", tmp_path / "data"
    message_id = str(uuid4())
    pid = identifier(message_id, "project")
    internal_token = uuid4().hex
    environment = {
        **os.environ,
        **decision_env,
        "PATH": f"{Path(sys.executable).parent}:{os.environ.get('PATH', '')}",
        "LANGGRAPH_URL": URL,
        "PYTHONPATH": str(ROOT / "src"),
        "QWEN_CODE_BIN": str(fixture),
        "QWEN_BASE_URL": "http://fixture.invalid/v1", "QWEN_API_KEY": "nonsecret-fixture-key",
        "QWEN_MODEL": "fixture-model", "WORKSPACE_ROOT": str(workspace), "DATA_ROOT": str(data),
        "DISPATCH_POLL_SECONDS": "0.05", "MAX_EXECUTE_WORKERS": "2", "MAX_QWEN_SESSIONS": "5",
        "TASK_LEASE_SECONDS": "30", "QWEN_TIMEOUT_SECONDS": "15",
        "LANGSMITH_TRACING": "false", "LANGGRAPH_API_DISABLE_ANALYTICS": "true",
        "DO_NOT_TRACK": "1", "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
    }
    # Prove langgraph dev loads credentials from config.env without a launcher.
    environment.pop("ORCHESTRATOR_TOKEN", None)
    (run_root / ".env").write_text(f"ORCHESTRATOR_TOKEN={internal_token}\n")
    with httpx.Client(base_url=URL, timeout=10, trust_env=False) as client:
        trusted = {"x-api-key": internal_token}
        first_log = tmp_path / "first-start.log"
        with supervisor(run_root, environment, first_log) as process:
            wait_for(lambda: client.get("/ok", headers=trusted).status_code == 200,
                     description="Agent Server readiness", process=process, logfile=first_log)
            assert client.get("/dashboard", headers=trusted).status_code == 404
            assert client.get("/api/projects", headers=trusted).status_code == 404
            assert client.post("/store/items/search",
                               json={"namespace_prefix": ["coding"], "limit": 10}).status_code in {401, 403}
            sdk = get_sync_client(url=URL, api_key=internal_token)

            def read_snapshot():
                result = {section: [row["value"] for row in sdk.store.search_items(
                    ("coding", pid, section), limit=100)["items"]]
                    for section in ("tasks", "workers", "notifications")}
                result["project"] = sdk.store.get_item(("coding", "projects"), key=pid)["value"]
                return result

            thread = sdk.threads.create()
            selected = sdk.runs.wait(thread["thread_id"], "communicator", input={"messages": [
                {"id": message_id, "role": "user", "content": "tạo project mới"}]})
            assert selected["project_id"] == pid
            assert selected["messages"][-1]["type"] == "ai"
            parts = list(sdk.runs.stream(thread["thread_id"], "communicator", input={"messages": [
                {"id": str(uuid4()), "role": "user", "content": "Build independent API and UI, then integrate and accept both."}]},
                stream_mode=["messages-tuple", "custom"], stream_subgraphs=True))
            frames = [line for part in parts for line in (
                f"event: {part.event}", json.dumps(part.data, ensure_ascii=False))]
            assert any(line.startswith("event: messages|communicate:") for line in frames), "\n".join(frames)[:10000]
            assert any('"status": "completed"' in line for line in frames), frames
            assert any('"type": "reply"' in line for line in frames), frames

            def completed():
                snapshot = read_snapshot()
                tasks = snapshot["tasks"]
                failed_workers = [worker for worker in snapshot["workers"] if worker.get("status") == "FAILED"]
                assert not failed_workers, f"Worker failed: {failed_workers}\n{first_log.read_text()[-16000:]}"
                return snapshot if len(tasks) == 7 and all(t["status"] == "CLOSED" for t in tasks) else None

            snapshot = wait_for(completed, timeout=60, description="all seven tasks CLOSED",
                                process=process, logfile=first_log)
            tasks = snapshot["tasks"]
            assert snapshot["project"]["repository_path"] == str(workspace / pid)
            assert len([r for r in decision_requests if r.get("response_format")]) == 7
            assert any(not r.get("stream") and not r.get("response_format") for r in decision_requests)
            assert any(r.get("stream") for r in decision_requests)
            execute = [task for task in tasks if task["agent"] == "EXECUTE"]
            assert len({task["assigned_worker"] for task in execute}) == 2
            assert all(task["integrated"] and task["commit_sha"] for task in execute)
            verify = next(task for task in tasks if task["agent"] == "VERIFY_INTEGRATE")
            assert set(verify["depends_on"]) == {task["task_id"] for task in execute}
            integration = workspace / "worktrees" / pid / "integration"
            assert (integration / "api.txt").read_text() == "api implemented\n"
            assert (integration / "ui.txt").read_text() == "ui implemented\n"
            assert snapshot["notifications"]
            assert not (data / "private" / pid / "COMMUNICATOR").exists()
            for task in tasks:
                if task["agent"] == "COMMUNICATOR":
                    continue  # Communicator calls ChatOpenAI without a CLI Work pass.
                runs = list((data / "private" / pid / task["agent"] / task["task_id"]).iterdir())
                assert len(runs) == 1
                assert (runs[0] / "work.jsonl").is_file()
                assert (runs[0] / "decision.jsonl").is_file()
            intervals = []
            for task in execute:
                work_log = next((data / "private" / pid / "EXECUTE" / task["task_id"]).glob("*/work.jsonl"))
                events = [json.loads(json.loads(line)["line"]) for line in work_log.read_text().splitlines()]
                intervals.append((next(e["at"] for e in events if e.get("fixture_phase") == "start"),
                                  next(e["at"] for e in events if e.get("fixture_phase") == "end")))
            assert max(start for start, _ in intervals) < min(end for _, end in intervals), "Execute work passes did not overlap"
            patches = sorted((workspace / "artifacts" / pid / "patches").glob("*.patch"))
            assert len(patches) == 3
            preserved_patches = {path: path.read_bytes() for path in patches}
            # Technical graphs run locally in worker processes, without server threads.
            threads = client.post("/threads/search", headers=trusted, json={"limit": 100}).json()
            assert len(threads) == 1 and threads[0]["thread_id"] == thread["thread_id"]
            task_ids = {task["task_id"] for task in tasks}
        assert process.returncode in (0, -signal.SIGTERM), first_log.read_text()[-6000:]
        # A fresh supervisor and Agent Server instance reattach the same data
        # directory. No task is resubmitted and no graph is rerun here.
        restart_log = tmp_path / "restart.log"
        with supervisor(run_root, environment, restart_log) as restarted:
            wait_for(lambda: client.get("/ok", headers=trusted).status_code == 200,
                     description="restarted Agent Server", process=restarted, logfile=restart_log)
            restored = wait_for(read_snapshot,
                                description="restored Store snapshot", process=restarted, logfile=restart_log)
            assert {task["task_id"] for task in restored["tasks"]} == task_ids
            assert all(task["status"] == "CLOSED" for task in restored["tasks"])
            assert (integration / "api.txt").read_text() == "api implemented\n"
            assert (integration / "ui.txt").read_text() == "ui implemented\n"
            assert all(path.read_bytes() == content for path, content in preserved_patches.items())
            restored_thread = sdk.threads.get_state(thread["thread_id"])
            assert restored_thread["values"]["project_id"] == pid
            assert restored_thread["values"]["messages"][-1]["type"] == "ai"
        assert restarted.returncode in (0, -signal.SIGTERM), restart_log.read_text()[-6000:]
        assert len([r for r in decision_requests if r.get("response_format")]) == 7
