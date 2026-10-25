#!/usr/bin/env python3
"""Deterministic CLI contract fixture. Never imported by the production runtime.

This exercises real subprocesses, Agent Server runs, workers, Store requests and
Git integration without contacting an LLM. It deliberately accepts Qwen CLI
arguments and emits the pinned CLI's stream-json terminal event shape.
"""

import json
from pathlib import Path
import sys
import time


def emit(value):
    print(json.dumps(value), flush=True)


def main():
    prompt = sys.argv[sys.argv.index("--prompt") + 1]
    payload = next(json.loads(line) for line in prompt.splitlines() if line.startswith('{"task":'))
    task, context = payload["task"], payload["context"]
    agent = task["agent"]
    if "--json-schema" not in sys.argv:
        emit({"type": "assistant", "fixture_phase": "start", "at": time.time(), "message": "fixture work"})
        if agent == "EXECUTE":
            name = "api" if task["title"] == "Implement API" else "ui"
            # The intervals recorded in private logs prove these work passes
            # overlapped; an open port or two allocated tasks is insufficient.
            # Agent Server's background worker polling can stagger starts by a
            # second. Leave enough work to observe overlap despite that cadence.
            time.sleep(3)
            Path(f"{name}.txt").write_text(f"{name} implemented\n")
        elif agent in {"VERIFY_INTEGRATE", "FINALIZE"}:
            assert Path("api.txt").read_text() == "api implemented\n"
            assert Path("ui.txt").read_text() == "ui implemented\n"
        emit({"type": "result", "subtype": "success", "is_error": False,
              "session_id": f"fixture-{agent}-work", "result": "Fixture work passed",
              "fixture_phase": "end", "at": time.time()})
        return
    output = {
        "schema_version": "1.0", "project_id": task["project_id"],
        "source_task_id": task["task_id"], "producer_agent": agent,
        "execution_chain_id": task["execution_chain_id"],
        "summary": "Deterministic fixture acceptance passed",
        "source_result": {"outcome": "SUCCESS", "proposed_status": "DONE"},
        "nodes": [], "edges": [],
    }
    if agent == "ANALYZE":
        output["nodes"] = [{"id": "plan", "target_agent": "PLAN_DELEGATE", "title": "Plan fixture feature"}]
    elif agent == "PLAN_DELEGATE":
        output["nodes"] = [
            {"id": "api", "target_agent": "EXECUTE", "title": "Implement API", "acceptance_criteria": ["api.txt contains implementation"]},
            {"id": "ui", "target_agent": "EXECUTE", "title": "Implement UI", "acceptance_criteria": ["ui.txt contains implementation"]},
            {"id": "verify", "target_agent": "VERIFY_INTEGRATE", "title": "Integrate and verify both changes"},
            {"id": "finalize", "target_agent": "FINALIZE", "title": "Accept complete fixture feature"},
        ]
        output["edges"] = [
            {"from": "api", "to": "verify", "type": "DEPENDS_ON", "condition": "SUCCESS"},
            {"from": "ui", "to": "verify", "type": "DEPENDS_ON", "condition": "SUCCESS"},
            {"from": "verify", "to": "finalize", "type": "DEPENDS_ON", "condition": "SUCCESS"},
        ]
    elif agent == "FINALIZE":
        output["source_result"].update(
            proposed_status="CLOSED", close_task_ids=[t["task_id"] for t in context["chain_tasks"]]
        )
        output["notification"] = {"message": "Fixture feature verified and finalized"}
    emit({"type": "result", "subtype": "success", "is_error": False,
          "session_id": f"fixture-{agent}-decision", "result": "Graph decision",
          "structured_result": output})


if __name__ == "__main__":
    main()
