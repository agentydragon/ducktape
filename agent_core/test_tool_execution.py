"""Tests for tool execution: errors, parallel calls, malformed input, sanitization, and schema generation."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Any, Literal

import pytest
import pytest_bazel
from fastmcp import FastMCP
from fastmcp.client import Client
from fastmcp.exceptions import ToolError
from hamcrest import anything, assert_that, contains_string, greater_than_or_equal_to, has_entries, has_length
from more_itertools import one
from pydantic import BaseModel

from agent_core.agent import Agent, _make_strict_function_tool, _sanitize_tool_result
from agent_core.events import ToolCall, ToolCallOutput
from agent_core.handler import BaseHandler
from agent_core.loop_control import Abort, InjectItems, RequireAnyTool
from agent_core.mcp_provider import MCPToolProvider
from agent_core.testing.handlers import FinishOnTextMessageHandler
from agent_core.testing.matchers import assert_function_call_output_structured, tool_call_with_error_text
from agent_core.testing.mcp.echo_server import ECHO_TOOL_NAME
from agent_core.testing.responses import DecoratorMock, ResponsesFactory
from agent_core.tool_provider import ImageContent, TextContent, ToolResult
from openai_utils.model import FunctionCallItem, ResponsesRequest, ResponsesResult, UserMessage
from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel
from openai_utils.testing.openai_mock import NoopOpenAIClient, make_mock


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


# ---- Local fixtures (only used by this module) ----


class _EmptyInput(BaseModel):
    """Payload helper for mocked no-argument rendezvous calls."""


class _RendezvousOutput(BaseModel):
    """Output for rendezvous tools."""

    ok: bool
    tool: str
    args: dict[str, Any]


class SendMessageInput(OpenAIStrictModeBaseModel):
    mime: Literal["text/markdown", "text/plain"]
    content: str


@pytest.fixture
def rendezvous_server() -> FastMCP:
    """FastMCP server whose tool returns only once a second call of it is running too."""
    mcp = FastMCP("dummy")
    both_running = asyncio.Barrier(2)

    @mcp.tool(output_schema=None)
    async def rendezvous() -> _RendezvousOutput:
        """Waits for another call of this tool to be in flight."""
        # Serial execution never brings the second call; the timeout turns that deadlock into a failure.
        async with asyncio.timeout(5):
            await both_running.wait()
        return _RendezvousOutput(ok=True, tool="rendezvous", args={})

    return mcp


class ValidationServer(FastMCP):
    """FastMCP server with a tool that validates input strictly."""

    send_message_tool: Callable[..., dict[str, Any]]

    def __init__(self):
        super().__init__("validator")

        def send_message(mime: Literal["text/markdown", "text/plain"], content: str) -> dict[str, Any]:
            """Send a message with mime type validation."""
            if mime == "text/plain":
                raise ToolError("Validation error: Only text/markdown is supported, not text/plain")
            return {"ok": True, "message": content}

        self.send_message_tool = self.tool(output_schema=None)(send_message)


@pytest.fixture
def validation_server() -> ValidationServer:
    """ValidationServer with typed tool access."""
    return ValidationServer()


# --- Tool error continuation tests ---


async def test_tool_error_continues_turn(validation_server, recording_handler) -> None:
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
        agent = Agent(
            tool_provider=MCPToolProvider(client),
            client=mock,
            handlers=[FinishOnTextMessageHandler(), recording_handler],
            tool_policy=RequireAnyTool(),
        )
        agent.process_message(UserMessage.text("send message"))
        result = await agent.run()

    # Verify the sequence of events
    tool_calls = [evt for evt in recording_handler.records if evt.type == "tool_call"]
    outputs = [evt for evt in recording_handler.records if evt.type == "function_call_output"]

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


# --- Parallel tool call tests ---


class OneShotSyntheticHandler(BaseHandler):
    """Handler that injects synthetic output once, then aborts."""

    def __init__(self, outputs: list[Any]):
        self._done = False
        self._outputs = outputs

    def on_before_sample(self):
        if not self._done:
            self._done = True
            return InjectItems(items=tuple(self._outputs))
        return Abort()


async def test_parallel_tool_calls_run_concurrently(rendezvous_server, recording_handler, responses_factory):
    tc1 = responses_factory.tool_call("rendezvous", {}, call_id="call_1")
    tc2 = responses_factory.tool_call("rendezvous", {}, call_id="call_2")

    handler = OneShotSyntheticHandler(outputs=[tc1, tc2])

    async with Client(rendezvous_server) as client:
        agent = Agent(
            tool_provider=MCPToolProvider(client),
            client=NoopOpenAIClient(),  # SyntheticAction path bypasses OpenAI
            parallel_tool_calls=True,
            handlers=[handler, recording_handler],
            tool_policy=RequireAnyTool(),
        )
        agent.process_message(UserMessage.text("go"))
        await agent.run()

    tool_calls = [e for e in recording_handler.records if isinstance(e, ToolCall)]
    tool_outputs = [e for e in recording_handler.records if isinstance(e, ToolCallOutput)]
    assert_that(tool_calls, has_length(greater_than_or_equal_to(2)))
    assert_that(tool_outputs, has_length(greater_than_or_equal_to(2)))
    assert not any(output.result.is_error for output in tool_outputs)


# --- Malformed JSON tests ---


async def _run_malformed_json_test(
    mcp_tool_provider_echo, recording_handler, *first_turn_calls: FunctionCallItem, parallel: bool = False
) -> tuple[str, list]:
    """Run tool calls and capture the agent's response and events."""
    factory = ResponsesFactory()

    async def handle_request(req: ResponsesRequest) -> ResponsesResult:
        if len(req.input) == 1:
            return factory.make(*first_turn_calls)
        return factory.make_assistant_message("I received an error")

    client = make_mock(handle_request)
    agent = Agent(
        tool_provider=mcp_tool_provider_echo,
        client=client,
        handlers=[FinishOnTextMessageHandler(), recording_handler],
        parallel_tool_calls=parallel,
        tool_policy=RequireAnyTool(),
    )
    agent.process_message(UserMessage.text("use echo"))

    res = await agent.run()
    events = recording_handler.records
    return res, events


@pytest.mark.parametrize(
    "arguments", ['{"text": "unterminated string', '["not", "an", "object"]'], ids=["malformed-json", "non-object-json"]
)
async def test_invalid_tool_argument_json(mcp_tool_provider_echo, recording_handler, arguments) -> None:
    call = FunctionCallItem(type="function_call", name=ECHO_TOOL_NAME, arguments=arguments, call_id="test:1")
    text, events = await _run_malformed_json_test(mcp_tool_provider_echo, recording_handler, call)

    assert text == "I received an error"

    tool_outputs = [evt for evt in events if isinstance(evt, ToolCallOutput)]
    assert len(tool_outputs) == 1

    tool_result = tool_outputs[0].result
    assert_that(tool_result, tool_call_with_error_text(anything()))


async def test_malformed_json_parallel_tool_calls(mcp_tool_provider_echo, recording_handler) -> None:
    """Test malformed JSON handling with parallel tool calls enabled."""
    text, events = await _run_malformed_json_test(
        mcp_tool_provider_echo,
        recording_handler,
        FunctionCallItem(
            type="function_call", name=ECHO_TOOL_NAME, arguments='{"text": "good call"}', call_id="test:1"
        ),
        FunctionCallItem(type="function_call", name=ECHO_TOOL_NAME, arguments='{"text": "bad', call_id="test:2"),
        parallel=True,
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


# --- Null byte sanitization tests ---


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
