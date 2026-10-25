"""Dashboard is a scoped request producer, never a task mutation API."""

import pytest
from fastapi.testclient import TestClient

from orchestrator.store import InMemoryStore
from orchestrator.web import app, events, get_store


@pytest.fixture
def web(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "dashboard-test-token")
    store = InMemoryStore()
    app.dependency_overrides[get_store] = lambda: store
    with TestClient(app) as client:
        yield client, store
    app.dependency_overrides.clear()


AUTH = {"Authorization": "Bearer dashboard-test-token"}


def test_api_fails_closed_and_static_does_not_embed_tokens(web, monkeypatch):
    client, _ = web
    assert client.get("/api/projects").status_code == 401
    assert client.get("/api/projects", headers={"Authorization": "Bearer runtime-secret"}).status_code == 401
    static = client.get("/dashboard")
    assert static.status_code == 200
    assert "dashboard-test-token" not in static.text
    assert "frame-ancestors 'none'" in static.headers["content-security-policy"]
    assert client.get("/dashboard/assets/app.js").status_code == 200
    monkeypatch.delenv("DASHBOARD_TOKEN")
    assert client.get("/api/projects", headers=AUTH).status_code == 503


@pytest.mark.asyncio
async def test_snapshot_never_reads_private_or_exposes_unknown_fields(web):
    client, store = web
    await store.put(("coding", "projects"), "p1", {"project_id": "p1", "title": "Project", "private_key": "secret"})
    await store.put(("coding", "p1", "tasks"), "t1", {
        "task_id": "t1", "agent": "EXECUTE", "title": "API", "status": "DONE",
        "outcome": "SUCCESS", "integrated": False, "private_prompt": "hidden prompt",
    })
    await store.put(("coding", "p1", "tasks"), "t2", {
        "task_id": "t2", "agent": "EXECUTE", "status": "TODO", "depends_on": ["t1"],
        "requires_integration": True,
    })
    await store.put(("coding", "p1", "private", "EXECUTE", "thread"), "scratch", {"text": "never visible"})
    await store.put(("coding", "p1", "events"), "e1", {"kind": "progress", "message": "Working", "raw_logs": "private log"})
    await store.put(("coding", "p2", "tasks"), "t-other", {"task_id": "not-this-project"})
    response = client.get("/api/projects/p1/snapshot", headers=AUTH)
    assert response.status_code == 200
    assert "hidden prompt" not in response.text
    assert "secret" not in response.text
    assert "never visible" not in response.text
    assert "private log" not in response.text
    assert "not-this-project" not in response.text
    task = next(t for t in response.json()["tasks"] if t["task_id"] == "t2")
    assert task["blocking_reasons"] == ["Dependency t1 has not been integrated"]


@pytest.mark.asyncio
async def test_human_actions_only_enqueue_control_requests(web, tmp_path, monkeypatch):
    client, store = web
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    repository = tmp_path / "repository"
    repository.mkdir()
    response = client.post("/api/projects", headers=AUTH, json={
        "project_id": "p1", "title": "Project", "repository_path": str(repository),
    })
    assert response.status_code == 202
    response = client.post("/api/projects/p1/messages", headers=AUTH, json={"message": "Build API"})
    assert response.status_code == 202
    await store.put(("coding", "p1", "approvals"), "approval-1", {"approval_id": "approval-1", "status": "PENDING"})
    response = client.post("/api/projects/p1/approvals/approval-1", headers=AUTH, json={"decision": "approve", "answer": "Yes"})
    assert response.status_code == 202
    requests = await store.search(("coding", "p1", "requests", "COMMUNICATOR"))
    assert {request["kind"] for request in requests} == {"register_project", "human_message", "approval"}
    assert all(request["request_id"] for request in requests)
    assert await store.search(("coding", "p1", "tasks")) == []
    assert (await store.get(("coding", "p1", "approvals"), "approval-1"))["status"] == "PENDING"
    # There is deliberately no browser route that mutates an authoritative task.
    assert client.post("/api/projects/p1/tasks/t1", headers=AUTH, json={"status": "CLOSED"}).status_code == 404


def test_rejects_paths_outside_workspace_and_oversized_or_extra_input(web, tmp_path, monkeypatch):
    client, _ = web
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("WORKSPACE_ROOT", str(root))
    for path in [str(outside), str(root / "escape")]:
        assert client.post("/api/projects", headers=AUTH, json={"project_id": "p", "title": "P", "repository_path": path}).status_code == 422
    assert client.post("/api/projects", headers=AUTH, json={"project_id": "../escape", "title": "P"}).status_code == 422
    assert client.post("/api/projects/p/messages", headers=AUTH, json={"message": "x" * 20001}).status_code == 422
    assert client.post("/api/projects/p/messages", headers=AUTH, json={"message": "Hi", "status": "CLOSED"}).status_code == 422
    assert client.post("/api/projects/p/approvals/a", headers=AUTH, json={"decision": "anything"}).status_code == 422


@pytest.mark.asyncio
async def test_artifacts_only_read_explicitly_shared_records(web):
    client, store = web
    await store.put(("coding", "p1", "shared", "artifacts"), "report", {"artifact_id": "report", "content": "Approved report", "internal_path": "/data/private.log"})
    response = client.get("/api/projects/p1/artifacts/report", headers=AUTH)
    assert response.json() == {"artifact_id": "report", "content": "Approved report"}
    assert client.get("/api/projects/p1/artifacts/private", headers=AUTH).status_code == 404


@pytest.mark.asyncio
async def test_stream_publishes_only_public_event_projection():
    store = InMemoryStore()
    await store.put(("coding", "p1", "events"), "e", {"kind": "progress", "message": "Review complete", "raw_logs": "private scratchpad"})

    class ConnectedRequest:
        async def is_disconnected(self):
            return False

    response = await events("p1", ConnectedRequest(), store)
    iterator = response.body_iterator
    try:
        chunk = await anext(iterator)
        assert "Review complete" in chunk
        assert "private scratchpad" not in chunk
        assert chunk.startswith("data: ")
    finally:
        await iterator.aclose()


@pytest.mark.asyncio
async def test_snapshot_resolves_rework_dependency_without_rewriting_history(web):
    client, store = web
    await store.put(("coding", "projects"), "p1", {"project_id": "p1", "title": "Rework"})
    await store.put(("coding", "p1", "tasks"), "old", {
        "task_id": "old", "agent": "EXECUTE", "status": "DONE", "outcome": "CHANGES_REQUESTED",
        "replaced_by_task_id": "fixed",
    })
    await store.put(("coding", "p1", "tasks"), "fixed", {
        "task_id": "fixed", "agent": "EXECUTE", "status": "TODO", "outcome": None,
    })
    await store.put(("coding", "p1", "tasks"), "verify", {
        "task_id": "verify", "agent": "VERIFY_INTEGRATE", "status": "TODO", "depends_on": ["old"],
    })
    first = client.get("/api/projects/p1/snapshot", headers=AUTH).json()
    verify = next(task for task in first["tasks"] if task["task_id"] == "verify")
    assert not verify["ready"]
    assert verify["blocking_reasons"] == ["Dependency old (replacement fixed) has not succeeded"]
    await store.put(("coding", "p1", "tasks"), "fixed", {
        "task_id": "fixed", "agent": "EXECUTE", "status": "DONE", "outcome": "SUCCESS", "integrated": False,
    })
    second = client.get("/api/projects/p1/snapshot", headers=AUTH).json()
    verify = next(task for task in second["tasks"] if task["task_id"] == "verify")
    assert verify["ready"]  # Verify itself supplies the integration barrier.
    assert verify["blocking_reasons"] == []
    old = next(task for task in second["tasks"] if task["task_id"] == "old")
    assert old["outcome"] == "CHANGES_REQUESTED" and old["replaced_by_task_id"] == "fixed"


async def test_snapshot_shows_human_conversation_and_timeout_but_not_system_messages(web):
    client, store = web
    await store.put(("coding", "projects"), "p1", {"project_id": "p1"})
    await store.put(("coding", "p1", "conversations"), "chain", {
        "execution_chain_id": "chain", "Messages": [
            {"type": "human", "data": {"content": "Build a portal"}},
            {"type": "ai", "data": {"content": "Who uses it?"}},
            {"type": "system", "data": {"content": "Internal timeout instructions"}},
        ], "private_field": "hidden",
    })
    await store.put(("coding", "p1", "approvals"), "approval", {
        "approval_id": "approval", "status": "TIMED_OUT", "expires_at": 1800,
    })
    response = client.get("/api/projects/p1/snapshot", headers=AUTH)
    assert response.json()["conversations"][0]["Messages"] == [
        {"type": "human", "content": "Build a portal"}, {"type": "ai", "content": "Who uses it?"}]
    assert "Internal timeout instructions" not in response.text and "hidden" not in response.text
    assert response.json()["approvals"][0]["expires_at"] == 1800
