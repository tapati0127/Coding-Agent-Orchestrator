"""Validated runtime settings; credentials never appear in repr()."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class Settings:
    langgraph_url: str = "http://127.0.0.1:2024"
    qwen_base_url: str = "http://llm.internal/v1"
    qwen_api_key: str = field(default="", repr=False)
    qwen_model: str = "qwen-coder"
    decision_base_url: str = ""
    decision_api_key: str = field(default="", repr=False)
    decision_model: str = ""
    decision_timeout_seconds: int = 120
    orchestrator_token: str = field(default="", repr=False)
    dashboard_token: str = field(default="", repr=False)
    max_execute_workers: int = 5
    max_qwen_sessions: int = 5
    max_backward_delegations: int = 3
    max_technical_retries: int = 3
    dispatch_poll_seconds: float = 2.0
    task_lease_seconds: int = 600
    human_response_timeout_seconds: int = 1800
    workspace_root: Path = Path("/workspace")
    data_root: Path = Path("/data")
    qwen_timeout_seconds: int = 1800

    def __post_init__(self) -> None:
        for name, upper in (("max_execute_workers", 5), ("max_qwen_sessions", 5),
                            ("max_backward_delegations", 3), ("max_technical_retries", 3)):
            value = getattr(self, name)
            minimum = 0 if name == "max_backward_delegations" else 1
            if not minimum <= value <= upper:
                raise ValueError(f"{name} must be between {minimum} and {upper}")
        if (self.dispatch_poll_seconds <= 0 or self.task_lease_seconds <= 0
                or self.qwen_timeout_seconds <= 0 or self.human_response_timeout_seconds <= 0
                or self.decision_timeout_seconds <= 0):
            raise ValueError("Polling, lease and timeout durations must be positive")
        if self.orchestrator_token and self.orchestrator_token == self.dashboard_token:
            raise ValueError("ORCHESTRATOR_TOKEN and DASHBOARD_TOKEN must be different")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        source = os.environ if environ is None else environ
        kwargs = {}
        casts = {
            "max_execute_workers": int, "max_qwen_sessions": int,
            "max_backward_delegations": int, "max_technical_retries": int,
            "dispatch_poll_seconds": float, "task_lease_seconds": int,
            "qwen_timeout_seconds": int, "workspace_root": Path, "data_root": Path,
            "human_response_timeout_seconds": int,
            "decision_timeout_seconds": int,
        }
        for name in cls.__dataclass_fields__:
            key = name.upper()
            if key in source:
                kwargs[name] = casts.get(name, str)(source[key])
        return cls(**kwargs)
