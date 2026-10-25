"""Prepare persistence, then run the Communicator server in the main process.

Child graphs and Python queues are owned by the root graph.py lifecycle.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
import shutil
import signal
from uuid import uuid4

from orchestrator.config import Settings

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


def server_command(settings: Settings) -> list[str]:
    return ["langgraph", "dev", "--host", "0.0.0.0", "--port", "2024",
            "--no-browser", "--no-reload", "--n-jobs-per-worker", "10"]


class Supervisor:
    def __init__(self, settings=None, cwd=None):
        self.settings = settings or Settings.from_env()
        self.cwd = Path(cwd or Path.cwd()).resolve()

    def run(self):
        if not self.settings.orchestrator_token:
            raise RuntimeError("Set ORCHESTRATOR_TOKEN before startup")
        prepare_persistence(self.cwd, self.settings.data_root)
        os.environ.update({name.upper(): str(getattr(self.settings, name))
                           for name in self.settings.__dataclass_fields__})
        os.chdir(self.cwd)
        from langgraph_cli.cli import cli

        # Uvicorn handles these during serving, then re-raises them on return.
        # Let Python finish finalizers after graceful shutdown (notably Queue
        # semaphore cleanup) instead of terminating on that re-raised signal.
        previous = {sig: signal.signal(sig, lambda *_: None)
                    for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            return cli.main(args=server_command(self.settings)[1:], prog_name="langgraph",
                            standalone_mode=False)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def main():
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    try:
        Supervisor().run()
    except Exception as exc:
        log.error("Startup failed (%s): %s", type(exc).__name__, exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
