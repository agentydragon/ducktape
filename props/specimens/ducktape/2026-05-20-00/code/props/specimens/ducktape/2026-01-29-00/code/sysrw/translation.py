"""Translation between Anthropic and OpenAI message formats."""

from __future__ import annotations

import json

from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessageParam,
    ChatCompletionMessageToolCallParam,
    ChatCompletionSystemMessageParam,
    ChatCompletionToolMessageParam,
    ChatCompletionUserMessageParam,
)

from sysrw.anthropic.text_extraction import extract_text_content
from sysrw.anthropic.types import (
    ContentBlock as AnthropicContentBlock,
    Message as AnthropicMessage,
    MessageRole as AnthropicMessageRole,
    TextBlock as AnthropicTextBlock,
    ToolResultBlock as AnthropicToolResultBlock,
    ToolUseBlock as AnthropicToolUseBlock,
)


def _join_texts(texts: list[str]) -> str:
    """Join text parts with newlines, filtering empty strings."""
    return "\n".join(t for t in texts if t)


def _extract_text_from_content(content: str | list[AnthropicContentBlock]) -> str:
    """Extract text from Anthropic message content."""
    if isinstance(content, str):
        return content
    texts = [block.text for block in content if isinstance(block, AnthropicTextBlock)]
    return _join_texts(texts)


def _handle_assistant_blocks(
    blocks: list[AnthropicContentBlock],
) -> tuple[str | None, list[ChatCompletionMessageToolCallParam]]:
    """Process assistant message blocks, extracting text and tool calls."""
    texts: list[str] = []
    tool_calls: list[ChatCompletionMessageToolCallParam] = []

    for block in blocks:
        if isinstance(block, AnthropicTextBlock):
            texts.append(block.text)
        elif isinstance(block, AnthropicToolUseBlock):
            tool_calls.append(
                ChatCompletionMessageToolCallParam(
                    type="function",
                    function={
                        "name": block.name,
                        "arguments": json.dumps(block.input, ensure_ascii=False, separators=(",", ":")),
                    },
                    id=block.id,
                )
            )

    content = _join_texts(texts) if texts else None
    return content, tool_calls


def _handle_user_blocks(blocks: list[AnthropicContentBlock]) -> tuple[list[str], list[ChatCompletionToolMessageParam]]:
    """Process user message blocks, extracting text and tool results."""
    texts: list[str] = []
    tool_msgs: list[ChatCompletionToolMessageParam] = []

    for block in blocks:
        if isinstance(block, AnthropicTextBlock):
            texts.append(block.text)
        elif isinstance(block, AnthropicToolResultBlock):
            tool_text = block.content if isinstance(block.content, str) else "\n".join(b.text for b in block.content)
            tool_msgs.append(
                ChatCompletionToolMessageParam(role="tool", tool_call_id=block.tool_use_id, content=tool_text)
            )

    return texts, tool_msgs


def anthropic_to_chat_messages(
    messages: list[AnthropicMessage], system: str | None = None
) -> list[ChatCompletionMessageParam]:
    """Translate Anthropic messages into OpenAI Chat Completion format."""
    result: list[ChatCompletionMessageParam] = []

    if system:
        result.append(ChatCompletionSystemMessageParam(role="system", content=system))

    for message in messages:
        # Handle string content
        if isinstance(message.content, str):
            if not message.content.strip():
                continue
            if message.role == AnthropicMessageRole.USER:
                result.append(ChatCompletionUserMessageParam(role="user", content=message.content))
            elif message.role == AnthropicMessageRole.ASSISTANT:
                result.append(ChatCompletionAssistantMessageParam(role="assistant", content=message.content))
            continue

        # Handle block content
        if message.role == AnthropicMessageRole.ASSISTANT:
            content, tool_calls = _handle_assistant_blocks(message.content)
            if tool_calls:
                result.append(
                    ChatCompletionAssistantMessageParam(role="assistant", content=content, tool_calls=tool_calls)
                )
            elif content:
                result.append(ChatCompletionAssistantMessageParam(role="assistant", content=content))

        elif message.role == AnthropicMessageRole.USER:
            texts, tool_msgs = _handle_user_blocks(message.content)
            result.extend(tool_msgs)
            if texts:
                result.append(ChatCompletionUserMessageParam(role="user", content=_join_texts(texts)))

    return result


def anthropic_messages_to_standard(messages: list[AnthropicMessage]) -> list[ChatCompletionMessageParam]:
    """Convert Anthropic messages to ChatCompletionMessageParam for grader context.

    Only extracts text content, suitable for grader context where tool calls are not needed.
    """
    result: list[ChatCompletionMessageParam] = []

    for msg in messages:
        if not (text_content := extract_text_content(msg).strip()):
            continue

        if msg.role == AnthropicMessageRole.USER:
            result.append(ChatCompletionUserMessageParam(role="user", content=text_content))
        elif msg.role == AnthropicMessageRole.ASSISTANT:
            result.append(ChatCompletionAssistantMessageParam(role="assistant", content=text_content))

    return result
