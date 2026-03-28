"""Prompt-caching Anthropic client for autogen evals.

autogen's AnthropicChatCompletionClient doesn't surface cache token counts or
enforce single-tool-call-per-turn. This module provides a subclass that overrides
create() to call the raw Anthropic API directly, injecting cache_control and
disable_parallel_tool_use, then returning a CachedCreateResult with typed cache
token fields.

See function_learning/debug/prompt_caching.md for the full investigation.
"""

import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from anthropic.types import Usage
from autogen_core import CancellationToken
from autogen_core.models import CreateResult, FunctionCall, FunctionExecutionResultMessage, LLMMessage, RequestUsage
from autogen_core.tools import Tool, ToolSchema
from autogen_ext.models.anthropic import AnthropicChatCompletionClient
from autogen_ext.models.anthropic._anthropic_client import (
    convert_tool_choice_anthropic,
    convert_tools,
    normalize_stop_reason,
    to_anthropic_type,
)
from pydantic import BaseModel

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

    cache_read_tokens: int | None
    cache_creation_tokens: int | None


class CachedAnthropicClient(AnthropicChatCompletionClient):
    """AnthropicChatCompletionClient with prompt caching and single-tool-call enforcement.

    Overrides create() to call self._client.messages.create() directly, giving
    clean access to cache token counts without any monkey-patching.

    - Injects top-level cache_control={"type":"ephemeral"} so the API places
      the cache breakpoint at the last cacheable block each turn.
    - Sets disable_parallel_tool_use=True on tool_choice so the model emits
      exactly one tool call per turn.
    - Returns CachedCreateResult with typed cache_read_tokens /
      cache_creation_tokens.

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
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[Tool | ToolSchema] = (),
        tool_choice: Tool | Literal["auto", "required", "none"] = "auto",
        json_output: bool | type[BaseModel] | None = None,
        extra_create_args: Mapping[str, Any] | None = None,
        cancellation_token: CancellationToken | None = None,
    ) -> CachedCreateResult:
        system_message = None
        anthropic_messages: list[Any] = []
        for msg in messages:
            converted = to_anthropic_type(msg)
            if isinstance(converted, str):  # SystemMessage
                system_message = converted
            elif isinstance(converted, list):  # FunctionExecutionResultMessage
                anthropic_messages.extend(converted)
            else:
                anthropic_messages.append(converted)

        has_tool_results = any(isinstance(msg, FunctionExecutionResultMessage) for msg in messages)

        request_args: dict[str, Any] = {
            **self._create_args,
            **(extra_create_args or {}),
            "messages": anthropic_messages,
            "max_tokens": self._create_args.get("max_tokens", 4096),
            "cache_control": {"type": "ephemeral"},
        }
        if system_message is not None:
            request_args["system"] = system_message

        if tools:
            converted_tools = convert_tools(tools)
            self._last_used_tools = converted_tools
            request_args["tools"] = converted_tools
        elif has_tool_results:
            request_args["tools"] = self._last_used_tools

        if tools or has_tool_results:
            tc = convert_tool_choice_anthropic(tool_choice)
            # Disable parallel tool use so the model emits exactly one tool call per turn.
            if isinstance(tc, dict) and tc.get("type") == "any":
                tc = {**tc, "disable_parallel_tool_use": True}
            request_args["tool_choice"] = tc

        response = await self._client.messages.create(**request_args)

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if tool_uses:
            content: str | list[FunctionCall] = [
                FunctionCall(
                    id=b.id,
                    name=b.name,
                    arguments=json.dumps(b.input) if isinstance(b.input, dict) else str(b.input or ""),
                )
                for b in tool_uses
            ]
        else:
            content = "".join(b.text for b in response.content if b.type == "text")

        assert isinstance(response.usage, Usage)
        usage = RequestUsage(prompt_tokens=response.usage.input_tokens, completion_tokens=response.usage.output_tokens)
        return CachedCreateResult(
            finish_reason=normalize_stop_reason(response.stop_reason),
            content=content,
            usage=usage,
            cached=False,
            cache_read_tokens=response.usage.cache_read_input_tokens,
            cache_creation_tokens=response.usage.cache_creation_input_tokens,
        )
