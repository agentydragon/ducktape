"""Anthropic provider implementing LLMProvider protocol.

This provider translates between the common request/result format (based on OpenAI's
Responses API) and Anthropic's Messages API, allowing Anthropic models to be used
seamlessly with the existing agent infrastructure.

Key translations:
- SystemMessage -> system parameter (not in messages array)
- FunctionCallItem/FunctionCallOutputItem <-> tool_use/tool_result blocks
- ReasoningItem -> skipped (Anthropic doesn't support reasoning blocks)
- Usage details -> placeholders for cached_tokens and reasoning_tokens (not provided by Anthropic)
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from anthropic import AsyncAnthropic
from anthropic.types import MessageParam, TextBlock as AnthropicTextBlock, ToolUseBlock as AnthropicToolUseBlock

from adgn.openai_utils.model import (
    AssistantMessage,
    AssistantMessageOut,
    FunctionCallItem,
    FunctionCallOutputItem,
    FunctionToolParam,
    InputItem,
    InputTextPart,
    OutputText,
    ReasoningItem,
    ResponsesRequest,
    ResponsesResult,
    SystemMessage,
    UserMessage,
)
from openai.types.responses.response_usage import (
    InputTokensDetails,
    OutputTokensDetails,
    ResponseUsage,
)


@dataclass
class AnthropicAdapter:
    """Anthropic provider implementing LLMProvider protocol.

    Translates between the common request/result format and Anthropic's native Messages API.
    """

    client: AsyncAnthropic
    model: str

    def _convert_messages_to_anthropic(
        self, items: list[InputItem], instructions: str | None
    ) -> tuple[str | None, list[MessageParam]]:
        """Convert Responses API input items to Anthropic messages format.

        Returns (system_prompt, messages).
        """
        system_prompt: str | None = None
        messages: list[MessageParam] = []

        for item in items:
            if isinstance(item, SystemMessage):
                # Anthropic uses a separate system parameter
                texts = [p.text for p in item.content if isinstance(p, InputTextPart)]
                if system_prompt:
                    system_prompt += "\n\n" + "\n".join(texts)
                else:
                    system_prompt = "\n".join(texts)
            elif isinstance(item, UserMessage):
                texts = [p.text for p in item.content if isinstance(p, InputTextPart)]
                messages.append({"role": "user", "content": "\n".join(texts)})
            elif isinstance(item, AssistantMessage):
                if item.content:
                    texts = [p.text for p in item.content if isinstance(p, InputTextPart)]
                    if texts:
                        messages.append({"role": "assistant", "content": "\n".join(texts)})
            elif isinstance(item, FunctionCallItem):
                # Convert function call to tool use
                messages.append({
                    "role": "assistant",
                    "content": [{
                        "type": "tool_use",
                        "id": item.call_id,
                        "name": item.name,
                        "input": json.loads(item.arguments) if item.arguments else {}
                    }]
                })
            elif isinstance(item, FunctionCallOutputItem):
                # Convert function output to tool result
                messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": item.call_id,
                        "content": item.output or ""
                    }]
                })
            elif isinstance(item, ReasoningItem):
                # Anthropic doesn't have reasoning blocks - skip them
                pass

        # If instructions were provided, add them to system prompt
        if instructions:
            if system_prompt:
                system_prompt = f"{instructions}\n\n{system_prompt}"
            else:
                system_prompt = instructions

        return system_prompt, messages

    def _convert_tools_to_anthropic(self, tools: list[FunctionToolParam] | None) -> list[dict[str, Any]] | None:
        """Convert Responses API tools to Anthropic tools format."""
        if not tools:
            return None

        anthropic_tools = []
        for tool in tools:
            anthropic_tools.append({
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": tool.parameters or {"type": "object", "properties": {}}
            })
        return anthropic_tools

    async def responses_create(self, req: ResponsesRequest) -> ResponsesResult:
        """Create a completion using Anthropic API, translating to/from Responses format."""

        # Convert input
        if isinstance(req.input, str):
            messages = [{"role": "user", "content": req.input}]
            system_prompt = req.instructions
        else:
            system_prompt, messages = self._convert_messages_to_anthropic(req.input, req.instructions)

        # Convert tools
        tools = self._convert_tools_to_anthropic(req.tools)

        # Call Anthropic API
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": req.max_output_tokens or 4096,  # Anthropic requires max_tokens
        }

        if system_prompt:
            kwargs["system"] = system_prompt
        if tools:
            kwargs["tools"] = tools

        response = await self.client.messages.create(**kwargs)

        # Convert response back to Responses format
        output_items: list[AssistantMessageOut | FunctionCallItem] = []

        for block in response.content:
            if isinstance(block, AnthropicTextBlock):
                output_items.append(AssistantMessageOut(parts=[OutputText(text=block.text)]))
            elif isinstance(block, AnthropicToolUseBlock):
                output_items.append(FunctionCallItem(
                    name=block.name,
                    arguments=json.dumps(block.input),
                    call_id=block.id,
                    id=block.id
                ))

        # Create usage info
        # Anthropic doesn't provide cached_tokens or reasoning_tokens, so we use 0
        usage = ResponseUsage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            total_tokens=response.usage.input_tokens + response.usage.output_tokens,
            input_tokens_details=InputTokensDetails(cached_tokens=0),
            output_tokens_details=OutputTokensDetails(reasoning_tokens=0)
        )

        return ResponsesResult(
            id=response.id,
            usage=usage,
            output=output_items
        )
