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
    assert agent != "COMMUNICATOR", "Communicator must use ChatOpenAI directly"
    if agent in {"ANALYZE", "FINALIZE"}:
        assert context["Messages"][0]["type"] == "human"
        assert any(message["type"] == "human" and "Build independent API and UI" in message["data"]["content"]
                   for message in context["Messages"])
    else:
        assert "Messages" not in context
    assert "--json-schema" not in sys.argv, "Decision must use LangChain, not CLI"
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


if __name__ == "__main__":
    main()
