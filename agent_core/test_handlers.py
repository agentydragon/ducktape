"""Tests for agent handlers: CaptureTextHandler and MaxTurnsHandler."""

from __future__ import annotations

import pytest
import pytest_bazel

from agent_core.agent import Agent
from agent_core.events import AssistantText
from agent_core.handler import CaptureTextHandler, FinishOnTextMessageHandler
from agent_core.loop_control import AllowAnyToolOrTextMessage, RequireAnyTool
from agent_core.testing.mcp.responses import EchoMock
from agent_core.turn_limit import MaxTurnsExceededError, MaxTurnsHandler
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


if __name__ == "__main__":
    pytest_bazel.main()
