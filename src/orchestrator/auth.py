"""Trusted runtime authentication for built-in Agent Server APIs."""
from __future__ import annotations

import hmac
import os
from typing import Any

from langgraph_sdk import Auth


auth = Auth()


def _header(headers: dict[Any, Any] | None, name: str) -> str:
    for key, value in (headers or {}).items():
        decoded_key = key.decode() if isinstance(key, bytes) else str(key)
        if decoded_key.lower() == name:
            return value.decode() if isinstance(value, bytes) else str(value)
    return ""


@auth.authenticate
async def authenticate(
    headers: dict[bytes, bytes] | None = None,
    authorization: str | None = None,
) -> dict:
    expected = os.environ.get("ORCHESTRATOR_TOKEN", "")
    # Misconfiguration must not silently expose the Store or run endpoints.
    if not expected:
        raise Auth.exceptions.HTTPException(status_code=503, detail="Runtime authentication is not configured")
    candidate = _header(headers, "x-api-key")
    bearer = authorization or _header(headers, "authorization")
    if not candidate and bearer.lower().startswith("bearer "):
        candidate = bearer[7:]
    if not candidate or not hmac.compare_digest(expected, candidate):
        raise Auth.exceptions.HTTPException(status_code=401, detail="Invalid runtime credentials")
    return {"identity": "orchestrator-runtime", "permissions": ["runtime"]}


@auth.on
async def authorize(ctx: Auth.types.AuthContext, value: dict) -> bool:
    return ctx.user.identity == "orchestrator-runtime"
