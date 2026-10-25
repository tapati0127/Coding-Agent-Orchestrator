"""Communicator observes Store changes without poll-triggered model calls."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from langchain_core.messages import AIMessage, HumanMessage
import pytest

from orchestrator.graphs import progress
from orchestrator.dispatcher.engine import identifier
from orchestrator.store import InMemoryStore


async def follow(monkeypatch, snapshots, *, previous_chain=None, approval=None, replay=False):
    store = InMemoryStore()
    await store.put(("coding", "projects"), "p", {"project_id": "p"})
    request_id = identifier("user-1", "chat")
    if approval:
        await store.put(("coding", "p", "approvals"), "a1", approval)
    if replay:
        await store.put(("coding", "p", "responses", "COMMUNICATOR"), request_id,
                        {"accepted": True, "task_ids": ["original"]})
        await store.put(("coding", "p", "tasks"), "original", {"execution_chain_id": "original-chain"})

    async def submit(pid, kind, payload, *, request_id):
        await store.put(("coding", pid, "responses", "COMMUNICATOR"), request_id, {"accepted": True})

    dispatcher = SimpleNamespace(submit=AsyncMock(side_effect=submit))
    runtime = SimpleNamespace(chat=AsyncMock(return_value=AIMessage(content="Update", id="reply")))
    reader = AsyncMock(side_effect=snapshots)
    monkeypatch.setattr(progress, "SDKStore", lambda: store)
    monkeypatch.setattr(progress, "get_dispatcher", lambda: dispatcher)
    monkeypatch.setattr(progress, "read_progress", reader)
    monkeypatch.setattr(progress.asyncio, "sleep", AsyncMock())
    events = []
    human = HumanMessage(content="Build API", id="user-1")
    result = await progress.follow_progress({"project_id": "p", "messages": [human],
        "execution_chain_id": previous_chain}, runtime, [human], events.append)
    return result, runtime, dispatcher, events, reader


def snapshot(status="DOING", *, chain="chain", task_id="t", approvals=None):
    return {"tasks": [{"task_id": task_id, "status": status}], "approvals": approvals or []}


async def test_only_changes_invoke_model_and_final_status_ends_stream(monkeypatch):
    result, runtime, dispatcher, events, reader = await follow(monkeypatch, [
        snapshot(), snapshot(), snapshot("CLOSED")])
    assert reader.await_count == 3
    assert runtime.chat.await_count == 2
    assert all(call.kwargs == {"stream": True} for call in runtime.chat.call_args_list)
    assert [e["status"] for e in events if e["type"] == "status"] == ["submitted", "running", "completed"]
    assert "messages" not in result
    dispatcher.submit.assert_awaited_once()


async def test_pending_question_is_streamed_and_releases_chat_run(monkeypatch):
    pending = {"approval_id": "a1", "status": "PENDING", "question": "Who uses it?"}
    result, runtime, _, events, _ = await follow(monkeypatch, [snapshot("DONE", approvals=[pending])])
    assert events[1]["status"] == "waiting_for_user"
    assert "Who uses it?" in runtime.chat.call_args.args[0][-1].content
    assert result["execution_chain_id"]


async def test_reply_resumes_pending_clarification_in_same_chain(monkeypatch):
    pending = {"approval_id": "a1", "status": "PENDING"}
    result, _, dispatcher, _, _ = await follow(monkeypatch, [
        snapshot("DONE", approvals=[pending]), snapshot("CLOSED")], previous_chain="chain",
        approval={**pending, "producer_agent": "COMMUNICATOR", "execution_chain_id": "chain"})
    assert result == {"execution_chain_id": "chain"}
    args = dispatcher.submit.call_args
    assert args.args == ("p", "human_reply", {"approval_id": "a1", "answer": "Build API"})


async def test_stalled_chain_does_not_wait_forever_or_reinvoke_each_poll(monkeypatch):
    _, runtime, _, events, _ = await follow(monkeypatch, [snapshot("DONE")] * 3)
    assert runtime.chat.await_count == 2
    assert events[-2]["status"] == "blocked"


async def test_progress_is_chain_scoped_and_excludes_private_fields():
    store = InMemoryStore()
    for tid, chain in (("t1", "a"), ("t2", "b")):
        await store.put(("coding", "p", "tasks"), tid, {"task_id": tid,
            "execution_chain_id": chain, "title": tid, "status": "DOING", "private_prompt": "secret"})
    result = await progress.read_progress(store, "p", "a")
    assert result == {"tasks": [{"task_id": "t1", "title": "t1", "status": "DOING"}], "approvals": []}


async def test_retry_observes_original_chain_without_resubmitting_work(monkeypatch):
    result, _, dispatcher, _, _ = await follow(monkeypatch, [snapshot("CLOSED")],
                                               previous_chain="other", replay=True)
    assert result == {"execution_chain_id": "original-chain"}
    dispatcher.submit.assert_not_awaited()
