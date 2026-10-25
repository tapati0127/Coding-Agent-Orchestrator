"""Communicator uses the existing chain budget and durable approval lifecycle."""
import uuid

import pytest
from langchain_core.messages import messages_from_dict

from orchestrator.dispatcher.engine import Dispatcher
from orchestrator.runtime.context_scope import ScopedContext
from orchestrator.store import InMemoryStore


class Conversation:
    def __init__(self, **options):
        self.now = 1000.
        self.store = InMemoryStore()
        self.dispatcher = Dispatcher(self.store, clock=lambda: self.now, **options)

    async def send(self, kind, payload=None, agent="COMMUNICATOR", **extra):
        return await self.dispatcher.handle({"request_id": uuid.uuid4().hex,
            "kind": kind, "project_id": "chat", "agent": agent,
            "payload": payload or {}, **extra})

    async def start(self):
        assert (await self.send("register_project", {"title": "Chat"}))["accepted"]
        assert (await self.send("human_message", {"message": "PRIVATE_USER_REQUEST"}))["accepted"]
        return await self.claim()

    async def claim(self, agent="COMMUNICATOR"):
        receipt = await self.send("claim", agent=agent, worker_id=agent.lower())
        assert receipt["accepted"], receipt
        return receipt["task"]

    async def result(self, task, **extra):
        return await self.send("result", {
            "project_id": "chat", "source_task_id": task["task_id"],
            "producer_agent": task["agent"], "execution_chain_id": task["execution_chain_id"],
            "summary": "User-oriented brief", "source_result": {"outcome": "SUCCESS"},
            **extra,
        }, agent=task["agent"], worker_id=task["agent"].lower(), task_id=task["task_id"])

    async def pending(self):
        return next(a for a in await self.store.search(("coding", "chat", "approvals")) if a["status"] == "PENDING")

    async def history(self, chain):
        record = await self.store.get(("coding", "chat", "conversations"), chain)
        return messages_from_dict(record["Messages"])


async def test_intake_asks_human_then_hands_off_in_same_chain():
    chat = Conversation()
    task = await chat.start()
    chain = task["execution_chain_id"]
    assert task["agent"] == "COMMUNICATOR"
    assert "PRIVATE_USER_REQUEST" not in str(await chat.dispatcher.tasks("chat"))
    assert (await chat.result(task, human_request={"question": "Who will use it?"}))["accepted"]
    saved = (await chat.dispatcher.tasks("chat"))[task["task_id"]]
    assert saved.status == "DONE" and saved.outcome == "SUCCESS" and saved.lease_expires_at is None
    assert (await chat.store.get(("coding", "chat", "workers"), "communicator"))["status"] == "IDLE"
    assert not (await chat.send("claim", worker_id="communicator"))["accepted"]
    approval = await chat.pending()
    assert approval["expires_at"] == 2800.
    reply = {"approval_id": approval["approval_id"], "decision": "approve", "answer": "Our customers"}
    assert (await chat.send("approval", reply))["accepted"]
    assert not (await chat.send("approval", reply))["accepted"]
    resumed = await chat.claim()
    assert resumed["execution_chain_id"] == chain
    assert resumed["backward_count"] == 1
    assert [m.content for m in await chat.history(chain)] == ["PRIVATE_USER_REQUEST", "Who will use it?", "Our customers"]
    assert (await chat.result(resumed, nodes=[{"id": "analyze", "target_agent": "ANALYZE", "title": "Analyze customer portal", "description": "A portal for customers"}]))["accepted"]
    analyze = await chat.claim("ANALYZE")
    assert analyze["execution_chain_id"] == chain


async def test_self_and_human_share_backward_budget_and_reject_fourth_round():
    chat = Conversation(max_backward=3)
    task = await chat.start()
    assert (await chat.result(task, nodes=[{"id": "self", "target_agent": "COMMUNICATOR", "title": "Refine brief", "reason": "Clarify desired outcome"}]))["accepted"]
    for count in (2, 3):
        task = await chat.claim()
        assert (await chat.result(task, human_request={"question": f"Clarification {count}?"}))["accepted"]
        approval = await chat.pending()
        assert (await chat.send("approval", {"approval_id": approval["approval_id"], "decision": "approve", "answer": "Yes"}))["accepted"]
    task = await chat.claim()
    chain = task["execution_chain_id"]
    assert task["backward_count"] == 3
    receipt = await chat.result(task, human_request={"question": "Fourth question?"})
    assert not receipt["accepted"] and "backward budget" in receipt["reason"]
    assert len(await chat.store.search(("coding", "chat", "approvals"))) == 2
    assert (await chat.store.get(("coding", "chat", "chains"), chain))["backward_count"] == 3
    assert (await chat.result(task, nodes=[{"id": "analyze", "target_agent": "ANALYZE", "title": "Analyze with assumptions"}], notification={"message": "I will assume the audience is your customers."}))["accepted"]


async def test_timeout_is_idempotent_and_does_not_invent_human_approval():
    chat = Conversation(human_response_timeout_seconds=1800)
    task = await chat.start()
    assert (await chat.result(task, human_request={"question": "Who will use it?"}))["accepted"]
    approval = await chat.pending()
    chat.now = approval["expires_at"] - 1
    await chat.dispatcher.recover("chat")
    assert not (await chat.send("claim", worker_id="communicator"))["accepted"]
    chat.now += 1
    await chat.dispatcher.recover("chat")
    await chat.dispatcher.recover("chat")
    updated = await chat.store.get(("coding", "chat", "approvals"), approval["approval_id"])
    assert updated["status"] == "TIMED_OUT" and updated["answer"] == ""
    assert len(await chat.dispatcher.tasks("chat")) == 2
    assert not (await chat.send("approval", {"approval_id": approval["approval_id"], "decision": "approve", "answer": "Late reply"}))["accepted"]
    resumed = await chat.claim()
    assert resumed["execution_chain_id"] == task["execution_chain_id"]
    assert resumed["backward_count"] == 1
    history = await chat.history(task["execution_chain_id"])
    assert history[-1].type == "system" and "no human approval" in history[-1].content
    assert not (await chat.result(resumed, human_request={"question": "Ask again?"}))["accepted"]
    assert (await chat.result(resumed, nodes=[{"id": "analyze", "target_agent": "ANALYZE", "title": "Analyze with assumptions"}], notification={"message": "No reply yet; I assume the audience is customers."}))["accepted"]


async def test_human_approval_node_uses_same_question_budget():
    chat = Conversation()
    task = await chat.start()
    assert (await chat.result(task, nodes=[{"id": "ask", "node_type": "HUMAN_APPROVAL", "target_agent": "COMMUNICATOR", "title": "Confirm the audience?"}]))["accepted"]
    assert (await chat.pending())["question"] == "Confirm the audience?"
    assert (await chat.store.get(("coding", "chat", "chains"), task["execution_chain_id"]))["backward_count"] == 1


@pytest.mark.parametrize("agent", ["COMMUNICATOR", "ANALYZE", "PLAN_DELEGATE", "EXECUTE", "VERIFY_INTEGRATE", "FINALIZE"])
async def test_history_visible_only_to_three_roles_and_scoped_to_chain(agent):
    chat = Conversation()
    task = await chat.start()
    await chat.store.put(("coding", "chat", "conversations"), "other-chain", {"Messages": [{"type": "human", "data": {"content": "OTHER_CHAIN_SECRET"}}]})
    reads = []
    original = chat.store.get

    async def track(ns, key):
        reads.append((ns, key))
        return await original(ns, key)

    chat.store.get = track
    context = await ScopedContext(chat.store).build("chat", agent, "thread", "execute-01", execution_chain_id=task["execution_chain_id"])
    allowed = agent in {"COMMUNICATOR", "ANALYZE", "FINALIZE"}
    assert ("PRIVATE_USER_REQUEST" in str(context)) == allowed
    assert "OTHER_CHAIN_SECRET" not in str(context)
    assert any(ns[-1] == "conversations" for ns, _ in reads) == allowed


@pytest.mark.parametrize("target", ["PLAN_DELEGATE", "EXECUTE", "VERIFY_INTEGRATE", "FINALIZE"])
async def test_communicator_cannot_delegate_to_technical_roles_other_than_analyze(target):
    chat = Conversation()
    task = await chat.start()
    assert not (await chat.result(task, nodes=[{"id": "bad", "target_agent": target, "title": "Bad route"}]))["accepted"]


async def test_rejection_stops_conversation_without_timeout_resuming_it():
    chat = Conversation()
    task = await chat.start()
    assert (await chat.result(task, human_request={"question": "Proceed?"}))["accepted"]
    approval = await chat.pending()
    assert (await chat.send("approval", {"approval_id": approval["approval_id"], "decision": "reject", "answer": "Cancel"}))["accepted"]
    chat.now = approval["expires_at"] + 1
    await chat.dispatcher.recover("chat")
    assert len(await chat.dispatcher.tasks("chat")) == 1
    assert (await chat.history(task["execution_chain_id"]))[-1].content == "Cancel"


async def test_timeout_replays_after_partial_write_without_duplicate_task_or_message():
    chat = Conversation(human_response_timeout_seconds=10)
    task = await chat.start()
    assert (await chat.result(task, human_request={"question": "Who uses it?"}))["accepted"]
    approval = await chat.pending()
    chat.now = approval["expires_at"]
    put = chat.store.put
    interrupted = False

    async def interrupt(ns, key, value):
        nonlocal interrupted
        if ns[-1] == "conversations" and not interrupted:
            interrupted = True
            raise OSError("crash after timeout approval was written")
        await put(ns, key, value)

    chat.store.put = interrupt
    with pytest.raises(OSError, match="crash"):
        await chat.dispatcher.recover("chat")
    chat.store.put = put
    chat.dispatcher = Dispatcher(chat.store, clock=lambda: chat.now, human_response_timeout_seconds=10)
    await chat.dispatcher.recover("chat")
    await chat.dispatcher.recover("chat")
    assert len(await chat.dispatcher.tasks("chat")) == 2
    history = await chat.history(task["execution_chain_id"])
    assert len(history) == 3 and history[-1].type == "system"
    assert (await chat.store.get(("coding", "chat", "chains"), task["execution_chain_id"]))["backward_count"] == 1


async def test_exhausted_budget_requires_user_visible_assumptions():
    chat = Conversation(max_backward=0)
    task = await chat.start()
    assert not (await chat.result(task, human_request={"question": "Not allowed?"}))["accepted"]
    node = {"id": "analyze", "target_agent": "ANALYZE", "title": "Analyze the brief"}
    assert not (await chat.result(task, nodes=[node]))["accepted"]
    assert (await chat.result(task, nodes=[node], notification={"message": "I assume this is for your internal team."}))["accepted"]
