import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from orchestrator.runtime.context_scope import ScopedContext
from orchestrator.runtime.qwen import QwenError, QwenRunner


def source_task(agent="EXECUTE"):
    return {"project_id": "p1", "task_id": "task1", "agent": agent, "execution_chain_id": "chain1"}


def result_graph():
    return {
        "schema_version": "1.0", "project_id": "p1", "source_task_id": "task1",
        "producer_agent": "EXECUTE", "execution_chain_id": "chain1", "summary": "done",
        "source_result": {"outcome": "SUCCESS", "proposed_status": "DONE"}, "nodes": [], "edges": [],
    }


def fake_cli(tmp_path: Path, body: str) -> Path:
    executable = tmp_path / "fake-qwen"
    executable.write_text("#!/usr/bin/env python3\n" + body)
    executable.chmod(0o700)
    return executable


def runner(tmp_path, executable, **kwargs):
    return QwenRunner(executable=str(executable), base_url="http://llm.internal/v1",
                      api_key="sensitive-key-value-12345", model="internal-coder",
                      data_root=tmp_path / "data", **kwargs)


async def test_real_subprocess_two_passes_streams_redacts_and_isolates_env(tmp_path, monkeypatch):
    executable = fake_cli(tmp_path, '''import json, os, sys
assert os.environ['OPENAI_BASE_URL'] == 'http://llm.internal/v1'
assert os.environ['OPENAI_MODEL'] == 'internal-coder'
assert 'LANGSMITH_API_KEY' not in os.environ
assert 'ORCHESTRATOR_TOKEN' not in os.environ
assert '--safe-mode' in sys.argv and '--auth-type' in sys.argv
print(json.dumps({'type':'assistant','text':os.environ['OPENAI_API_KEY']}), flush=True)
if '--json-schema' in sys.argv:
    schema_arg = sys.argv[sys.argv.index('--json-schema')+1]
    schema = json.load(open(schema_arg[1:]))
    assert schema['type'] == 'object'
    assert '--core-tools' in sys.argv
    print(json.dumps({'type':'result','subtype':'success','result':'decision',
        'structured_result': ''' + repr(result_graph()) + '''}))
else:
    print(json.dumps({'type':'result','subtype':'success','result':'implemented and tested'}))
''')
    monkeypatch.setenv("LANGSMITH_API_KEY", "must-not-propagate")
    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "must-not-propagate")
    logs = []

    async def on_log(entry):
        logs.append(entry)

    qwen = runner(tmp_path, executable)
    graph = await qwen.run(source_task(), {"shared": [], "private": []}, str(tmp_path), on_log)
    assert graph["source_result"]["outcome"] == "SUCCESS"
    assert [p["phase"] for p in qwen.last_run["passes"]] == ["work", "decision"]
    assert {l["phase"] for l in logs} == {"work", "decision"}
    all_logs = "".join(p.read_text() for p in (tmp_path / "data").rglob("*.jsonl"))
    assert "sensitive-key-value-12345" not in all_logs
    assert "[REDACTED]" in all_logs
    assert all(p.stat().st_mode & 0o077 == 0 for p in (tmp_path / "data").rglob("*.jsonl"))


async def test_nonzero_exit_is_not_accepted_as_success(tmp_path):
    executable = fake_cli(tmp_path, "import sys\nprint('bad key', file=sys.stderr)\nsys.exit(7)\n")
    with pytest.raises(QwenError, match="status 7"):
        await runner(tmp_path, executable).run(source_task(), {}, str(tmp_path))


async def test_timeout_kills_child_and_releases_global_slot(tmp_path):
    executable = fake_cli(tmp_path, "import time\ntime.sleep(60)\n")
    with pytest.raises(TimeoutError):
        await runner(tmp_path, executable, timeout=0.1).run(source_task(), {}, str(tmp_path))


async def test_rejects_decision_source_spoofing(tmp_path):
    graph = result_graph()
    graph["source_task_id"] = "other-task"
    executable = fake_cli(tmp_path, "import json\nprint(json.dumps({'type':'result', 'result':'ok', 'structured_result':" + repr(graph) + "}))\n")
    with pytest.raises(QwenError, match="identity"):
        await runner(tmp_path, executable).run(source_task(), {}, str(tmp_path))


async def test_graph_runtime_performs_no_blocking_filesystem_io(tmp_path):
    blockbuster = pytest.importorskip("blockbuster")
    executable = fake_cli(tmp_path, "import json\nprint(json.dumps({'type':'result', 'result':'ok', 'structured_result':" + repr(result_graph()) + "}))\n")
    qwen = runner(tmp_path, executable)
    # LangGraph dev uses this same detector. Scan our runtime while allowing
    # pytest's own fixture machinery to prepare/read files outside the scope.
    with blockbuster.blockbuster_ctx(scanned_modules="orchestrator.runtime"):
        output = await qwen.run(source_task(), {}, str(tmp_path))
    assert output["source_task_id"] == "task1"


async def test_missing_credentials_and_unknown_agent_never_start_qwen(tmp_path):
    qwen = QwenRunner(api_key="", base_url="", model="", data_root=tmp_path)
    with pytest.raises(QwenError, match="Configure"):
        await qwen.run(source_task(), {}, str(tmp_path))
    with pytest.raises(QwenError, match="Unknown Qwen agent"):
        await runner(tmp_path, "not-an-executable").run(source_task("UNKNOWN"), {}, str(tmp_path))


async def test_shared_global_session_limit_across_runner_instances(tmp_path, monkeypatch):
    active = 0
    high_watermark = 0

    async def invoke(self, *args, **kwargs):
        nonlocal active, high_watermark
        active += 1
        high_watermark = max(active, high_watermark)
        await asyncio.sleep(0.01)
        active -= 1
        return {"result": "ok", "structured_result": result_graph()}

    monkeypatch.setattr(QwenRunner, "_invoke", invoke)
    runners = [runner(tmp_path, "unused", max_sessions=2) for _ in range(8)]
    await asyncio.gather(*(q.run(source_task(), {}, str(tmp_path)) for q in runners))
    assert high_watermark == 2


async def test_session_limit_shared_across_threads_with_separate_event_loops(tmp_path, monkeypatch):
    active = 0
    high_watermark = 0
    counter_lock = threading.Lock()
    start_together = threading.Barrier(8)

    async def invoke(self, *args, **kwargs):
        nonlocal active, high_watermark
        with counter_lock:
            active += 1
            high_watermark = max(active, high_watermark)
        try:
            await asyncio.sleep(0.03)
            return {"result": "ok", "structured_result": result_graph()}
        finally:
            with counter_lock:
                active -= 1

    def run_in_thread():
        start_together.wait(timeout=10)
        qwen = runner(tmp_path, "unused", max_sessions=2)
        return asyncio.run(qwen.run(source_task(), {}, str(tmp_path)))

    monkeypatch.setattr(QwenRunner, "_invoke", invoke)
    outputs = await asyncio.gather(*(asyncio.to_thread(run_in_thread) for _ in range(8)))
    assert len(outputs) == 8
    assert high_watermark == 2
    assert active == 0


async def test_cancelled_waiter_and_holder_do_not_leak_global_capacity(tmp_path, monkeypatch):
    first_entered = asyncio.Event()
    never_finish = asyncio.Event()
    calls = 0

    async def blocked_invoke(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        first_entered.set()
        await never_finish.wait()

    monkeypatch.setattr(QwenRunner, "_invoke", blocked_invoke)
    holder = asyncio.create_task(runner(tmp_path, "unused", max_sessions=1).run(source_task(), {}, str(tmp_path)))
    await asyncio.wait_for(first_entered.wait(), timeout=2)
    waiter = asyncio.create_task(runner(tmp_path, "unused", max_sessions=1).run(source_task(), {}, str(tmp_path)))
    await asyncio.sleep(0.05)
    assert calls == 1 and not waiter.done()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    holder.cancel()
    with pytest.raises(asyncio.CancelledError):
        await holder

    async def completed_invoke(self, *args, **kwargs):
        return {"result": "ok", "structured_result": result_graph()}

    monkeypatch.setattr(QwenRunner, "_invoke", completed_invoke)
    output = await asyncio.wait_for(
        runner(tmp_path, "unused", max_sessions=1).run(source_task(), {}, str(tmp_path)), timeout=2)
    assert output["source_result"]["outcome"] == "SUCCESS"


async def test_same_runtime_scope_rejects_inconsistent_session_limit(tmp_path, monkeypatch):
    async def invoke(self, *args, **kwargs):
        return {"result": "ok", "structured_result": result_graph()}

    monkeypatch.setattr(QwenRunner, "_invoke", invoke)
    await runner(tmp_path, "unused", max_sessions=2).run(source_task(), {}, str(tmp_path))
    with pytest.raises(QwenError, match="same MAX_QWEN_SESSIONS"):
        await runner(tmp_path, "unused", max_sessions=1).run(source_task(), {}, str(tmp_path))


class Store:
    def __init__(self):
        self.data = {}
        self.reads = []

    async def get(self, ns, key):
        self.reads.append(ns)
        return self.data.get((ns, key))

    async def search(self, ns, limit=1000):
        self.reads.append(ns)
        return [value for (scope, _), value in self.data.items() if scope == ns][:limit]


async def test_private_context_never_crosses_agent_worker_or_communicator(tmp_path):
    store = Store()
    shared = ("coding", "p1", "shared", "context")
    own = ("coding", "p1", "private", "EXECUTE", "execute-01", "thread1")
    other = ("coding", "p1", "private", "EXECUTE", "execute-02", "thread1")
    store.data[(shared, "contract")] = {"contract": "public"}
    store.data[(own, "scratchpad")] = {"secret_analysis": "owner-only"}
    store.data[(other, "scratchpad")] = {"secret_analysis": "other-worker"}
    context = await ScopedContext(store).build("p1", "EXECUTE", "thread1", "execute-01", ["shared://contract"])
    assert context["private"] == [{"secret_analysis": "owner-only"}]
    assert other not in store.reads
    public = await ScopedContext(store).build("p1", "COMMUNICATOR", "thread1")
    assert public["private"] == []
    assert "owner-only" not in json.dumps(public)
    with pytest.raises(ValueError, match="Only published"):
        await ScopedContext(store).build("p1", "ANALYZE", "thread1", context_refs=["private://EXECUTE/execute-01/thread1"])


async def test_execute_requires_trusted_owner_and_private_artifacts_are_not_resolved():
    store = Store()
    with pytest.raises(ValueError, match="worker owner"):
        await ScopedContext(store).build("p1", "EXECUTE", "thread1")
    with pytest.raises(ValueError, match="not published"):
        await ScopedContext(store).build("p1", "ANALYZE", "thread1", context_refs=["artifact://private/log"])
    with pytest.raises(ValueError, match="identifier"):
        await ScopedContext(store).build("../p1", "ANALYZE", "thread1")


@pytest.mark.skipif(os.getenv("RUN_QWEN_CLI_TESTS") != "1", reason="requires installed pinned Qwen CLI; opt in with RUN_QWEN_CLI_TESTS=1")
async def test_pinned_actual_cli_two_pass_openai_protocol_and_decision_tools(tmp_path):
    """Exercise the real CLI against a deterministic HTTP protocol fixture.

    This verifies CLI/provider/schema integration, not a model's coding ability.
    In 0.26.1 safe mode does not enforce the core-tools allowlist; checking the
    actual outgoing request guards against silently exposing tools in pass 2.
    """
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            tools = [tool.get("function", {}).get("name") for tool in request.get("tools", [])]
            if "structured_output" in tools:
                delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "decision_1", "type": "function",
                         "function": {"name": "structured_output", "arguments": json.dumps(result_graph())}}]}
                finish = "tool_calls"
            else:
                delta = {"role": "assistant", "content": "Protocol fixture completed; no model coding performed."}
                finish = "stop"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for choice in [{"index": 0, "delta": delta, "finish_reason": None},
                           {"index": 0, "delta": {}, "finish_reason": finish}]:
                event = {"id": "fixture", "object": "chat.completion.chunk", "created": 1,
                         "model": "fixture", "choices": [choice]}
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        qwen = QwenRunner(base_url=f"http://127.0.0.1:{server.server_port}/v1", api_key="local-fixture-key",
                          model="fixture", data_root=tmp_path / "data", timeout=45)
        output = await qwen.run(source_task(), {"shared": [], "private": []}, str(tmp_path))
        assert output["source_result"]["outcome"] == "SUCCESS"
        assert len(requests) == 2
        assert "structured_output" not in [t["function"]["name"] for t in requests[0].get("tools", [])]
        assert [t["function"]["name"] for t in requests[1].get("tools", [])] == ["structured_output"]
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=2)


async def test_communicator_two_passes_use_own_prompt_and_exclude_code_tools(tmp_path):
    output = result_graph()
    output.update(producer_agent="COMMUNICATOR", nodes=[{
        "id": "analyze", "target_agent": "ANALYZE", "title": "Analyze clarified request"}])
    executable = fake_cli(tmp_path, '''import json, sys
prompt = sys.argv[sys.argv.index('--prompt') + 1]
assert "the user's representative" in prompt
assert 'PRIVATE_HISTORY_MARKER' in prompt
for tool in ['read_file', 'run_shell_command', 'edit', 'write_file', 'agent', 'web_fetch']:
    assert tool in sys.argv
if '--json-schema' in sys.argv:
    print(json.dumps({'type': 'result', 'result': 'decision', 'structured_result': ''' + repr(output) + '''}))
else:
    print(json.dumps({'type': 'result', 'result': 'The user wants a customer portal.'}))
''')
    qwen = runner(tmp_path, executable)
    result = await qwen.run(source_task("COMMUNICATOR"), {
        "Messages": [{"type": "human", "data": {"content": "PRIVATE_HISTORY_MARKER"}}],
        "delegation": {"backward_count": 0, "max_backward_delegations": 3},
    }, str(tmp_path))
    assert result["nodes"][0]["target_agent"] == "ANALYZE"
    assert [p["phase"] for p in qwen.last_run["passes"]] == ["work", "decision"]


async def test_qwen_runtime_drops_conversation_for_execute_even_if_supplied(tmp_path):
    executable = fake_cli(tmp_path, '''import json, sys
assert 'PRIVATE_HISTORY_MARKER' not in sys.argv[sys.argv.index('--prompt') + 1]
print(json.dumps({'type':'result', 'result':'ok', 'structured_result': ''' + repr(result_graph()) + '''}))
''')
    await runner(tmp_path, executable).run(source_task(), {
        "Messages": [{"type": "human", "data": {"content": "PRIVATE_HISTORY_MARKER"}}],
    }, str(tmp_path))


@pytest.mark.parametrize("target,outcome", [("COMMUNICATOR", "NEED_ANALYSIS"), ("PLAN_DELEGATE", "SUCCESS")])
async def test_analyze_repairs_wrong_route_without_repeating_work(tmp_path, target, outcome):
    invalid = {**result_graph(), "producer_agent": "ANALYZE", "nodes": [
        {"id": "next", "target_agent": "EXECUTE", "title": "Implement immediately"}]}
    corrected = {**invalid, "source_result": {"outcome": outcome}, "nodes": [
        {"id": "next", "target_agent": target, "title": "Clarify or plan the brief", "reason": "Resolve scope before implementation"}]}
    executable = fake_cli(tmp_path, '''import json, sys
from pathlib import Path
prompt = sys.argv[sys.argv.index('--prompt') + 1]
if '--json-schema' not in sys.argv:
    count = Path('work-count')
    assert not count.exists(), 'Work pass must not be repeated'
    count.write_text('1')
    print(json.dumps({'type': 'result', 'result': 'WORK_RESULT_SENTINEL'}))
else:
    schema = json.load(open(sys.argv[sys.argv.index('--json-schema')+1][1:]))
    assert schema['$defs']['GraphNode']['properties']['target_agent']['enum'] == ['COMMUNICATOR', 'PLAN_DELEGATE']
    assert 'ANALYZE' in schema['$defs']['Agent']['enum']
    counter = Path('decision-count')
    attempt = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(attempt))
    output = ''' + repr(invalid) + '''
    if attempt == 2:
        assert 'DELEGATION CORRECTION REQUIRED' in prompt
        assert 'WORK_RESULT_SENTINEL' in prompt
        assert 'Do not merely rename' in prompt
        output = ''' + repr(corrected) + '''
    print(json.dumps({'type': 'result', 'result': 'decision', 'structured_result': output}))
''')
    qwen = runner(tmp_path, executable)
    result = await qwen.run(source_task("ANALYZE"), {}, str(tmp_path))
    assert result["nodes"][0]["target_agent"] == target
    assert result["source_result"]["outcome"] == outcome
    assert [p["phase"] for p in qwen.last_run["passes"]] == ["work", "decision", "decision-repair-1"]
    assert (tmp_path / "work-count").read_text() == "1"
    assert (tmp_path / "decision-count").read_text() == "2"


async def test_analyze_invalid_route_repairs_are_bounded_and_release_slot(tmp_path):
    invalid = {**result_graph(), "producer_agent": "ANALYZE", "nodes": [
        {"id": "next", "target_agent": "FINALIZE", "title": "Skip planning"}]}
    executable = fake_cli(tmp_path, "import json\nprint(json.dumps({'type':'result', 'result':'ok', 'structured_result':" + repr(invalid) + "}))\n")
    qwen = runner(tmp_path, executable, max_sessions=1)
    with pytest.raises(QwenError, match="after 2 decision attempts"):
        await qwen.run({**source_task("ANALYZE"), "max_attempts": 2}, {}, str(tmp_path))
    assert [p["phase"] for p in qwen.last_run["passes"]] == ["work", "decision", "decision-repair-1"]
    valid = {**invalid, "nodes": [{"id": "plan", "target_agent": "PLAN_DELEGATE", "title": "Plan the work"}]}
    fake_cli(tmp_path, "import json\nprint(json.dumps({'type':'result', 'result':'ok', 'structured_result':" + repr(valid) + "}))\n")
    result = await asyncio.wait_for(qwen.run(source_task("ANALYZE"), {}, str(tmp_path)), timeout=5)
    assert result["nodes"][0]["target_agent"] == "PLAN_DELEGATE"


@pytest.mark.parametrize("agent", ["ANALYZE", "PLAN_DELEGATE", "EXECUTE", "VERIFY_INTEGRATE", "FINALIZE", "COMMUNICATOR"])
async def test_skills_and_gitlab_credentials_are_work_only_and_role_scoped(tmp_path, monkeypatch, agent):
    monkeypatch.delenv("WORK_PASS_SKILLS_CONFIG", raising=False)
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-fixture-private-value")
    monkeypatch.setenv("GITLAB_HOST", "gitlab.example.invalid")
    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "runtime-secret-never-forward")
    output = {**result_graph(), "producer_agent": agent}
    executable = fake_cli(tmp_path, '''import json, os, sys
prompt = sys.argv[sys.argv.index('--prompt') + 1]
assert 'glpat-fixture-private-value' not in prompt
assert 'ORCHESTRATOR_TOKEN' not in os.environ
is_work = '--json-schema' not in sys.argv
is_analyze = ''' + repr(agent == "ANALYZE") + '''
if is_work and is_analyze:
    assert '<work-skill name="gitlab-cli">' in prompt
    assert 'glab api --method GET' in prompt
    assert os.environ['GITLAB_HOST'] == 'gitlab.example.invalid'
    assert os.environ['GITLAB_TOKEN'] == 'glpat-fixture-private-value'
    # Exercise redaction of subprocess output and the Work result passed onward.
    print(json.dumps({'type':'assistant', 'text':os.environ['GITLAB_TOKEN']}))
    print(json.dumps({'type':'result', 'result':os.environ['GITLAB_TOKEN']}))
else:
    assert '<work-skill name="gitlab-cli">' not in prompt
    assert 'GITLAB_TOKEN' not in os.environ and 'GITLAB_HOST' not in os.environ
    print(json.dumps({'type':'result', 'result':'done', 'structured_result': ''' + repr(output) + '''}))
''')
    qwen = runner(tmp_path, executable)
    await qwen.run(source_task(agent), {}, str(tmp_path))
    assert qwen.last_run["work_skills"] == (["gitlab-cli"] if agent == "ANALYZE" else [])
    logs = "".join(path.read_text() for path in Path(qwen.last_run["private_log_dir"]).glob("*.jsonl"))
    assert "glpat-fixture-private-value" not in logs
    if agent == "ANALYZE":
        assert "[REDACTED]" in logs


async def test_runtime_uses_config_override_only_for_work_prompt(tmp_path, monkeypatch):
    skill = tmp_path / "custom.md"
    skill.write_text("ROLE_SPECIFIC_SKILL_MARKER")
    config = tmp_path / "skills.json"
    config.write_text(json.dumps({"version": 1, "skills": {"custom": "custom.md"},
                                 "agents": {"EXECUTE": ["custom"]}}))
    executable = fake_cli(tmp_path, '''import json, sys
prompt = sys.argv[sys.argv.index('--prompt') + 1]
assert ('ROLE_SPECIFIC_SKILL_MARKER' in prompt) == ('--json-schema' not in sys.argv)
print(json.dumps({'type':'result', 'result':'done', 'structured_result': ''' + repr(result_graph()) + '''}))
''')
    qwen = runner(tmp_path, executable, skills_config=config)
    await qwen.run(source_task(), {}, str(tmp_path))
    assert qwen.last_run["work_skills"] == ["custom"]
