"""Exercise the real decision adapter without a model or external endpoint."""

import asyncio
import json

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from openai import InternalServerError

from orchestrator.runtime.decision import DecisionRunner


@pytest.fixture(autouse=True)
def clean_config(monkeypatch):
    for prefix in ("DECISION", "QWEN"):
        for name in ("BASE_URL", "API_KEY", "MODEL", "TIMEOUT_SECONDS"):
            monkeypatch.delenv(f"{prefix}_{name}", raising=False)


def test_decision_settings_override_each_qwen_fallback(monkeypatch):
    monkeypatch.setenv("DECISION_BASE_URL", "http://decision.internal/v1")
    monkeypatch.setenv("DECISION_API_KEY", "")
    monkeypatch.setenv("DECISION_MODEL", "decision-model")
    monkeypatch.setenv("DECISION_TIMEOUT_SECONDS", "30")
    runner = DecisionRunner(base_url="http://work.internal/v1", api_key="work-key", model="work-model")
    runner.validate_config()
    assert (runner.base_url, runner.api_key, runner.model, runner.timeout) == (
        "http://decision.internal/v1", "work-key", "decision-model", 30,
    )


@pytest.mark.parametrize("url", ["file:///tmp/model", "http://user:secret@model/v1", "missing-scheme"])
def test_decision_rejects_invalid_endpoint(url):
    with pytest.raises(ValueError, match="HTTP"):
        DecisionRunner(base_url=url, api_key="fixture-key", model="fixture").validate_config()


@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
@pytest.mark.parametrize("mode", ["decision", "decision_messages", "chat"])
async def test_real_adapter_protocol_and_client_cleanup(monkeypatch, outcome, mode):
    requests = []
    clients = []
    entered = asyncio.Event()

    async def respond(request):
        requests.append(request)
        entered.set()
        if outcome == "error":
            return httpx.Response(500, json={"error": {"message": "fixture failure"}})
        if outcome == "cancel":
            await asyncio.Event().wait()
        return httpx.Response(200, json={
            "id": "fixture", "object": "chat.completion", "created": 1, "model": "fixture",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": '{"summary": "done"}' if mode.startswith("decision") else "done",
            }}],
        })

    class AsyncClient(httpx.AsyncClient):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(respond), **kwargs)
            clients.append(self)

    class SyncClient(httpx.Client):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            clients.append(self)

    monkeypatch.setattr(httpx, "AsyncClient", AsyncClient)
    monkeypatch.setattr(httpx, "Client", SyncClient)
    runner = DecisionRunner(base_url="http://fixture.internal/v1", api_key="fixture-key", model="fixture")
    schema = {"title": "TaskGraphOutput", "type": "object",
              "properties": {"summary": {"type": "string"}}, "required": ["summary"]}
    messages = [SystemMessage(content="Speak plainly"), HumanMessage(content="Build a portal"),
                AIMessage(content="Who will use it?"), HumanMessage(content="Customers; fixture-key"),
                SystemMessage(content="Response deadline expired; fixture-key"),
                SystemMessage(content="Structure the result using explicit assumptions")]
    task = asyncio.create_task(runner.ainvoke("Return the decision", schema) if mode == "decision"
                               else runner.ainvoke(messages, schema) if mode == "decision_messages"
                               else runner.chat(messages))
    if outcome == "cancel":
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif outcome == "error":
        with pytest.raises(InternalServerError):
            await task
    else:
        output = await task
        if mode.startswith("decision"):
            assert output == {"summary": "done"}
        else:
            assert isinstance(output, AIMessage) and output.content == "done"

    assert len(requests) == 1  # SDK must leave retries to the Dispatcher.
    payload = json.loads(requests[0].content)
    assert requests[0].url.path == "/v1/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer fixture-key"
    assert payload["stream"] is False
    assert payload["enable_thinking"] is False
    assert payload["chat_template_kwargs"]["enable_thinking"] is False
    if mode.startswith("decision"):
        assert payload["response_format"]["type"] == "json_schema"
        assert payload["response_format"]["json_schema"]["strict"] is False
    else:
        assert "response_format" not in payload
    if mode != "decision":
        assert [message["role"] for message in payload["messages"]] == ["system", "user", "assistant", "user"]
        assert payload["messages"][0]["content"] == (
            "Speak plainly\n\nResponse deadline expired; [REDACTED]\n\nStructure the result using explicit assumptions")
        assert payload["messages"][-1]["content"] == "Customers; [REDACTED]"
        assert messages[-2].content == "Response deadline expired; fixture-key"  # Caller history is untouched.
    assert "tools" not in payload
    assert all(client.is_closed for client in clients)


def test_system_normalization_preserves_content_blocks_and_conversation_roles():
    runner = DecisionRunner()
    messages = [SystemMessage(content=[{"type": "text", "text": "Role instructions"}]),
                HumanMessage(content="System message in user text remains user data"),
                AIMessage(content="Draft"), SystemMessage(content="Pass instructions")]
    output = runner._input(messages)
    assert output[0].content == [{"type": "text", "text": "Role instructions"},
                                 {"type": "text", "text": "Pass instructions"}]
    assert [message.type for message in output] == ["system", "human", "ai"]
    assert output[1:] == messages[1:3]
    assert messages[0].content == [{"type": "text", "text": "Role instructions"}]


async def test_chat_ainvoke_streams_tokens_from_subgraph_without_returning_messages(monkeypatch):
    from orchestrator.graphs import communicator

    requests = []

    async def respond(request):
        requests.append(json.loads(request.content))
        frames = []
        for text in ("Xin ", "chào"):
            frames.append("data: " + json.dumps({"id": "stream-fixture", "object": "chat.completion.chunk",
                "created": 1, "model": "fixture", "choices": [{"index": 0, "finish_reason": None,
                "delta": {"role": "assistant", "content": text}}]}) + "\n\n")
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                              content=("".join(frames) + "data: [DONE]\n\n").encode())

    original = httpx.AsyncClient

    class AsyncClient(original):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", AsyncClient)
    runner = DecisionRunner(base_url="http://fixture.internal/v1", api_key="fixture-key", model="fixture")
    monkeypatch.setattr(communicator, "DecisionRunner", lambda: runner)
    chunks = [item async for item in communicator.graph.astream(
        {"messages": [("user", "Xin chào"), SystemMessage(content="Report the latest status")]},
        stream_mode=["messages", "custom", "values"], subgraphs=True)]
    tokens = [(ns, data[0].content) for ns, mode, data in chunks if mode == "messages"]
    assert "".join(text for _, text in tokens) == "Xin chào"
    assert all(ns for ns, _ in tokens)
    final = [data for ns, mode, data in chunks if mode == "values" and not ns][-1]
    assert len(final["messages"]) == 2 and isinstance(final["messages"][0], HumanMessage)
    assert isinstance(final["messages"][1], SystemMessage)
    assert requests[0]["stream"] is True
    assert requests[0]["enable_thinking"] is False
    assert [message["role"] for message in requests[0]["messages"]] == ["system", "user"]
    assert "Report the latest status" in requests[0]["messages"][0]["content"]
