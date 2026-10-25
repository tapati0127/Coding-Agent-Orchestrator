"""Human clarification releases capacity and resumes only after a decision."""
import uuid

from orchestrator.dispatcher.engine import Dispatcher
from orchestrator.store import InMemoryStore


async def test_intake_clarification_approval_resume_without_waiting_worker():
    store = InMemoryStore()
    dispatcher = Dispatcher(store, clock=lambda: 1000)

    async def send(kind, payload=None, agent="COMMUNICATOR", **extra):
        return await dispatcher.handle({"request_id": uuid.uuid4().hex,
            "kind": kind, "project_id": "clarify", "agent": agent,
            "payload": payload or {}, **extra})

    assert (await send("register_project", {"title": "Clarification fixture"}))["accepted"]
    assert (await send("human_message", {"message": "Build login with unspecified identity provider"}))["accepted"]
    claim = await send("claim", agent="ANALYZE", worker_id="analyze-01")
    task = claim["task"]
    result = await send("result", {
        "schema_version": "1.0", "project_id": "clarify",
        "source_task_id": task["task_id"], "producer_agent": "ANALYZE",
        "execution_chain_id": task["execution_chain_id"],
        "summary": "Build login; identity provider needs a human decision",
        "source_result": {"outcome": "NEED_ANALYSIS"},
        "human_request": {"question": "Which identity provider should login use?"},
    }, agent="ANALYZE", worker_id="analyze-01", task_id=task["task_id"])
    assert result["accepted"]
    saved = (await dispatcher.tasks("clarify"))[task["task_id"]]
    assert saved.status == "DONE" and saved.outcome == "NEED_ANALYSIS"
    assert saved.lease_expires_at is None
    assert (await store.get(("coding", "clarify", "workers"), "analyze-01"))["status"] == "IDLE"
    approval = (await store.search(("coding", "clarify", "approvals")))[0]
    assert approval["status"] == "PENDING"
    assert not (await send("claim", agent="ANALYZE", worker_id="analyze-01"))["accepted"]
    assert (await send("approval", {"approval_id": approval["approval_id"],
                                    "decision": "approve", "answer": "Use our internal OIDC provider"}))["accepted"]
    resumed = (await send("claim", agent="ANALYZE", worker_id="analyze-01"))["task"]
    assert "internal OIDC" in resumed["description"]
    assert resumed["execution_chain_id"] != task["execution_chain_id"]
    assert not (await send("approval", {"approval_id": approval["approval_id"],
                                        "decision": "approve", "answer": "duplicate"}))["accepted"]
