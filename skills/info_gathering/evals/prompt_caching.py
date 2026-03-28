"""Prompt-caching Anthropic client for autogen evals.

autogen's AnthropicChatCompletionClient doesn't forward cache_control to the
Anthropic API. This module provides a subclass that injects a top-level
cache_control marker, disables parallel tool use (so each turn has exactly one
tool call), and surfaces cache token counts in a typed CreateResult subclass.

See function_learning/debug/prompt_caching.md for the full investigation.
"""

import logging
from collections.abc import Sequence
from typing import Any

from anthropic.types import Usage
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


class CachedCreateResult(CreateResult):
    """CreateResult extended with Anthropic prompt-caching token counts."""

    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


class CachedAnthropicClient(AnthropicChatCompletionClient):
    """AnthropicChatCompletionClient with prompt caching and single-tool-call enforcement.

    - Injects top-level cache_control={"type":"ephemeral"} so the API places
      the cache breakpoint at the last cacheable block each turn.
    - Sets disable_parallel_tool_use=True on tool_choice so the model emits
      exactly one tool call per turn.
    - Returns CachedCreateResult with typed cache_read_tokens /
      cache_creation_tokens instead of attaching dynamic attributes.

    Caching behavior confirmed via live API testing (2026-03-28):
    - Anthropic does NOT auto-cache without cache_control (unlike OpenAI)
    - Top-level cache_control works: first call creates cache, subsequent calls read it
    - Per-block cache_control (old approach) creates a new cache entry every call
    - Haiku 4.5 minimum cacheable prefix: 4096 tokens
    """

    def __init__(self, *, model: str, **kwargs: Any) -> None:
        kwargs.setdefault("model_info", _ANTHROPIC_MODEL_INFO)
        super().__init__(model=model, **kwargs)

    async def create(
        self,
        messages: Any,
        *,
        tools: Sequence[Tool | ToolSchema] = (),
        tool_choice: Any = "auto",
        json_output: Any = None,
        extra_create_args: Any = None,
        cancellation_token: CancellationToken | None = None,
    ) -> CachedCreateResult:
        raw_client = getattr(self, "_client", None)
        original_create = raw_client.messages.create if raw_client is not None else None
        captured: tuple[int, int] = (0, 0)

        if original_create is not None:

            async def intercepted(*, model: str, messages: list[dict], max_tokens: int, **rest: Any) -> object:
                nonlocal captured
                rest["cache_control"] = {"type": "ephemeral"}
                # Disable parallel tool use so the model emits exactly one tool call.
                tc = rest.get("tool_choice")
                if isinstance(tc, dict) and tc.get("type") == "any":
                    rest["tool_choice"] = {**tc, "disable_parallel_tool_use": True}
                response = await original_create(model=model, messages=messages, max_tokens=max_tokens, **rest)
                assert isinstance(response.usage, Usage)
                captured = (
                    response.usage.cache_read_input_tokens or 0,
                    response.usage.cache_creation_input_tokens or 0,
                )
                return response

            raw_client.messages.create = intercepted

        try:
            result = await super().create(
                messages,
                tools=tools,
                tool_choice=tool_choice,
                json_output=json_output,
                extra_create_args=extra_create_args or {},
                cancellation_token=cancellation_token,
            )
        finally:
            if original_create is not None:
                raw_client.messages.create = original_create

        cache_read, cache_creation = captured
        return CachedCreateResult(
            **result.model_dump(), cache_read_tokens=cache_read, cache_creation_tokens=cache_creation
        )
