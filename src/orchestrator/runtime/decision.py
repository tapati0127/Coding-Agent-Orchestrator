"""Async ChatOpenAI conversations and JSON Schema decisions for internal LLMs."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
from typing import Any, AsyncIterator
from urllib.parse import urlsplit

import httpx
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, messages_from_dict, messages_to_dict
from langchain_openai import ChatOpenAI


_SECRET_KEYS = ("api_key", "apikey", "token", "password", "credential", "secret")


def safe_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): safe_payload(item) for key, item in value.items()
                if not any(word in str(key).lower() for word in _SECRET_KEYS)}
    if isinstance(value, (list, tuple)):
        return [safe_payload(item) for item in value]
    return value


class DecisionRunner:
    def __init__(
        self,
        *,
        base_url: str = "",
        api_key: str = "",
        model: str = "",
        timeout: float | None = None,
    ):
        self.base_url = os.getenv("DECISION_BASE_URL") or base_url or os.getenv("QWEN_BASE_URL", "")
        self.api_key = os.getenv("DECISION_API_KEY") or api_key or os.getenv("QWEN_API_KEY", "")
        self.model = os.getenv("DECISION_MODEL") or model or os.getenv("QWEN_MODEL", "")
        self.timeout = float(timeout if timeout is not None else os.getenv("DECISION_TIMEOUT_SECONDS", "120"))
        if self.timeout <= 0:
            raise ValueError("Decision timeout must be positive")

    def validate_config(self) -> None:
        if not all((self.base_url, self.api_key, self.model)):
            raise ValueError("Configure DECISION_BASE_URL, DECISION_API_KEY and DECISION_MODEL or their QWEN_* fallbacks")
        url = urlsplit(self.base_url)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            raise ValueError("DECISION_BASE_URL must be an HTTP(S) endpoint without embedded credentials")

    def _redact(self, value: Any) -> Any:
        if isinstance(value, str):
            for secret in (self.api_key, *(os.getenv(key, "") for key in (
                "QWEN_API_KEY", "DECISION_API_KEY", "ORCHESTRATOR_TOKEN", "GITLAB_TOKEN",
            ))):
                if secret:
                    value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, dict):
            return {key: self._redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        return value

    def _input(self, prompt: str | list[BaseMessage]) -> str | list[BaseMessage]:
        if isinstance(prompt, str):
            return self._redact(prompt)
        messages = messages_from_dict(self._redact(messages_to_dict(prompt)))
        # Some chat templates require exactly one leading system message. Pass
        # instructions and persisted timeout notices can arrive after the chat.
        # Preserve their system role without changing the order of human/AI data.
        systems = [message for message in messages if isinstance(message, SystemMessage)]
        if not systems:
            return messages
        if all(isinstance(message.content, str) for message in systems):
            content = "\n\n".join(message.content for message in systems)
        else:
            content = [block for message in systems for block in (
                [{"type": "text", "text": message.content}] if isinstance(message.content, str)
                else message.content)]
        return [systems[0].model_copy(update={"content": content}),
                *(message for message in messages if not isinstance(message, SystemMessage))]

    @asynccontextmanager
    async def _llm(self, *, stream: bool = False) -> AsyncIterator[ChatOpenAI]:
        self.validate_config()
        # SSL setup and SDK initialization can read files. Keep them off the
        # graph event loop, and own both HTTP clients for this invocation.
        async_client = await asyncio.to_thread(httpx.AsyncClient, timeout=self.timeout)
        async with async_client:
            sync_client = await asyncio.to_thread(httpx.Client, timeout=self.timeout)
            try:
                llm = await asyncio.to_thread(
                    ChatOpenAI,
                    base_url=self.base_url,
                    api_key=self.api_key,
                    model=self.model,
                    timeout=self.timeout,
                    max_retries=0,
                    streaming=stream,
                    disable_streaming=not stream,
                    use_responses_api=False,
                    extra_body={"enable_thinking": False, "chat_template_kwargs": {"enable_thinking": False}},
                    http_client=sync_client,
                    http_async_client=async_client,
                )
                yield llm
            finally:
                await asyncio.to_thread(sync_client.close)

    async def chat(self, messages: list[BaseMessage], *, stream: bool = False) -> AIMessage:
        """Read conversation messages directly, without a CLI work pass."""
        async with self._llm(stream=stream) as llm:
            output = await llm.ainvoke(self._input(messages), config={"tags": [] if stream else ["nostream"]})
            return output.model_copy(update={"content": self._redact(output.content)})

    async def ainvoke(self, prompt: str | list[BaseMessage], schema: dict) -> dict:
        async with self._llm() as llm:
            decision = llm.with_structured_output(schema, method="json_schema", strict=False)
            output = await decision.ainvoke(self._input(prompt), config={"tags": ["nostream"]})
            if not isinstance(output, dict):
                raise ValueError("Decision response must be a JSON object")
            return self._redact(output)
