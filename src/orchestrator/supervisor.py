"""One-container process supervisor with bounded restarts and signal forwarding."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
from urllib.error import URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from orchestrator.config import Settings

log = logging.getLogger(__name__)


def prepare_persistence(cwd: Path, data_root: Path) -> Path:
    """Attach dev runtime files to /data without discarding pre-existing data.

    langgraph dev persists relative to its working directory. This is development
    persistence, not transactional production durability; backups remain available
    if this function migrates existing files into an otherwise empty volume.
    """
    target = (data_root / "langgraph").absolute()
    target.mkdir(parents=True, exist_ok=True)
    link = cwd / ".langgraph_api"
    if link.is_symlink():
        if link.resolve() != target.resolve():
            raise RuntimeError("Existing .langgraph_api points to another data directory")
        return target
    if link.exists():
        if not link.is_dir():
            raise RuntimeError("Existing .langgraph_api is not a directory")
        if any(link.iterdir()):
            if any(target.iterdir()):
                raise RuntimeError("Both runtime directories contain data; reconcile before startup")
            shutil.copytree(link, target, dirs_exist_ok=True)
            link.rename(cwd / f".langgraph_api.backup-{uuid4().hex}")
        else:
            link.rmdir()
    link.symlink_to(target, target_is_directory=True)
    return target


def child_commands(settings: Settings) -> list[tuple[str, list[str]]]:
    children = [("dispatcher", [sys.executable, "-m", "orchestrator.dispatcher.main"])]
    for agent in ("COMMUNICATOR", "ANALYZE", "PLAN_DELEGATE", "VERIFY_INTEGRATE", "FINALIZE"):
        children.append((agent.lower(), [
            sys.executable, "-m", "orchestrator.workers.main",
            "--agent", agent, "--worker-id", f"{agent.lower()}-01",
        ]))
    for index in range(1, settings.max_execute_workers + 1):
        children.append((f"execute-{index:02d}", [
            sys.executable, "-m", "orchestrator.workers.main",
            "--agent", "EXECUTE", "--worker-id", f"execute-{index:02d}",
        ]))
    return children


def server_command(settings: Settings) -> list[str]:
    # The pinned CLI overwrites N_JOBS_PER_WORKER with 1 unless this flag is
    # supplied explicitly. The environment variable alone does not enable
    # parallel graph runs, even though the Agent Server supports them.
    return [
        "langgraph", "dev", "--host", "0.0.0.0", "--port", "2024",
        "--no-browser", "--no-reload", "--n-jobs-per-worker",
        str(max(10, settings.max_qwen_sessions)),
    ]


def wait_for_readiness(url: str, token: str, stopped: threading.Event, timeout: float = 90) -> bool:
    deadline = time.monotonic() + timeout
    while not stopped.is_set() and time.monotonic() < deadline:
        try:
            request = Request(url.rstrip("/") + "/ok", headers={"x-api-key": token})
            with urlopen(request, timeout=2) as response:
                if response.status == 200:
                    return True
        except (URLError, TimeoutError, OSError):
            pass
        stopped.wait(0.25)
    return False


@dataclass
class Child:
    name: str
    command: list[str]
    process: subprocess.Popen
    restarts: int = 0
    started_at: float = 0


class Supervisor:
    def __init__(self, settings: Settings | None = None, cwd: Path | None = None):
        self.settings = settings or Settings.from_env()
        self.cwd = Path(cwd or Path.cwd()).resolve()
        self.stopped = threading.Event()
        self.children: list[Child] = []
        self.max_restarts = int(os.getenv("WORKER_MAX_RESTARTS", "3"))
        self.restart_window = float(os.getenv("WORKER_RESTART_WINDOW_SECONDS", "300"))

    def spawn(self, name: str, command: list[str]) -> Child:
        child_env = dict(os.environ)
        # All graph jobs share one server event loop, so the Qwen process-wide
        # semaphore applies across every role. More runnable jobs than Qwen slots
        # lets dependency-independent graph runs make concurrent progress.
        child_env["N_JOBS_PER_WORKER"] = str(max(10, self.settings.max_qwen_sessions))
        child_env["BG_JOB_ISOLATED_LOOPS"] = "false"
        process = subprocess.Popen(command, cwd=self.cwd, env=child_env, start_new_session=True)
        child = Child(name, command, process, started_at=time.monotonic())
        self.children.append(child)
        log.info("Started %s (PID %s)", name, process.pid)
        return child

    @staticmethod
    def signal_child(child: Child, sig: int):
        if child.process.poll() is None:
            try:
                os.killpg(child.process.pid, sig)
            except ProcessLookupError:
                pass

    def shutdown(self):
        # Stop producers before the Store server so final requests can flush.
        service = [child for child in self.children if child.name != "langgraph"]
        server = [child for child in self.children if child.name == "langgraph"]
        for group in (service, server):
            for child in group:
                self.signal_child(child, signal.SIGTERM)
            deadline = time.monotonic() + 10
            for child in group:
                try:
                    child.process.wait(timeout=max(0.01, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    self.signal_child(child, signal.SIGKILL)
                    child.process.wait(timeout=5)
        self.children.clear()

    def run(self) -> int:
        settings = self.settings
        if not settings.orchestrator_token or not settings.dashboard_token:
            raise RuntimeError("Set distinct ORCHESTRATOR_TOKEN and DASHBOARD_TOKEN before startup")
        prepare_persistence(self.cwd, settings.data_root)
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.stopped.set())
        try:
            server = self.spawn("langgraph", server_command(settings))
            if not wait_for_readiness(settings.langgraph_url, settings.orchestrator_token, self.stopped):
                if not self.stopped.is_set():
                    log.error("Agent Server failed readiness (exit %s)", server.process.poll())
                return 0 if self.stopped.is_set() else 1
            for name, command in child_commands(settings):
                self.spawn(name, command)
            while not self.stopped.wait(0.25):
                if server.process.poll() is not None:
                    log.error("Agent Server exited; stopping workers for lease recovery on restart")
                    return 1
                for child in list(self.children):
                    if child is server or child.process.poll() is None:
                        continue
                    if time.monotonic() - child.started_at >= self.restart_window:
                        child.restarts = 0
                    if child.restarts >= self.max_restarts:
                        log.error("%s exceeded its bounded restart budget", child.name)
                        return 1
                    self.children.remove(child)
                    replacement = self.spawn(child.name, child.command)
                    replacement.restarts = child.restarts + 1
            return 0
        finally:
            self.shutdown()


def main():
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    try:
        code = Supervisor().run()
    except Exception as exc:
        # Configuration values and process environments never enter public logs.
        log.error("Supervisor startup failed (%s): %s", type(exc).__name__, exc)
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
