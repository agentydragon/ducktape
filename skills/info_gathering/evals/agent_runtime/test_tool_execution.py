"""Tests for tool execution: errors, malformed input, sanitization, and schema generation."""

from __future__ import annotations

import re
from typing import Any, Literal

import pytest
import pytest_bazel
from fastmcp import FastMCP
from fastmcp.client import Client
from fastmcp.exceptions import ToolError
from hamcrest import anything, assert_that, contains_string, has_entries
from more_itertools import one

from openai_utils.model import FunctionCallItem, ResponsesRequest, ResponsesResult, UserMessage
from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel
from openai_utils.testing.openai_mock import make_mock
from openai_utils.testing.responses import DecoratorMock, ResponsesFactory
from skills.info_gathering.evals.agent_runtime.agent import Agent, _make_strict_function_tool, _sanitize_tool_result
from skills.info_gathering.evals.agent_runtime.events import ToolCallOutput
from skills.info_gathering.evals.agent_runtime.loop_control import RequireAnyTool
from skills.info_gathering.evals.agent_runtime.mcp_provider import MCPToolProvider
from skills.info_gathering.evals.agent_runtime.testing.handlers import TextCaptureHandler, run_until_text
from skills.info_gathering.evals.agent_runtime.testing.matchers import (
    assert_function_call_output_structured,
    tool_call_with_error_text,
)
from skills.info_gathering.evals.agent_runtime.testing.mcp.echo_server import ECHO_TOOL_NAME
from skills.info_gathering.evals.agent_runtime.tool_provider import ImageContent, TextContent, ToolResult


def test_agent_tool_boundary_validates_nested_union() -> None:
    object_variant: dict[str, Any] = {"type": "object", "properties": {}}
    schema = {
        "type": "object",
        "properties": {"payload": {"anyOf": [{"type": "string"}, object_variant]}},
        "required": ["payload"],
        "additionalProperties": False,
    }
    with pytest.raises(ValueError, match="additionalProperties"):
        _make_strict_function_tool("sample", "sample", schema)
    object_variant["additionalProperties"] = False
    assert _make_strict_function_tool("sample", "sample", schema).strict is True


class SendMessageInput(OpenAIStrictModeBaseModel):
    mime: Literal["text/markdown", "text/plain"]
    content: str


class ValidationServer(FastMCP):
    """FastMCP server with a tool that validates input strictly."""

    def __init__(self):
        super().__init__("validator")

        def send_message(mime: Literal["text/markdown", "text/plain"], content: str) -> dict[str, Any]:
            """Send a message with mime type validation."""
            if mime == "text/plain":
                raise ToolError("Validation error: Only text/markdown is supported, not text/plain")
            return {"ok": True, "message": content}

        self.tool(output_schema=None)(send_message)


@pytest.fixture
def validation_server() -> ValidationServer:
    """ValidationServer with typed tool access."""
    return ValidationServer()


async def test_tool_error_continues_turn(validation_server) -> None:
    """Test that a tool validation error doesn't abort the turn.

    The agent should:
    1. Call the tool with wrong mime type (text/plain)
    2. Get a validation error
    3. Continue to the next phase
    4. Retry with correct mime type (text/markdown)
    5. Successfully complete
    """

    @DecoratorMock.mock()
    def mock(m: DecoratorMock):
        yield
        # First attempt with wrong mime type
        yield m.tool_call("send_message", SendMessageInput(mime="text/plain", content="Hello"))
        # After error, agent retries with correct mime type
        yield m.tool_call("send_message", SendMessageInput(mime="text/markdown", content="Hello"))
        yield m.assistant_text("Successfully sent message")

    async with Client(validation_server) as client:
        capture = TextCaptureHandler()
        agent = Agent(
            tool_provider=MCPToolProvider(client), client=mock, handlers=[capture], tool_policy=RequireAnyTool()
        )
        agent.process_message(UserMessage.text("send message"))
        result = await run_until_text(agent, capture)

    # Verify the sequence of events
    tool_calls = [evt for evt in agent._transcript if isinstance(evt, FunctionCallItem)]
    outputs = [evt for evt in agent._transcript if isinstance(evt, ToolCallOutput)]

    assert len(tool_calls) == 2, f"Expected 2 tool calls, got {len(tool_calls)}"

    # First call should fail with validation error
    first_output = outputs[0]
    assert first_output.result.is_error is True
    error_block = one(first_output.result.content)
    assert isinstance(error_block, TextContent)
    error_content = error_block.text
    assert_that(error_content, contains_string("text/markdown"))

    # Second call should succeed
    second_output = outputs[1]
    assert second_output.result.is_error is False
    assert_function_call_output_structured([second_output], has_entries(ok=True))

    assert_that(result, contains_string("Successfully sent message"))


async def _run_malformed_json_test(mcp_tool_provider_echo, *first_turn_calls: FunctionCallItem) -> tuple[str, list]:
    """Run tool calls and capture the agent's response and events."""
    factory = ResponsesFactory()

    async def handle_request(req: ResponsesRequest) -> ResponsesResult:
        if len(req.input) == 1:
            return factory.make(*first_turn_calls)
        return factory.make(factory.assistant_text("I received an error"))

    client = make_mock(handle_request)
    capture = TextCaptureHandler()
    agent = Agent(tool_provider=mcp_tool_provider_echo, client=client, handlers=[capture], tool_policy=RequireAnyTool())
    agent.process_message(UserMessage.text("use echo"))

    res = await run_until_text(agent, capture)
    events = agent._transcript
    return res, events


@pytest.mark.parametrize(
    "arguments", ['{"text": "unterminated string', '["not", "an", "object"]'], ids=["malformed-json", "non-object-json"]
)
async def test_invalid_tool_argument_json(mcp_tool_provider_echo, arguments) -> None:
    call = FunctionCallItem(type="function_call", name=ECHO_TOOL_NAME, arguments=arguments, call_id="test:1")
    text, events = await _run_malformed_json_test(mcp_tool_provider_echo, call)

    assert text == "I received an error"

    tool_outputs = [evt for evt in events if isinstance(evt, ToolCallOutput)]
    assert len(tool_outputs) == 1

    tool_result = tool_outputs[0].result
    assert_that(tool_result, tool_call_with_error_text(anything()))


async def test_malformed_json_multiple_tool_calls(mcp_tool_provider_echo) -> None:
    """A malformed call does not prevent another call in the same response."""
    text, events = await _run_malformed_json_test(
        mcp_tool_provider_echo,
        FunctionCallItem(
            type="function_call", name=ECHO_TOOL_NAME, arguments='{"text": "good call"}', call_id="test:1"
        ),
        FunctionCallItem(type="function_call", name=ECHO_TOOL_NAME, arguments='{"text": "bad', call_id="test:2"),
    )

    # Agent should complete successfully
    assert text

    # Check that we got two tool results: one success, one error
    tool_outputs = [evt for evt in events if isinstance(evt, ToolCallOutput)]
    assert len(tool_outputs) == 2

    # One should be error, one should be success
    results = [out.result for out in tool_outputs]
    error_count = sum(1 for r in results if r.is_error)
    success_count = sum(1 for r in results if not r.is_error)

    assert error_count == 1
    assert success_count == 1


def test_no_nulls_unchanged():
    result = ToolResult(content=[TextContent(text="clean")], is_error=False)
    sanitized = _sanitize_tool_result(result)
    assert sanitized.content == result.content


def _assert_null_notice(text: str, *, count: int) -> None:
    """The notice is prose for the model; the removed-null count is the part that has to be right."""
    assert re.search(rf"\b{count}\b", text)


def test_nulls_in_text():
    result = ToolResult(content=[TextContent(text="a\x00b\x00c")], is_error=False)
    sanitized = _sanitize_tool_result(result)
    first_item = sanitized.content[0]
    assert isinstance(first_item, TextContent)
    text = first_item.text
    assert text.endswith("abc")
    assert text != "abc"
    _assert_null_notice(text, count=2)


def test_nulls_in_structured():
    result = ToolResult(
        content=[TextContent(text="kept")], structured_content={"k": "v\x00", "nested": {"d": "x\x00"}}, is_error=False
    )
    sanitized = _sanitize_tool_result(result)
    assert sanitized.structured_content == {"k": "v", "nested": {"d": "x"}}
    first_item = sanitized.content[0]
    assert isinstance(first_item, TextContent)
    assert first_item.text.endswith("kept")
    assert first_item.text != "kept"
    _assert_null_notice(first_item.text, count=2)


def test_empty_content_nulls_in_structured():
    result = ToolResult(content=[], structured_content={"a": "b\x00"}, is_error=False)
    sanitized = _sanitize_tool_result(result)
    assert len(sanitized.content) == 1
    first_item = sanitized.content[0]
    assert isinstance(first_item, TextContent)
    _assert_null_notice(first_item.text, count=1)


def test_prepends_to_first_text_block():
    result = ToolResult(content=[TextContent(text="a\x00"), TextContent(text="b\x00")], is_error=False)
    sanitized = _sanitize_tool_result(result)
    first_item = sanitized.content[0]
    second_item = sanitized.content[1]
    assert isinstance(first_item, TextContent)
    assert isinstance(second_item, TextContent)
    assert first_item.text.endswith("a")
    assert first_item.text != "a"
    _assert_null_notice(first_item.text, count=2)
    assert second_item.text == "b"


def test_inserts_before_non_text_first_block():
    result = ToolResult(
        content=[ImageContent(mime_type="image/png", data="data"), TextContent(text="a\x00")], is_error=False
    )
    sanitized = _sanitize_tool_result(result)
    assert len(sanitized.content) == 3
    first_item = sanitized.content[0]
    assert isinstance(first_item, TextContent)
    _assert_null_notice(first_item.text, count=1)
    assert isinstance(sanitized.content[1], ImageContent)


def test_nested_structures():
    result = ToolResult(
        content=[TextContent(text="x")], structured_content={"a": {"b": {"c": ["x\x00", "y\x00"]}}}, is_error=False
    )
    sanitized = _sanitize_tool_result(result)
    assert sanitized.structured_content == {"a": {"b": {"c": ["x", "y"]}}}


def test_preserves_error_flag():
    result = ToolResult(content=[TextContent(text="err\x00")], is_error=True)
    sanitized = _sanitize_tool_result(result)
    assert sanitized.is_error is True


if __name__ == "__main__":
    pytest_bazel.main()
