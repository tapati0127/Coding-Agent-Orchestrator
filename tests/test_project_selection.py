from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from orchestrator.dispatcher.engine import Dispatcher
from orchestrator.graphs import communicator, progress, projects
from orchestrator.store import StoreRecords


@pytest.fixture
def session(monkeypatch, tmp_path):
    store = InMemoryStore()
    engine = Dispatcher(StoreRecords(store))
    async def submit(pid, kind, payload, *, request_id):
        return await engine.handle({"project_id": pid, "kind": kind, "payload": payload,
                                    "agent": "COMMUNICATOR", "request_id": request_id})
    dispatcher = SimpleNamespace(submit=AsyncMock(side_effect=submit))
    monkeypatch.setattr(progress, "get_dispatcher", lambda: dispatcher)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data"))
    follow = AsyncMock(return_value={"execution_chain_id": "chain", "messages": [AIMessage(content="Who uses it?", id="question")]})
    monkeypatch.setattr(communicator, "follow_progress", follow)
    graph = communicator.builder.compile(store=store, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "session"}}
    return store, graph, config, dispatcher, follow


async def seed(store, pid, title):
    await store.aput(("coding", "projects"), pid, {"project_id": pid, "title": title})


async def test_list_choose_and_continue_using_only_new_user_messages(session):
    store, graph, config, dispatcher, follow = session
    await seed(store, "portal", "Sales portal")
    await seed(store, "api", "API")
    menu = await graph.ainvoke({"messages": [("user", "Xin chào")]}, config)
    assert "1. API" in menu["messages"][-1].content
    assert "2. Sales portal" in menu["messages"][-1].content
    assert not menu.get("project_id")
    dispatcher.submit.assert_not_awaited()
    selected = await graph.ainvoke({"messages": [("user", "2")]}, config)
    assert selected["project_id"] == "portal"
    follow.assert_not_awaited()
    result = await graph.ainvoke({"messages": [("user", "Add login")]}, config)
    assert result["project_id"] == "portal"
    assert result["execution_chain_id"] == "chain"
    assert result["messages"][-1].content == "Who uses it?"
    assert follow.call_args.args[0]["project_id"] == "portal"
    assert len([m for m in result["messages"] if m.content == "Who uses it?"]) == 1


async def test_empty_catalog_creates_workspace_and_retry_does_not_duplicate(session):
    store, graph, config, dispatcher, follow = session
    message = HumanMessage(content="Xin chào", id="first")
    result = await graph.ainvoke({"messages": [message]}, config)
    pid = result["project_id"]
    project = (await store.aget(("coding", "projects"), pid)).value
    assert Path(project["repository_path"]).name == pid
    assert (Path(project["repository_path"]) / ".git").is_dir()
    assert "Đã tạo project mới" in result["messages"][-1].content
    await graph.ainvoke({"messages": [message]}, config)
    assert len(await projects.project_catalog(store)) == 1
    dispatcher.submit.assert_awaited_once()
    follow.assert_not_awaited()


async def test_create_new_project_from_an_active_project_resets_chain_and_history_boundary(session):
    store, graph, config, dispatcher, follow = session
    await seed(store, "old", "Existing project")
    await graph.ainvoke({"messages": [("user", "Xin chào")]}, config)
    await graph.ainvoke({"messages": [("user", "1")]}, config)
    await graph.ainvoke({"messages": [("user", "OLD_PROJECT_REQUIREMENT")]}, config)
    result = await graph.ainvoke({"messages": [("user", "tạo project mới")]}, config)
    assert result["project_id"] != "old"
    assert result["execution_chain_id"] is None
    scoped = result["messages"][result["project_message_start"]:]
    assert all("OLD_PROJECT_REQUIREMENT" not in message.content for message in scoped)
    assert dispatcher.submit.await_args.args[1] == "register_project"


async def test_number_uses_the_menu_snapshot_and_existing_question_is_shown(session):
    store, graph, config, _, _ = session
    await seed(store, "old", "B portal")
    await store.aput(("coding", "old", "approvals"), "pending", {
        "status": "PENDING", "question": "Who will use the portal?", "created_at": 1})
    await graph.ainvoke({"messages": [("user", "Xin chào")]}, config)
    await seed(store, "new", "A portal")
    result = await graph.ainvoke({"messages": [("user", "1")]}, config)
    assert result["project_id"] == "old"
    assert "Who will use the portal?" in result["messages"][-1].content


async def test_unknown_or_invented_selection_does_not_bind_project(session, monkeypatch):
    store, graph, config, dispatcher, follow = session
    await seed(store, "old", "Portal")
    await graph.ainvoke({"messages": [("user", "Xin chào")]}, config)
    model = SimpleNamespace(ainvoke=AsyncMock(return_value={"action": "select", "project_id": "invented"}))
    monkeypatch.setattr(projects, "DecisionRunner", lambda: model)
    result = await graph.ainvoke({"messages": [("user", "chọn project không tồn tại")]}, config)
    assert not result.get("project_id")
    assert "Bạn muốn tiếp tục project nào" in result["messages"][-1].content
    dispatcher.submit.assert_not_awaited()
    follow.assert_not_awaited()


async def test_natural_language_selection_is_validated_against_catalog(session, monkeypatch):
    store, graph, config, _, _ = session
    await seed(store, "old", "Portal")
    await graph.ainvoke({"messages": [("user", "Xin chào")]}, config)
    model = SimpleNamespace(ainvoke=AsyncMock(return_value={"action": "select", "project_id": "old"}))
    monkeypatch.setattr(projects, "DecisionRunner", lambda: model)
    result = await graph.ainvoke({"messages": [("user", "Tôi muốn tiếp tục portal hôm trước")]}, config)
    assert result["project_id"] == "old"
    model.ainvoke.assert_awaited_once()


async def test_catalog_lists_all_pages(session):
    store, graph, config, _, _ = session
    for index in range(105):
        await seed(store, f"p{index:03}", f"Project {index:03}")
    result = await graph.ainvoke({"messages": [("user", "Xin chào")]}, config)
    assert len(result["project_options"]) == 105
    assert "105. Project 104" in result["messages"][-1].content
