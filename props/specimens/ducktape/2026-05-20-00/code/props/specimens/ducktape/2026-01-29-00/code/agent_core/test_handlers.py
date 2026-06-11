"""Tests for agent handlers: CaptureTextHandler, MaxTurnsHandler, and bootstrap integration."""

from __future__ import annotations

import pytest
import pytest_bazel
from fastmcp.server import FastMCP
from pydantic import BaseModel

from agent_core.agent import Agent
from agent_core.events import AssistantText
from agent_core.handler import CaptureTextHandler, FinishOnTextMessageHandler, SequenceHandler
from agent_core.loop_control import AllowAnyToolOrTextMessage, InjectItems, NoAction, RequireAnyTool
from agent_core.testing.assertions import is_all_function_calls
from agent_core.turn_limit import MaxTurnsExceededError, MaxTurnsHandler
from agent_core_testing.responses import EchoMock
from mcp_infra.bootstrap.bootstrap import TypedBootstrapBuilder
from mcp_infra.prefix import MCPMountPrefix
from openai_utils.model import OpenAIModelProto, UserMessage

# --- CaptureTextHandler tests ---


@pytest.fixture
def capture_handler():
    """Fresh CaptureTextHandler for each test."""
    return CaptureTextHandler()


@pytest.fixture
def make_agent_with_capture(mcp_tool_provider_echo, recording_handler, capture_handler):
    """Factory for creating agents with CaptureTextHandler."""

    async def _make(client: OpenAIModelProto):
        return await Agent.create(
            tool_provider=mcp_tool_provider_echo,
            client=client,
            handlers=[capture_handler, recording_handler],
            tool_policy=AllowAnyToolOrTextMessage(),
        )

    return _make


async def test_capture_text_basic(make_agent_with_capture, capture_handler) -> None:
    """Test that CaptureTextHandler captures assistant text."""

    @EchoMock.mock()
    def mock(m: EchoMock):
        yield
        yield m.assistant_text("Hello, world!")

    agent = await make_agent_with_capture(mock)
    agent.process_message(UserMessage.text("greet me"))

    await agent.run()

    assert capture_handler.has_text
    assert capture_handler.take() == "Hello, world!"
    assert not capture_handler.has_text  # Cleared after take()


async def test_capture_text_after_tool_call(make_agent_with_capture, capture_handler) -> None:
    """Test capture after agent makes a tool call then responds."""

    @EchoMock.mock()
    def mock(m: EchoMock):
        yield
        yield from m.echo_roundtrip("testing")
        yield m.assistant_text("Tool call completed.")

    agent = await make_agent_with_capture(mock)
    agent.process_message(UserMessage.text("use echo then respond"))

    await agent.run()

    assert capture_handler.take() == "Tool call completed."


async def test_capture_text_multiple_runs(make_agent_with_capture, capture_handler) -> None:
    """Test capture across multiple agent runs (conversational pattern)."""

    @EchoMock.mock()
    def mock(m: EchoMock):
        yield
        yield m.assistant_text("First response")
        yield m.assistant_text("Second response")

    agent = await make_agent_with_capture(mock)

    # First run
    agent.process_message(UserMessage.text("first question"))
    await agent.run()
    assert capture_handler.take() == "First response"

    # Second run (handler state reset after take)
    agent.process_message(UserMessage.text("second question"))
    await agent.run()
    assert capture_handler.take() == "Second response"


async def test_capture_text_not_captured_raises(capture_handler) -> None:
    """Test that take() raises when no text was captured."""
    with pytest.raises(ValueError, match="No text captured"):
        capture_handler.take()


async def test_has_text_property(capture_handler) -> None:
    """Test has_text property without consuming the text."""
    assert not capture_handler.has_text

    # Simulate receiving text event
    capture_handler.on_assistant_text_event(AssistantText(text="test"))

    assert capture_handler.has_text
    assert capture_handler.has_text  # Still true, not consumed

    # Now consume it
    text = capture_handler.take()
    assert text == "test"
    assert not capture_handler.has_text


# --- MaxTurnsHandler tests ---


@pytest.fixture
def make_agent_with_turn_limit(mcp_tool_provider_echo, recording_handler):
    """Factory for creating agents with turn limit."""

    async def _make(client: OpenAIModelProto, max_turns: int):
        return await Agent.create(
            tool_provider=mcp_tool_provider_echo,
            client=client,
            handlers=[FinishOnTextMessageHandler(), recording_handler, MaxTurnsHandler(max_turns=max_turns)],
            tool_policy=RequireAnyTool(),
        )

    return _make


async def test_turn_limit_exceeded(make_agent_with_turn_limit) -> None:
    """Test that MaxTurnsHandler raises MaxTurnsExceededError when limit is exceeded."""

    @EchoMock.mock()
    def mock(m: EchoMock):
        yield
        yield from m.echo_roundtrip("call1")
        yield from m.echo_roundtrip("call2")
        yield from m.echo_roundtrip("call3")
        yield from m.echo_roundtrip("call4")

    agent = await make_agent_with_turn_limit(mock, max_turns=3)
    agent.process_message(UserMessage.text("keep calling echo"))

    with pytest.raises(MaxTurnsExceededError) as exc_info:
        await agent.run()

    assert "exceeded maximum allowed turns (3)" in str(exc_info.value).lower()
    assert "stuck in a loop" in str(exc_info.value).lower()


async def test_turn_limit_within_bounds(make_agent_with_turn_limit) -> None:
    """Test that agent completes successfully when staying within turn limit."""

    @EchoMock.mock()
    def mock(m: EchoMock):
        yield
        yield from m.echo_roundtrip("call1")
        yield from m.echo_roundtrip("call2")
        yield m.assistant_text("done")

    agent = await make_agent_with_turn_limit(mock, max_turns=5)
    agent.process_message(UserMessage.text("call echo twice"))

    result = await agent.run()
    assert result.text.strip() == "done"


async def test_turn_limit_exactly_at_boundary(make_agent_with_turn_limit) -> None:
    """Test that agent can use exactly max_turns without error."""

    @EchoMock.mock()
    def mock(m: EchoMock):
        yield
        yield from m.echo_roundtrip("call1")
        yield from m.echo_roundtrip("call2")
        yield m.assistant_text("done")

    agent = await make_agent_with_turn_limit(mock, max_turns=3)
    agent.process_message(UserMessage.text("call echo twice"))

    result = await agent.run()
    assert result.text.strip() == "done"


# --- Bootstrap handler tests ---

TEST_SERVER_PREFIX = MCPMountPrefix("test_server")


class TestInput(BaseModel):
    """Test input model for fake MCP server."""

    value: str


class TestOutput(BaseModel):
    """Test output model for fake MCP server."""

    result: str


@pytest.fixture
def test_server() -> FastMCP:
    """Fake MCP server with a single test tool."""
    server = FastMCP("test_server")

    @server.tool()
    def test_tool(input: TestInput) -> TestOutput:
        return TestOutput(result=f"processed: {input.value}")

    return server


async def test_bootstrap_handler_injects_calls_before_first_sampling(test_server):
    """Bootstrap handler injects calls on first on_before_sample() and returns NoAction thereafter."""
    # Create builder with introspection (validates payload types)
    builder = TypedBootstrapBuilder.for_server(test_server)

    # Build calls - auto-generated call_ids
    calls = [
        builder.call(TEST_SERVER_PREFIX, "test_tool", TestInput(value="foo")),
        builder.call(TEST_SERVER_PREFIX, "test_tool", TestInput(value="bar")),
    ]

    # Create handler
    bootstrap = SequenceHandler([InjectItems(items=calls)])

    # First call: should inject calls via InjectItems
    decision = bootstrap.on_before_sample()
    assert isinstance(decision, InjectItems)
    assert len(decision.items) == 2

    # Verify call structure - use TypeGuard to narrow types
    assert is_all_function_calls(decision.items)
    first_call, second_call = decision.items

    assert first_call.name == "test_server_test_tool"
    assert first_call.call_id == "bootstrap:1"  # auto-generated

    assert second_call.name == "test_server_test_tool"
    assert second_call.call_id == "bootstrap:2"

    # Second call: should return NoAction (already injected)
    decision2 = bootstrap.on_before_sample()
    assert isinstance(decision2, NoAction)

    # Third call: should still return NoAction
    decision3 = bootstrap.on_before_sample()
    assert isinstance(decision3, NoAction)


async def test_bootstrap_builder_accepts_any_payload_without_introspection(test_server):
    """TypedBootstrapBuilder without introspection accepts any Pydantic payload."""
    # Note: introspection may not work for all FastMCP configurations
    # This test verifies builder works with or without type validation
    builder = TypedBootstrapBuilder.for_server(test_server)

    # Valid payload: should succeed
    call = builder.call(TEST_SERVER_PREFIX, "test_tool", TestInput(value="test"))
    assert call.name == "test_server_test_tool"

    # Different payload type: should succeed (no validation if introspection fails)
    class WrongInput(BaseModel):
        other_field: int

    call2 = builder.call(TEST_SERVER_PREFIX, "test_tool", WrongInput(other_field=42))
    assert call2.name == "test_server_test_tool"


async def test_bootstrap_builder_auto_generates_call_ids(test_server):
    """TypedBootstrapBuilder auto-generates sequential call_ids."""
    builder = TypedBootstrapBuilder.for_server(test_server)

    # Build multiple calls - verify auto-increment
    call1 = builder.call(TEST_SERVER_PREFIX, "test_tool", TestInput(value="a"))
    call2 = builder.call(TEST_SERVER_PREFIX, "test_tool", TestInput(value="b"))
    call3 = builder.call(TEST_SERVER_PREFIX, "test_tool", TestInput(value="c"))

    assert call1.call_id == "bootstrap:1"
    assert call2.call_id == "bootstrap:2"
    assert call3.call_id == "bootstrap:3"


async def test_bootstrap_builder_custom_call_id_prefix(test_server):
    """TypedBootstrapBuilder supports custom call_id prefix."""
    builder = TypedBootstrapBuilder.for_server(test_server, call_id_prefix="init")

    call = builder.call(TEST_SERVER_PREFIX, "test_tool", TestInput(value="test"))
    assert call.call_id == "init:1"


async def test_bootstrap_builder_explicit_call_id(test_server):
    """TypedBootstrapBuilder accepts explicit call_id override."""
    builder = TypedBootstrapBuilder.for_server(test_server)

    call = builder.call(TEST_SERVER_PREFIX, "test_tool", TestInput(value="test"), call_id="custom-id")
    assert call.call_id == "custom-id"


async def test_bootstrap_builder_without_introspection():
    """TypedBootstrapBuilder works without introspection (no type validation)."""
    # Create builder without server introspection
    builder = TypedBootstrapBuilder()

    # Should accept any payload without validation
    call = builder.call(MCPMountPrefix("unknown_server"), "unknown_tool", TestInput(value="test"))
    assert call.name == "unknown_server_unknown_tool"
    assert call.call_id == "bootstrap:1"


if __name__ == "__main__":
    pytest_bazel.main()
