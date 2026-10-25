"""Logical context scoping. This adapter is not an OS/filesystem security boundary."""

from __future__ import annotations

import re
from typing import Any

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_AGENTS = {"COMMUNICATOR", "ANALYZE", "PLAN_DELEGATE", "EXECUTE", "VERIFY_INTEGRATE", "FINALIZE"}


def identifier(value: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value) or value in {".", ".."}:
        raise ValueError("Invalid context owner identifier")
    return value


class ScopedContext:
    """Only resolve shared data and the trusted caller's own private namespace.

    Ownership arguments must come from a claimed task/worker, never model output.
    Do not expose this object's underlying store or the Store SDK to Qwen tools.
    """

    def __init__(self, store: Any):
        self._store = store

    async def build(
        self,
        project_id: str,
        agent: str,
        thread_id: str,
        worker_id: str | None = None,
        context_refs: list[str] | tuple[str, ...] = (),
    ) -> dict:
        project_id, thread_id = identifier(project_id), identifier(thread_id)
        agent = str(agent).upper()
        if agent not in _AGENTS:
            raise ValueError("Unknown context owner agent")
        shared_ns = ("coding", project_id, "shared", "context")
        refs = {}
        for ref in context_refs:
            if ref.startswith("shared://"):
                key = ref.removeprefix("shared://")
                if key.startswith("context/"):
                    key = key.removeprefix("context/")
                # Shared keys can contain logical paths, but never escape a namespace.
                if not key or any(part in {"", ".", ".."} for part in key.split("/")):
                    raise ValueError("Invalid shared context reference")
                value = await self._store.get(shared_ns, key)
                if value is None:
                    raise ValueError(f"Missing shared reference: {ref}")
                refs[ref] = value
            elif ref.startswith("artifact://"):
                key = ref.removeprefix("artifact://")
                value = await self._store.get(("coding", project_id, "shared", "artifacts"), key)
                if value is None:
                    raise ValueError(f"Artifact is not published in shared metadata: {ref}")
                refs[ref] = value
            else:
                raise ValueError("Only published shared/artifact references are allowed")
        private: list[dict] = []
        if agent != "COMMUNICATOR":
            ns = ("coding", project_id, "private", agent)
            if agent == "EXECUTE":
                if worker_id is None:
                    raise ValueError("Execute context requires a trusted worker owner")
                ns += (identifier(worker_id),)
            ns += (thread_id,)
            private = await self._store.search(ns, limit=1000)
        return {
            "shared": await self._store.search(shared_ns, limit=1000),
            "private": private,
            "references": refs,
        }
