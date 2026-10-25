#!/usr/bin/env python3
"""Exercise the real configured LLM, Qwen CLI, server, queues and Git integration.

Run from the repository with its installed environment:
    .venv/bin/python scripts/live_smoke.py --timeout 600

Artifacts stay in .local/live-tests/<timestamp>-<id>. No existing service is
stopped, no .env file is copied, and every harness log redacts configured secrets.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Any
from uuid import uuid4

import httpx
from dotenv import dotenv_values
from langgraph_sdk import get_sync_client


ROOT = Path(__file__).resolve().parents[1]
URL = "http://127.0.0.1:2024"
ROLES = {"ANALYZE", "PLAN_DELEGATE", "EXECUTE", "VERIFY_INTEGRATE", "FINALIZE"}
REQUEST = """Implement this complete, deliberately small Python task in the selected workspace.
Create calculator.py at the repository root containing add(a, b), returning a + b.
Create test_calculator.py at the root using only Python's standard-library unittest,
with at least three actual test methods covering positive numbers, negative numbers,
and zero. Include mixed signs as well. Create README.md describing add and the exact
command `python -m unittest discover -v`. No external dependencies, network access,
GitLab work, web application, or additional features are needed. All requirements
and acceptance criteria are given here; choose routine implementation details and
proceed through analysis, planning, implementation, integration, and final acceptance.
Run the tests and report their actual result. Delivery is accepted when those three
files exist in the integrated workspace, add behaves correctly, and the unittest
command passes with at least three tests. Finalize the execution chain after verifying
these criteria. There is no need to ask a clarification question for this task.
"""


def require(condition: Any, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def descendants(pid: int) -> list[int]:
    """Track only processes descended from the Supervisor we created (Linux)."""
    try:
        children = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
    except OSError:
        return []
    return [descendant for child in children
            for descendant in [*descendants(int(child)), int(child)]]


class LiveSmoke:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.deadline = time.monotonic() + args.timeout
        self.env = {**os.environ, **{
            key: value for key, value in dotenv_values(args.env_file).items() if value is not None
        }}
        self.secrets = sorted({value for key, value in self.env.items() if value and any(
            word in key.lower() for word in ("api_key", "token", "password", "secret", "credential"))},
            key=len, reverse=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.output = ROOT / ".local" / "live-tests" / f"{stamp}-{uuid4().hex[:8]}"
        self.output.mkdir(parents=True, mode=0o700)
        self.run_root = self.output / "runtime"
        self.run_root.mkdir()
        self.workspace = self.output / "workspace"
        self.data = self.output / "data"
        self.process: subprocess.Popen | None = None
        self.container_name: str | None = None
        self.docker_binary: str | None = None
        self.log_thread: threading.Thread | None = None
        self.stream_thread: threading.Thread | None = None
        self.stream_done = threading.Event()
        self.stream_errors: list[str] = []
        self.report: dict = {"status": "running", "started_at": stamp,
                             "artifacts": str(self.output), "checks": {}}

    def redact(self, value: Any) -> Any:
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, dict):
            return {str(key): self.redact(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact(item) for item in value]
        return value

    def say(self, message: str) -> None:
        print(self.redact(message), flush=True)

    def save_report(self) -> None:
        (self.output / "report.json").write_text(
            json.dumps(self.redact(self.report), ensure_ascii=False, indent=2, default=str) + "\n")

    def remaining(self, maximum: float | None = None) -> float:
        seconds = self.deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError(f"Live smoke exceeded {self.args.timeout} seconds")
        return min(seconds, maximum) if maximum is not None else seconds

    def prepare(self) -> None:
        for key in ("QWEN_BASE_URL", "QWEN_API_KEY", "QWEN_MODEL", "ORCHESTRATOR_TOKEN"):
            require(self.env.get(key), f"Missing configuration: {key}")
        for name in ("src", "prompts", "config", "skills"):
            (self.run_root / name).symlink_to(ROOT / name, target_is_directory=True)
        for name in ("langgraph.json", "pyproject.toml", "graph.py"):
            shutil.copy2(ROOT / name, self.run_root / name)
        for key in ("SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS"):
            if self.env.get(key):
                path = Path(self.env[key]).expanduser()
                self.env[key] = str((ROOT / path).resolve() if not path.is_absolute() else path.resolve())
        self.env.update({
            "PATH": os.pathsep.join([str(ROOT / ".venv" / "bin"), str(Path(sys.executable).parent),
                                     self.env.get("PATH", "")]),
            "PYTHONPATH": str(ROOT / "src"),
            "LANGGRAPH_URL": URL, "DATA_ROOT": str(self.data), "WORKSPACE_ROOT": str(self.workspace),
            "LANGSMITH_TRACING": "false", "LANGGRAPH_API_DISABLE_ANALYTICS": "true",
            "DO_NOT_TRACK": "1", "PYTHONUNBUFFERED": "1",
            "QWEN_TIMEOUT_SECONDS": str(max(1, int(min(
                float(self.env.get("QWEN_TIMEOUT_SECONDS", "1800")), self.args.work_timeout)))),
        })
        # Relative executable and skill configuration paths refer to the repository,
        # rather than the isolated Supervisor working directory.
        for key in ("QWEN_CODE_BIN", "QWEN_EXECUTABLE", "WORK_PASS_SKILLS_CONFIG"):
            if self.env.get(key) and "/" in self.env[key] and not Path(self.env[key]).is_absolute():
                self.env[key] = str((ROOT / self.env[key]).resolve())
        executable = self.env.get("QWEN_CODE_BIN") or self.env.get("QWEN_EXECUTABLE", "qwen")
        if self.args.docker_image:
            self.docker_binary = shutil.which("docker", path=self.env["PATH"])
            require(self.docker_binary, "Docker CLI is unavailable")
            binary = f"{self.args.docker_image}:qwen"
            version_command = [self.docker_binary, "run", "--rm", "--network", "none",
                               "--entrypoint", executable, self.args.docker_image, "--version"]
        else:
            binary = shutil.which(executable, path=self.env["PATH"])
            require(binary, f"Qwen CLI executable is unavailable: {executable}")
            version_command = [binary, "--version"]
        version = subprocess.run(version_command, env=self.env, capture_output=True,
                                 text=True, timeout=self.remaining(30))
        require(version.returncode == 0, "Qwen CLI --version failed")
        self.report.update({"qwen_binary": binary, "qwen_version": version.stdout.strip(),
                            "expected_qwen_version": "0.26.1",
                            "qwen_timeout_seconds": self.env["QWEN_TIMEOUT_SECONDS"],
                            "docker_image": self.args.docker_image,
                            "request": REQUEST})
        self.say(f"Artifacts: {self.output}")
        self.say(f"Real Qwen CLI: {version.stdout.strip()}")
        self.save_report()

    def start(self, phase: str) -> None:
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", 2024))
            except OSError as exc:
                raise RuntimeError("Port 2024 is occupied; the existing service was left running") from exc
        command = [sys.executable, "-m", "orchestrator.supervisor"]
        environment = self.env
        if self.args.docker_image:
            self.container_name = f"orchestrator-live-{uuid4().hex[:12]}"
            environment = {**self.env, "PATH": "/app/.venv/bin:/usr/local/bin:/usr/bin:/bin"}
            command = [self.docker_binary, "run", "--rm", "--name", self.container_name,
                       "--network", "host", "--user", f"{os.getuid()}:{os.getgid()}",
                       "--mount", f"type=bind,src={ROOT},dst={ROOT},readonly",
                       "--mount", f"type=bind,src={self.output},dst={self.output}",
                       "--workdir", str(self.run_root)]
            # Pass names only: docker obtains the values from Popen's environment,
            # keeping credentials out of both command arguments and saved logs.
            for key in environment:
                command.extend(["--env", key])
            command.extend(["--entrypoint", "/usr/bin/tini", self.args.docker_image, "--",
                            "/app/.venv/bin/python", "-m", "orchestrator.supervisor"])
        self.process = subprocess.Popen(
            command, cwd=self.run_root, env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
            start_new_session=True,
        )
        process = self.process

        def copy_log() -> None:
            with (self.output / "server.log").open("a") as output:
                output.write(f"\n--- {phase}, Supervisor PID {process.pid} ---\n")
                for line in process.stdout:
                    output.write(self.redact(line))
                    output.flush()

        self.log_thread = threading.Thread(target=copy_log, daemon=True)
        self.log_thread.start()
        deadline = time.monotonic() + self.remaining(90)
        with httpx.Client(base_url=URL, timeout=2, trust_env=False) as client:
            while time.monotonic() < deadline:
                require(process.poll() is None, f"Supervisor exited during {phase}; see server.log")
                try:
                    if client.get("/ok", headers={"x-api-key": self.env["ORCHESTRATOR_TOKEN"]}).status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(.25)
        raise TimeoutError(f"Agent Server did not become ready during {phase}")

    def stop(self) -> int | None:
        process = self.process
        if process is None:
            return None
        owned = descendants(process.pid)
        if self.container_name and process.poll() is None:
            subprocess.run([self.docker_binary, "stop", "--time", "30", self.container_name],
                           capture_output=True, timeout=40)
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=35)
            except subprocess.TimeoutExpired:
                for pid in [*owned, process.pid]:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                process.wait(timeout=5)
        # The normal Supervisor path joins its workers. Clean any remaining owned
        # descendants if a failure prevented that path from completing.
        if process.returncode:
            for pid in owned:
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        if self.log_thread:
            self.log_thread.join(timeout=5)
        self.process = None
        self.container_name = None
        return process.returncode

    def check_auth(self) -> None:
        payload = {"namespace_prefix": ["coding"], "limit": 10}
        with httpx.Client(base_url=URL, timeout=self.remaining(10), trust_env=False) as client:
            missing = client.post("/store/items/search", json=payload).status_code
            invalid = client.post("/store/items/search", json=payload,
                                  headers={"x-api-key": "invalid-live-smoke-" + uuid4().hex}).status_code
            valid = client.post("/store/items/search", json=payload,
                                headers={"x-api-key": self.env["ORCHESTRATOR_TOKEN"]}).status_code
        require(missing in {401, 403}, f"Missing credential was not rejected: HTTP {missing}")
        require(invalid in {401, 403}, f"Invalid credential was not rejected: HTTP {invalid}")
        require(valid == 200, f"Valid credential failed: HTTP {valid}")
        self.report["checks"]["authentication"] = {"missing": missing, "invalid": invalid, "valid": valid}

    @staticmethod
    def records(client, project_id: str, section: str) -> list[dict]:
        rows: list[dict] = []
        while True:
            items = client.store.search_items(("coding", project_id, section), limit=100, offset=len(rows))["items"]
            rows.extend(item["value"] for item in items)
            if len(items) < 100:
                return rows

    def snapshot(self, client, project_id: str) -> dict:
        return {section: self.records(client, project_id, section)
                for section in ("tasks", "workers", "approvals", "notifications")}

    def begin_requirement(self, thread_id: str) -> None:
        def collect() -> None:
            try:
                with get_sync_client(url=URL, api_key=self.env["ORCHESTRATOR_TOKEN"],
                                     timeout=httpx.Timeout(self.remaining(), connect=10)) as client:
                    with (self.output / "stream.jsonl").open("w") as output:
                        for part in client.runs.stream(
                            thread_id, "communicator", input={"messages": [
                                {"id": str(uuid4()), "role": "user", "content": REQUEST}]},
                            stream_mode=["custom"], stream_subgraphs=True, on_disconnect="continue",
                        ):
                            event = {"event": part.event, "data": part.data}
                            output.write(json.dumps(self.redact(event), ensure_ascii=False, default=str) + "\n")
                            output.flush()
                            if part.event == "error":
                                self.stream_errors.append(str(self.redact(part.data)))
            except Exception as exc:
                self.stream_errors.append(self.redact(f"{type(exc).__name__}: {exc}"))
            finally:
                self.stream_done.set()

        self.stream_thread = threading.Thread(target=collect, daemon=True)
        self.stream_thread.start()

    def await_delivery(self, client, project_id: str) -> dict:
        previous = None
        idle_since = None
        while True:
            self.remaining()
            require(self.process.poll() is None, "Supervisor exited before delivery; see server.log")
            snapshot = self.snapshot(client, project_id)
            tasks = snapshot["tasks"]
            self.report["snapshot"] = snapshot
            state = sorted((task["task_id"], task["agent"], task["status"], task.get("outcome"),
                            task.get("attempt")) for task in tasks)
            if state != previous:
                self.say("Tasks: " + ", ".join(f"{agent}:{status}/{outcome or '-'} (try {attempt})"
                                               for _, agent, status, outcome, attempt in state))
                previous = state
                self.save_report()
                idle_since = None
            failed = [worker for worker in snapshot["workers"] if worker.get("status") == "FAILED"]
            require(not failed, f"Worker failure: {self.redact(failed)}")
            pending = [approval for approval in snapshot["approvals"] if approval.get("status") == "PENDING"]
            require(not pending, f"Model requested human clarification: {self.redact(pending)}")
            require(not self.stream_errors, f"Stream failed: {self.stream_errors}")
            if tasks and all(task["status"] == "CLOSED" for task in tasks):
                require(all(task.get("outcome") != "FAILED" for task in tasks), "A closed task has FAILED outcome")
                require(ROLES <= {task["agent"] for task in tasks}, "Delivery did not exercise all technical roles")
                if self.stream_done.wait(timeout=self.remaining(30)):
                    require(not self.stream_errors, f"Stream failed: {self.stream_errors}")
                    return snapshot
                raise TimeoutError("Delivery closed but the Communicator stream did not finish")
            active = any(task["status"] in {"TODO", "DOING", "IN_REVIEW"} for task in tasks)
            if tasks and not active:
                idle_since = idle_since or time.monotonic()
                require(time.monotonic() - idle_since < 10, "Execution chain is blocked; see report snapshot")
            if self.stream_done.is_set():
                raise AssertionError("Communicator stream ended before the execution chain closed")
            time.sleep(min(1, self.remaining()))

    def check_delivery(self, snapshot: dict, project_id: str) -> dict[str, str]:
        integration = self.workspace / "worktrees" / project_id / "integration"
        for name in ("calculator.py", "test_calculator.py", "README.md"):
            require((integration / name).is_file(), f"Missing integrated {name}")
        tests = subprocess.run([sys.executable, "-m", "unittest", "discover", "-v"],
                               cwd=integration, env=self.env, text=True, capture_output=True,
                               timeout=self.remaining(30))
        (self.output / "acceptance.log").write_text(self.redact(tests.stdout + tests.stderr))
        require(tests.returncode == 0, "Integrated unittest failed; see acceptance.log")
        count = re.search(r"Ran (\d+) tests?", tests.stdout + tests.stderr)
        require(count and int(count[1]) >= 3, "Unittest did not execute at least three tests")
        independent = subprocess.run([
            sys.executable, "-c", "from calculator import add; "
            "assert add(2, 3) == 5; assert add(-2, -3) == -5; "
            "assert add(-2, 3) == 1; assert add(0, 0) == 0; print('Independent add checks passed')",
        ], cwd=integration, env=self.env, text=True, capture_output=True, timeout=self.remaining(10))
        require(independent.returncode == 0, "Independent add acceptance checks failed")
        evidence = {}
        for task in snapshot["tasks"]:
            if task["agent"] not in ROLES:
                continue
            directory = self.data / "private" / project_id / task["agent"] / task["task_id"]
            runs = [path for path in directory.iterdir() if path.is_dir()] if directory.exists() else []
            good = [path for path in runs if (path / "work.jsonl").is_file()
                    and (path / "work.jsonl").stat().st_size > 0
                    and (path / "decision.jsonl").is_file()
                    and (path / "decision.jsonl").stat().st_size > 0]
            require(good, f"Missing actual Work/Decision logs for {task['agent']}:{task['task_id']}")
            evidence[task["task_id"]] = [str(path.relative_to(self.output)) for path in good]
        self.report["checks"]["delivery"] = {"unittest_count": int(count[1]), "independent_add": "passed",
                                                 "work_decision_logs": evidence}
        return {name: hashlib.sha256((integration / name).read_bytes()).hexdigest()
                for name in ("calculator.py", "test_calculator.py", "README.md")}

    def run(self) -> None:
        self.prepare()
        self.start("first start")
        self.check_auth()
        with get_sync_client(url=URL, api_key=self.env["ORCHESTRATOR_TOKEN"],
                             timeout=httpx.Timeout(self.remaining(), connect=10)) as client:
            thread_id = client.threads.create()["thread_id"]
            selected = client.runs.wait(thread_id, "communicator", input={"messages": [
                {"id": str(uuid4()), "role": "user", "content": "tạo project mới"}]})
            project_id = selected.get("project_id")
            require(project_id, "Project selection did not create a project")
            self.report.update({"thread_id": thread_id, "project_id": project_id})
            self.begin_requirement(thread_id)
            snapshot = self.await_delivery(client, project_id)
            hashes = self.check_delivery(snapshot, project_id)
            thread_before = client.threads.get_state(thread_id)["values"]
        self.report["checks"]["first_shutdown_exit"] = self.stop()
        require(self.report["checks"]["first_shutdown_exit"] == 0, "First Supervisor shutdown was not clean")
        self.start("persistence restart")
        with get_sync_client(url=URL, api_key=self.env["ORCHESTRATOR_TOKEN"], timeout=self.remaining(30)) as client:
            restored = self.snapshot(client, project_id)
            original_ids = {task["task_id"] for task in snapshot["tasks"]}
            require({task["task_id"] for task in restored["tasks"]} == original_ids, "Task IDs changed after restart")
            require(all(task["status"] == "CLOSED" for task in restored["tasks"]), "Task state changed after restart")
            require(client.threads.get_state(thread_id)["values"] == thread_before, "Thread state changed after restart")
            integration = self.workspace / "worktrees" / project_id / "integration"
            require(all(hashlib.sha256((integration / name).read_bytes()).hexdigest() == digest
                        for name, digest in hashes.items()), "Integrated files changed after restart")
        self.report["checks"]["restart_persistence"] = {"task_ids": sorted(original_ids), "file_sha256": hashes}
        self.report["checks"]["second_shutdown_exit"] = self.stop()
        require(self.report["checks"]["second_shutdown_exit"] == 0, "Restarted Supervisor shutdown was not clean")
        self.report["status"] = "passed"
        self.say("PASS: real-model chain closed, calculator tests passed, and restart preserved delivery.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--timeout", type=float, default=600, help="Overall execution deadline in seconds")
    parser.add_argument("--work-timeout", type=float, default=120, help="Maximum seconds for one real Qwen Work pass")
    parser.add_argument("--docker-image", help="Use this existing Docker image for the pinned real runtime")
    args = parser.parse_args()
    if args.timeout <= 0 or args.work_timeout <= 0:
        parser.error("Timeouts must be positive")
    if not args.env_file.is_file():
        parser.error("The specified .env file does not exist")
    smoke = LiveSmoke(args)
    try:
        smoke.run()
    except (Exception, KeyboardInterrupt) as exc:
        smoke.report.update({"status": "failed", "error": smoke.redact(f"{type(exc).__name__}: {exc}")})
        smoke.say(f"FAIL: {smoke.report['error']}")
    finally:
        if smoke.process is not None:
            smoke.report["checks"]["cleanup_shutdown_exit"] = smoke.stop()
        if smoke.stream_thread:
            smoke.stream_thread.join(timeout=5)
        smoke.report["finished_at"] = datetime.now(timezone.utc).isoformat()
        smoke.save_report()
        smoke.say(f"Report: {smoke.output / 'report.json'}")
    return 0 if smoke.report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
