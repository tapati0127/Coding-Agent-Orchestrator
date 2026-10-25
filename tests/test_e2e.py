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

from fixtures.decision_fixture import decision_endpoint


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
            [sys.executable, "-m", "orchestrator.supervisor"],
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
    for name in ("src", "prompts", "dashboard"):
        (run_root / name).symlink_to(ROOT / name, target_is_directory=True)
    for name in ("langgraph.json", "pyproject.toml"):
        shutil.copy2(ROOT / name, run_root / name)
    fixture = run_root / "qwen-fixture"
    shutil.copy2(ROOT / "tests" / "fixtures" / "qwen_fixture.py", fixture)
    fixture.chmod(0o700)
    workspace, data = tmp_path / "workspace", tmp_path / "data"
    repository = workspace / "repositories" / "fixture"
    repository.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    (repository / "README.md").write_text("Fixture project\n")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repository), "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "-qm", "Fixture baseline"], check=True)
    internal_token, dashboard_token = uuid4().hex, uuid4().hex
    environment = {
        **os.environ,
        **decision_env,
        "PATH": f"{Path(sys.executable).parent}:{os.environ.get('PATH', '')}",
        "LANGGRAPH_URL": URL, "ORCHESTRATOR_TOKEN": internal_token,
        "DASHBOARD_TOKEN": dashboard_token, "QWEN_CODE_BIN": str(fixture),
        "QWEN_BASE_URL": "http://fixture.invalid/v1", "QWEN_API_KEY": "nonsecret-fixture-key",
        "QWEN_MODEL": "fixture-model", "WORKSPACE_ROOT": str(workspace), "DATA_ROOT": str(data),
        "DISPATCH_POLL_SECONDS": "0.05", "MAX_EXECUTE_WORKERS": "2", "MAX_QWEN_SESSIONS": "5",
        "TASK_LEASE_SECONDS": "30", "QWEN_TIMEOUT_SECONDS": "15",
        "LANGSMITH_TRACING": "false", "LANGGRAPH_API_DISABLE_ANALYTICS": "true",
        "DO_NOT_TRACK": "1", "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
    }
    with httpx.Client(base_url=URL, timeout=10, trust_env=False) as client:
        headers = {"Authorization": f"Bearer {dashboard_token}"}
        trusted = {"x-api-key": internal_token}
        first_log = tmp_path / "first-start.log"
        with supervisor(run_root, environment, first_log) as process:
            wait_for(lambda: client.get("/ok", headers=trusted).status_code == 200,
                     description="Agent Server readiness", process=process, logfile=first_log)
            assert client.get("/dashboard").status_code == 200
            assert client.get("/api/projects").status_code == 401
            assert client.post("/store/items/search", headers=headers,
                               json={"namespace_prefix": ["coding"], "limit": 10}).status_code in {401, 403}
            assert client.post("/api/projects", headers=headers, json={
                "project_id": "fixture", "title": "Fixture", "repository_path": str(repository),
            }).status_code == 202
            wait_for(lambda: client.get("/api/projects/fixture/snapshot", headers=headers).status_code == 200,
                     description="project registration", process=process, logfile=first_log)
            with client.stream("POST", "/api/chat/stream", headers=headers, timeout=60, json={
                "project_id": "fixture", "messages": [{"id": str(uuid4()), "role": "user",
                "content": "Build independent API and UI, then integrate and accept both."}],
            }) as response:
                assert response.status_code == 200
                frames = list(response.iter_lines())
            assert any(line.startswith("event: messages|communicate:") for line in frames), "\n".join(frames)[:10000]
            assert any('"status": "completed"' in line for line in frames), frames
            assert any('"type": "reply"' in line for line in frames), frames

            def completed():
                response = client.get("/api/projects/fixture/snapshot", headers=headers)
                response.raise_for_status()
                snapshot = response.json()
                tasks = snapshot["tasks"]
                failed_workers = [worker for worker in snapshot["workers"] if worker.get("status") == "FAILED"]
                assert not failed_workers, f"Worker failed: {failed_workers}\n{first_log.read_text()[-16000:]}"
                return snapshot if len(tasks) == 7 and all(t["status"] == "CLOSED" for t in tasks) else None

            snapshot = wait_for(completed, timeout=60, description="all seven tasks CLOSED",
                                process=process, logfile=first_log)
            tasks = snapshot["tasks"]
            assert len([r for r in decision_requests if not r.get("stream")]) == 7
            assert any(r.get("stream") for r in decision_requests)
            execute = [task for task in tasks if task["agent"] == "EXECUTE"]
            assert len({task["assigned_worker"] for task in execute}) == 2
            assert all(task["integrated"] and task["commit_sha"] for task in execute)
            verify = next(task for task in tasks if task["agent"] == "VERIFY_INTEGRATE")
            assert set(verify["depends_on"]) == {task["task_id"] for task in execute}
            integration = workspace / "worktrees" / "fixture" / "integration"
            assert (integration / "api.txt").read_text() == "api implemented\n"
            assert (integration / "ui.txt").read_text() == "ui implemented\n"
            assert snapshot["notifications"]
            assert not (data / "private" / "fixture" / "COMMUNICATOR").exists()
            for task in tasks:
                if task["agent"] == "COMMUNICATOR":
                    continue  # Communicator calls ChatOpenAI without a CLI Work pass.
                runs = list((data / "private" / "fixture" / task["agent"] / task["task_id"]).iterdir())
                assert len(runs) == 1
                assert (runs[0] / "work.jsonl").is_file()
                assert (runs[0] / "decision.jsonl").is_file()
            intervals = []
            for task in execute:
                work_log = next((data / "private" / "fixture" / "EXECUTE" / task["task_id"]).glob("*/work.jsonl"))
                events = [json.loads(json.loads(line)["line"]) for line in work_log.read_text().splitlines()]
                intervals.append((next(e["at"] for e in events if e.get("fixture_phase") == "start"),
                                  next(e["at"] for e in events if e.get("fixture_phase") == "end")))
            assert max(start for start, _ in intervals) < min(end for _, end in intervals), "Execute work passes did not overlap"
            patches = sorted((workspace / "artifacts" / "fixture" / "patches").glob("*.patch"))
            assert len(patches) == 3
            preserved_patches = {path: path.read_bytes() for path in patches}
            # Technical graphs run locally in worker processes, without server threads.
            threads = client.post("/threads/search", headers=trusted, json={"limit": 100}).json()
            assert len(threads) == 1 and threads[0]["metadata"]["dashboard_chat"]
            task_ids = {task["task_id"] for task in tasks}
        assert process.returncode == 0, first_log.read_text()[-6000:]
        # A fresh supervisor and Agent Server instance reattach the same data
        # directory. No task is resubmitted and no graph is rerun here.
        restart_log = tmp_path / "restart.log"
        with supervisor(run_root, environment, restart_log) as restarted:
            wait_for(lambda: client.get("/ok", headers=trusted).status_code == 200,
                     description="restarted Agent Server", process=restarted, logfile=restart_log)
            restored = wait_for(lambda: client.get("/api/projects/fixture/snapshot", headers=headers).json(),
                                description="restored Store snapshot", process=restarted, logfile=restart_log)
            assert {task["task_id"] for task in restored["tasks"]} == task_ids
            assert all(task["status"] == "CLOSED" for task in restored["tasks"])
            assert (integration / "api.txt").read_text() == "api implemented\n"
            assert (integration / "ui.txt").read_text() == "ui implemented\n"
            assert all(path.read_bytes() == content for path, content in preserved_patches.items())
        assert restarted.returncode == 0, restart_log.read_text()[-6000:]
        assert len([r for r in decision_requests if not r.get("stream")]) == 7
