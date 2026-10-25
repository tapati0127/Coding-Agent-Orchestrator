"""Run from repository root: python -m examples.run_execute 'Task'."""

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

from orchestrator.agents.execute import build_execute_agent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("instructions")
    args = parser.parse_args()
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    result = build_execute_agent().invoke({
        "task": {
            "task_id": "execute-001",
            "instructions": args.instructions,
            "acceptance_criteria": ["Hoàn thành yêu cầu và báo cáo kiểm tra đã chạy"],
            "constraints": ["Không commit hoặc push"],
        },
    })
    print(result["output"] or result["error"])
    print(json.dumps(result["assessment"], ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] == "completed" else 1)


if __name__ == "__main__":
    main()
