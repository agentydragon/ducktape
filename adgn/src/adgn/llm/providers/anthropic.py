"""Anthropic provider implementation.

Translates between provider-agnostic types and Anthropic's Messages API.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

import anthropic
from anthropic import AsyncAnthropic
from anthropic.types import MessageParam, TextBlock as AnthropicTextBlock, ToolUseBlock as AnthropicToolUseBlock
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from adgn.llm.types import CompletionRequest, CompletionResult, Message, Tool, ToolCall, Usage


# Retry on Anthropic-specific exceptions
_ANTHROPIC_RETRY_EXCEPTIONS = (
    anthropic.RateLimitError,
    anthropic.APIConnectionError,
    anthropic.InternalServerError,
    anthropic.APITimeoutError,
)


@retry(
    stop=stop_after_attempt(10),
    wait=wait_exponential_jitter(initial=0.5, max=60.0),
    retry=retry_if_exception_type(_ANTHROPIC_RETRY_EXCEPTIONS),
)
async def _call_anthropic_with_retry(client: AsyncAnthropic, **kwargs: Any) -> anthropic.types.Message:
    """Call Anthropic API with retry logic for transient errors."""
    return await client.messages.create(**kwargs)


@dataclass
class AnthropicProvider:
    """Anthropic provider implementing LLMProvider protocol.

    Translates between provider-agnostic types and Anthropic's native Messages API format.
    """

    client: AsyncAnthropic
    model: str

    def _convert_messages(self, messages: list[Message]) -> tuple[str | None, list[MessageParam]]:
        """Convert agnostic messages to Anthropic format.

        Returns (system_prompt, messages).
        Anthropic uses a separate system parameter instead of system messages in the array.
        """
        system_prompt: str | None = None
        anthropic_messages: list[MessageParam] = []

        for msg in messages:
            if msg.role == "system":
                # Extract text content
                if isinstance(msg.content, str):
                    text = msg.content
                else:
                    # Concatenate all text parts
                    text = "\n".join(part.text for part in msg.content if hasattr(part, "text"))

                # Append to system prompt
                if system_prompt:
                    system_prompt += "\n\n" + text
                else:
                    system_prompt = text
            else:
                # User or assistant message
                if isinstance(msg.content, str):
                    content = msg.content
                else:
                    # Concatenate text parts for now
                    content = "\n".join(part.text for part in msg.content if hasattr(part, "text"))

                anthropic_messages.append({"role": msg.role, "content": content})

        return system_prompt, anthropic_messages

    def _convert_tools(self, tools: list[Tool] | None) -> list[dict[str, Any]] | None:
        """Convert agnostic tools to Anthropic format."""
        if not tools:
            return None

        anthropic_tools = []
        for tool in tools:
            anthropic_tools.append({
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.parameters,
            })
        return anthropic_tools

    async def complete(self, req: CompletionRequest) -> CompletionResult:
        """Generate a completion using Anthropic's Messages API."""

        # Convert messages
        system_prompt, messages = self._convert_messages(req.messages)

        # Convert tools
        tools = self._convert_tools(req.tools)

        # Build request kwargs
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": req.max_tokens or 4096,  # Anthropic requires max_tokens
        }

        if system_prompt:
            kwargs["system"] = system_prompt
        if tools:
            kwargs["tools"] = tools
        if req.temperature is not None:
            kwargs["temperature"] = req.temperature

        # Call API with retries
        response = await _call_anthropic_with_retry(self.client, **kwargs)

        # Convert response
        content_text: list[str] = []
        tool_calls: list[ToolCall] = []

        for block in response.content:
            if isinstance(block, AnthropicTextBlock):
                content_text.append(block.text)
            elif isinstance(block, AnthropicToolUseBlock):
                tool_calls.append(ToolCall(
                    id=block.id,
                    name=block.name,
                    arguments=block.input,  # Already dict
                ))

        return CompletionResult(
            id=response.id,
            content="\n".join(content_text) if content_text else None,
            tool_calls=tool_calls if tool_calls else None,
            usage=Usage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                total_tokens=response.usage.input_tokens + response.usage.output_tokens,
            ),
        )
