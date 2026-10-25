"""python -m examples.run_plan 'Request' [--plan-only]"""

import argparse
import asyncio
import json
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage

from graph import build_graph


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("request")
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    result = await build_graph(dispatch_execute=not args.plan_only).ainvoke({
        "messages": [HumanMessage(content=args.request)],
    })
    print(result["messages"][-1].content)
    print(json.dumps({key: result.get(key) for key in
                      ("status", "clarification", "natural_plan", "plan", "results", "error", "replan_count", "execution_history", "approved_work")}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] in {"planned", "completed", "needs_clarification"} else 1)


if __name__ == "__main__":
    asyncio.run(main())
