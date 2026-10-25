"""Start the web server and Python worker processes connected by queues."""

from __future__ import annotations

import json
import logging
import multiprocessing as mp
import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading
import time
from urllib.error import URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from orchestrator.config import Settings
from orchestrator.queues import start_bridge

log = logging.getLogger(__name__)


def prepare_persistence(cwd: Path, data_root: Path) -> Path:
    """Keep existing Agent Server persistence without discarding old data."""
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


def worker_specs(settings: Settings) -> dict[str, str]:
    roles = {f"{agent.lower()}-01": agent for agent in (
        "COMMUNICATOR", "ANALYZE", "PLAN_DELEGATE", "VERIFY_INTEGRATE", "FINALIZE")}
    roles.update({f"execute-{i:02d}": "EXECUTE" for i in range(1, settings.max_execute_workers + 1)})
    return roles


def server_command(settings: Settings) -> list[str]:
    return ["langgraph", "dev", "--host", "0.0.0.0", "--port", "2024",
            "--no-browser", "--no-reload", "--n-jobs-per-worker", "10"]


def wait_for_readiness(url, token, stopped, timeout=90):
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


class Supervisor:
    def __init__(self, settings=None, cwd=None):
        self.settings = settings or Settings.from_env()
        self.cwd = Path(cwd or Path.cwd()).resolve()
        self.context = mp.get_context("spawn")
        self.stopped = threading.Event()
        self.children, self.specs, self.started, self.restarts = {}, {}, {}, {}
        self.inboxes, self.events, self.bridge, self.server = {}, None, None, None
        self.dispatcher_status = None
        self.max_restarts = int(os.getenv("WORKER_MAX_RESTARTS", "3"))
        self.restart_window = float(os.getenv("WORKER_RESTART_WINDOW_SECONDS", "300"))

    def spawn(self, name, target, args):
        process = self.context.Process(name=name, target=target, args=args)
        process.start()
        self.children[name], self.specs[name], self.started[name] = process, (target, args), time.monotonic()
        log.info("Started %s (PID %s)", name, process.pid)
        return process

    @staticmethod
    def stop_process(process):
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        if process.is_alive():
            process.kill()
        process.join()

    def shutdown(self):
        # Flush worker results, stop the in-server Dispatcher, then persistence.
        for inbox in self.inboxes.values():
            inbox.put(None)
        deadline = time.monotonic() + 10
        for name, process in self.children.items():
            process.join(timeout=max(0, deadline - time.monotonic()))
            self.stop_process(process)
        if self.dispatcher_status is not None and self.dispatcher_status.get("state") == "running":
            self.events.put(None)
            deadline = time.monotonic() + 10
            while self.dispatcher_status.get("state") == "running" and time.monotonic() < deadline:
                if self.server and self.server.poll() is not None:
                    break
                time.sleep(0.05)
        self.children.clear()
        if self.server and self.server.poll() is None:
            self.server.terminate()
            try:
                self.server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.server.kill()
                self.server.wait()
        if self.bridge:
            self.bridge.shutdown()
            self.bridge = None
        for queue in [*self.inboxes.values(), *([self.events] if self.events is not None else [])]:
            queue.cancel_join_thread()
            queue.close()

    def run(self):
        from orchestrator.workers.main import run_worker

        settings = self.settings
        if not settings.orchestrator_token or not settings.dashboard_token:
            raise RuntimeError("Set distinct ORCHESTRATOR_TOKEN and DASHBOARD_TOKEN before startup")
        prepare_persistence(self.cwd, settings.data_root)
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.stopped.set())
        try:
            roles = worker_specs(settings)
            self.events = self.context.Queue()
            self.inboxes = {worker_id: self.context.Queue() for worker_id in roles}
            self.bridge = start_bridge(self.events, self.context, self.inboxes, roles)
            self.dispatcher_status = self.bridge.status()
            env = {**os.environ, "ORCHESTRATOR_IPC_ADDRESS": json.dumps(self.bridge.address),
                   "ORCHESTRATOR_IPC_KEY": bytes(self.bridge._authkey).hex()}
            self.server = subprocess.Popen(server_command(settings), cwd=self.cwd, env=env)
            if not wait_for_readiness(settings.langgraph_url, settings.orchestrator_token, self.stopped):
                return 0 if self.stopped.is_set() else 1
            for worker_id, agent in roles.items():
                self.spawn(worker_id, run_worker, (agent, worker_id, self.inboxes[worker_id], self.events, settings))
            while not self.stopped.wait(0.25):
                if self.server.poll() is not None:
                    return 1
                if self.dispatcher_status.get("state") in {"failed", "stopped"}:
                    log.error("Dispatcher stopped; shutting down workers")
                    return 1
                for name, process in list(self.children.items()):
                    if process.is_alive():
                        continue
                    process.join()
                    retries = 0 if time.monotonic() - self.started[name] >= self.restart_window else self.restarts.get(name, 0)
                    if retries >= self.max_restarts:
                        log.error("%s exceeded its restart budget", name)
                        return 1
                    self.restarts[name] = retries + 1
                    target, args = self.specs[name]
                    self.spawn(name, target, args)
            return 0
        finally:
            self.shutdown()


def main():
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    try:
        code = Supervisor().run()
    except Exception as exc:
        log.error("Supervisor failed (%s): %s", type(exc).__name__, exc)
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
