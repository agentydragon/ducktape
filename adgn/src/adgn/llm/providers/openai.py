"""OpenAI provider implementation.

Translates between provider-agnostic types and OpenAI's Responses API.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import openai
from openai import AsyncOpenAI
from openai.types.responses import Response, ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from adgn.llm.types import CompletionRequest, CompletionResult, Message, Tool, ToolCall, Usage


# Retry on OpenAI-specific exceptions
_OPENAI_RETRY_EXCEPTIONS = (
    openai.RateLimitError,
    openai.APIConnectionError,
    openai.InternalServerError,
    openai.APITimeoutError,
    httpx.TimeoutException,
    httpx.ConnectError,
)


@retry(
    stop=stop_after_attempt(10),
    wait=wait_exponential_jitter(initial=0.5, max=60.0),
    retry=retry_if_exception_type(_OPENAI_RETRY_EXCEPTIONS),
)
async def _call_openai_with_retry(client: AsyncOpenAI, **kwargs: Any) -> Response:
    """Call OpenAI Responses API with retry logic for transient errors."""
    return await client.responses.create(**kwargs)


@dataclass
class OpenAIProvider:
    """OpenAI provider implementing LLMProvider protocol.

    Translates between provider-agnostic types and OpenAI's native Responses API format.
    """

    client: AsyncOpenAI
    model: str

    def _convert_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Convert agnostic messages to OpenAI Responses API format.

        OpenAI Responses API accepts various message types in the input array.
        """
        openai_messages: list[dict[str, Any]] = []

        for msg in messages:
            if isinstance(msg.content, str):
                content = [{"type": "input_text", "text": msg.content}]
            else:
                # Convert content parts
                content = []
                for part in msg.content:
                    if hasattr(part, "text"):
                        content.append({"type": "input_text", "text": part.text})

            openai_messages.append({"role": msg.role, "content": content})

        return openai_messages

    def _convert_tools(self, tools: list[Tool] | None) -> list[dict[str, Any]] | None:
        """Convert agnostic tools to OpenAI Responses API format."""
        if not tools:
            return None

        openai_tools = []
        for tool in tools:
            openai_tools.append({
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            })
        return openai_tools

    async def complete(self, req: CompletionRequest) -> CompletionResult:
        """Generate a completion using OpenAI's Responses API."""

        # Convert messages and tools
        messages = self._convert_messages(req.messages)
        tools = self._convert_tools(req.tools)

        # Build request kwargs
        kwargs: dict[str, Any] = {
            "model": self.model,
            "input": messages,
        }

        if tools:
            kwargs["tools"] = tools
        if req.tool_choice:
            kwargs["tool_choice"] = req.tool_choice
        if req.max_tokens:
            kwargs["max_output_tokens"] = req.max_tokens
        if req.temperature is not None:
            kwargs["temperature"] = req.temperature

        # Call API with retries
        response = await _call_openai_with_retry(self.client, **kwargs)

        # Convert response
        content_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        for item in response.output:
            if isinstance(item, ResponseOutputMessage):
                # Extract text from message content
                for content_item in item.content:
                    if isinstance(content_item, ResponseOutputText):
                        content_parts.append(content_item.text)
            elif isinstance(item, ResponseFunctionToolCall):
                # Convert to agnostic tool call
                import json
                tool_calls.append(ToolCall(
                    id=item.call_id,
                    name=item.name,
                    arguments=json.loads(item.arguments) if item.arguments else {},
                ))

        # Build usage info
        usage = None
        if response.usage:
            usage = Usage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                total_tokens=response.usage.total_tokens,
            )

        return CompletionResult(
            id=response.id,
            content="\n".join(content_parts) if content_parts else None,
            tool_calls=tool_calls if tool_calls else None,
            usage=usage,
        )
