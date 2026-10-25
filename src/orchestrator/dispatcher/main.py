"""Internal process; no orchestration HTTP server is started here."""
import asyncio
import fcntl
import logging
import os
from pathlib import Path

from orchestrator.store import SDKStore
from .engine import Dispatcher


async def run():
    data = Path(os.getenv("DATA_ROOT", "/data"))
    data.mkdir(parents=True, exist_ok=True)
    # Hold the descriptor for the lifetime of the process, rejecting accidental
    # second dispatchers sharing this data volume.
    with (data / "dispatcher.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        dispatcher = Dispatcher(SDKStore(),
            max_qwen_sessions=int(os.getenv("MAX_QWEN_SESSIONS", "5")),
            max_execute_workers=int(os.getenv("MAX_EXECUTE_WORKERS", "5")),
            max_backward=int(os.getenv("MAX_BACKWARD_DELEGATIONS", "3")),
            max_attempts=int(os.getenv("MAX_TECHNICAL_RETRIES", "3")),
            lease_seconds=int(os.getenv("TASK_LEASE_SECONDS", "600")),
            human_response_timeout_seconds=int(os.getenv("HUMAN_RESPONSE_TIMEOUT_SECONDS", "1800")))
        while True:
            try:
                # Discover exact request namespaces, including not-yet-registered
                # projects. Never scan private context or ever-growing journals.
                for inbox_ns in await dispatcher.store.inboxes():
                    inbox = await dispatcher.store.search(inbox_ns)
                    for envelope in sorted(inbox, key=lambda r: (r.get("submitted_at", 0), r["request_id"])):
                        if envelope.get("agent", "COMMUNICATOR") != inbox_ns[-1]:
                            continue
                        await dispatcher.handle(envelope)
                        # Receipt and replay journal retain idempotency/history.
                        await dispatcher.store.delete(inbox_ns, envelope["request_id"])
                for project in await dispatcher.store.search(("coding", "projects")):
                    await dispatcher.recover(project["project_id"])
            except Exception:
                logging.exception("dispatcher polling failed")
            await asyncio.sleep(float(os.getenv("DISPATCH_POLL_SECONDS", "2")))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
