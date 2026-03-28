"""Prompt-caching Anthropic client for autogen evals.

autogen's AnthropicChatCompletionClient doesn't forward cache_control to the
Anthropic API. This module provides a subclass that injects a top-level
cache_control marker and surfaces cache token counts through RequestUsage.

See function_learning/debug/prompt_caching.md for the full investigation.
"""

import logging
from collections.abc import Sequence
from typing import Any

from autogen_core import CancellationToken
from autogen_core.models import CreateResult
from autogen_core.tools import Tool, ToolSchema
from autogen_ext.models.anthropic import AnthropicChatCompletionClient

logger = logging.getLogger(__name__)

_ANTHROPIC_MODEL_INFO: dict[str, Any] = {
    "vision": True,
    "function_calling": True,
    "json_output": True,
    "family": "unknown",
    "structured_output": True,
    "multiple_system_messages": False,
}


class CachedAnthropicClient(AnthropicChatCompletionClient):
    """AnthropicChatCompletionClient with prompt caching enabled.

    Injects top-level cache_control={"type":"ephemeral"} on every request so
    the Anthropic API automatically places the cache breakpoint at the last
    cacheable block and moves it forward as the conversation grows.

    Also captures cache_creation_input_tokens and cache_read_input_tokens from
    the raw Anthropic response and attaches them to the returned RequestUsage
    as dynamic attributes (cache_creation_tokens, cache_read_tokens), since
    autogen's RequestUsage dataclass only has prompt_tokens/completion_tokens.

    Caching behavior confirmed via live API testing (2026-03-28):
    - Anthropic does NOT auto-cache without cache_control (unlike OpenAI)
    - Top-level cache_control works: first call creates cache, subsequent calls read it
    - Per-block cache_control (old approach) creates a new cache entry every call
    - Haiku 4.5 minimum cacheable prefix: 4096 tokens
    """

    def __init__(self, *, model: str, **kwargs: Any) -> None:
        kwargs.setdefault("model_info", _ANTHROPIC_MODEL_INFO)
        super().__init__(model=model, **kwargs)
        self._last_cache_read: int = 0
        self._last_cache_creation: int = 0
        self._wrap_messages_create()

    def _wrap_messages_create(self) -> None:
        raw_client = getattr(self, "_client", None)
        if raw_client is None:
            return
        original_create = raw_client.messages.create

        async def cached_create(*, model: str, messages: list[dict], max_tokens: int, **rest: Any) -> object:
            rest["cache_control"] = {"type": "ephemeral"}
            response = await original_create(model=model, messages=messages, max_tokens=max_tokens, **rest)
            # Capture cache tokens from the raw Anthropic Message.usage.
            self._last_cache_read = getattr(response.usage, "cache_read_input_tokens", 0) or 0
            self._last_cache_creation = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
            return response

        raw_client.messages.create = cached_create

    async def create(
        self,
        messages: Any,
        *,
        tools: Sequence[Tool | ToolSchema] = (),
        tool_choice: Any = "auto",
        json_output: Any = None,
        extra_create_args: Any = None,
        cancellation_token: CancellationToken | None = None,
    ) -> CreateResult:
        result = await super().create(
            messages,
            tools=tools,
            tool_choice=tool_choice,
            json_output=json_output,
            extra_create_args=extra_create_args or {},
            cancellation_token=cancellation_token,
        )
        # Attach cache counts as dynamic attrs on the RequestUsage dataclass so
        # callers can use getattr(result.usage, "cache_read_tokens", 0).
        result.usage.cache_read_tokens = self._last_cache_read  # type: ignore[attr-defined]
        result.usage.cache_creation_tokens = self._last_cache_creation  # type: ignore[attr-defined]
        return result
