from __future__ import annotations

from collections.abc import Iterator
from enum import StrEnum
from typing import Any, cast

from openai.types.chat import ChatCompletionMessageParam, ChatCompletionMessageToolCallParam
from openai.types.responses import ResponseOutputMessage, ResponseOutputRefusal, ResponseOutputText
from pydantic import TypeAdapter

# Union type for response content parts
type ResponseContentPart = ResponseOutputText | ResponseOutputRefusal


CHAT_TOOL_CALL_ADAPTER = TypeAdapter(list[ChatCompletionMessageToolCallParam])
RESPONSES_MESSAGE_ADAPTER = TypeAdapter(list[ResponseOutputMessage])
CHAT_MESSAGE_ADAPTER = TypeAdapter(list[ChatCompletionMessageParam])
DICT_ADAPTER = TypeAdapter(dict[str, Any])
DICT_LIST_ADAPTER = TypeAdapter(list[dict[str, Any]])


class MessageRole(StrEnum):
    ASSISTANT = "assistant"
    USER = "user"
    SYSTEM = "system"
    TOOL = "tool"
    FUNCTION = "function"
    DEVELOPER = "developer"


# DATA EXTRACTION: Work with validated models only
def response_message_role(message: ResponseOutputMessage) -> MessageRole:
    """Extract role from a ResponseOutputMessage."""
    role_str = cast(str, message.role)
    return MessageRole(role_str)


def chat_param_message_role(message: ChatCompletionMessageParam) -> MessageRole:
    """Extract role from a ChatCompletionMessageParam."""
    return MessageRole(message["role"])


def iter_resolved_text(parts: list[ResponseContentPart]) -> Iterator[str]:
    """Extract text from validated content parts."""
    for part in parts:
        if isinstance(part, ResponseOutputText) and part.text:
            yield part.text
        elif isinstance(part, ResponseOutputRefusal) and part.refusal:
            yield part.refusal


def chat_param_message_tool_calls(message: ChatCompletionMessageParam) -> list[ChatCompletionMessageToolCallParam]:
    """Extract tool calls from a ChatCompletionMessageParam."""
    # Only assistant messages can have tool_calls
    role = MessageRole(message["role"])

    match role:
        case MessageRole.ASSISTANT:
            # ChatCompletionAssistantMessageParam has tool_calls field
            tool_calls = message.get("tool_calls")
            if tool_calls is None:
                return []
            validated = CHAT_TOOL_CALL_ADAPTER.validate_python(tool_calls)
            return cast(list[ChatCompletionMessageToolCallParam], validated)
        case MessageRole.USER | MessageRole.SYSTEM | MessageRole.TOOL | MessageRole.FUNCTION | MessageRole.DEVELOPER:
            # Other message types don't have tool_calls
            return []
        case _:
            raise ValueError(f"Unhandled MessageRole: {role}")


def response_message_content_as_text(message: ResponseOutputMessage) -> str:
    """Extract text content from a ResponseOutputMessage."""
    content = message.content
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    parts = TypeAdapter(list[ResponseContentPart]).validate_python(content)
    return "\n".join(iter_resolved_text(parts))


def chat_param_message_content_as_text(message: ChatCompletionMessageParam) -> str:
    """Extract text content from a ChatCompletionMessageParam."""
    role = MessageRole(message["role"])

    match role:
        case MessageRole.ASSISTANT:
            # ChatCompletionAssistantMessageParam - content is optional
            content = message.get("content")
            if isinstance(content, str):
                return content
            return str(content) if content else ""
        case MessageRole.USER:
            # ChatCompletionUserMessageParam - content is required
            content = message["content"]
            if isinstance(content, str):
                return content
            return str(content)
        case MessageRole.SYSTEM:
            # ChatCompletionSystemMessageParam - content is required
            content = message["content"]
            if isinstance(content, str):
                return content
            return str(content)
        case MessageRole.TOOL | MessageRole.FUNCTION | MessageRole.DEVELOPER:
            # Other message types - handle gracefully
            content = message.get("content")
            if isinstance(content, str):
                return content
            return str(content) if content else ""
        case _:
            raise ValueError(f"Unhandled MessageRole: {role}")


# Removed parse_tool_call and extract_*_tool_call_info - no longer needed since we work with typed objects directly


def parse_response_messages(messages: Any) -> list[ResponseOutputMessage] | None:
    """Parse messages into validated ResponseOutputMessage objects."""
    if not messages:
        return None
    validated = RESPONSES_MESSAGE_ADAPTER.validate_python(messages)
    return cast(list[ResponseOutputMessage], validated)


def dump_response_messages(messages: list[ResponseOutputMessage]) -> list[dict[str, Any]]:
    """Convert validated ResponseOutputMessage objects back to dict form."""
    return [msg.model_dump(by_alias=True) for msg in messages]


def dump_chat_messages(messages: list[ChatCompletionMessageParam]) -> list[dict[str, Any]]:
    """Convert ChatCompletionMessageParam objects to dict form."""
    return [cast(dict[str, Any], DICT_ADAPTER.validate_python(msg)) for msg in messages]


def parse_chat_messages(messages: Any) -> list[ChatCompletionMessageParam] | None:
    """Parse messages into validated ChatCompletionMessageParam objects."""
    if not messages:
        return None
    validated = CHAT_MESSAGE_ADAPTER.validate_python(messages)
    return cast(list[ChatCompletionMessageParam], validated)


def parse_tools_list(tools: Any) -> list[dict[str, Any]]:
    """Parse a list of tools into validated dicts."""
    validated = DICT_LIST_ADAPTER.validate_python(tools if tools else [])
    return cast(list[dict[str, Any]], validated)
