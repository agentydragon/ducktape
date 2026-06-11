"""Tests for handler.py: CaptureTextHandler, FinishOnTextMessageHandler, RedirectOnTextMessageHandler."""

from __future__ import annotations

import pytest
import pytest_bazel
from more_itertools import one

from agent_core.agent import Agent
from agent_core.events import AssistantText, ToolCall
from agent_core.handler import CaptureTextHandler, RedirectOnTextMessageHandler
from agent_core.loop_control import AllowAnyToolOrTextMessage, InjectItems, NoAction
from agent_core.testing.mcp.responses import EchoMock
from openai_utils.model import OpenAIModelProto, UserMessage


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


REMINDER = "Use tools, do not output text directly."


@pytest.fixture
def redirect_handler() -> RedirectOnTextMessageHandler:
    return RedirectOnTextMessageHandler(REMINDER)


def _tool_call() -> ToolCall:
    return ToolCall(name="exec", args_json="{}", call_id="call_1")


def _injected_text(decision: InjectItems) -> str:
    msg = one(decision.items)
    assert isinstance(msg, UserMessage)
    return "".join(part.text for part in (msg.content or []))


def test_redirect_text_only_injects_reminder(redirect_handler) -> None:
    """Non-empty text with no tool call is 'text instead of tools' — redirect."""
    redirect_handler.on_assistant_text_event(AssistantText(text="I think the bug is in foo.py."))

    decision = redirect_handler.on_before_sample()

    assert isinstance(decision, InjectItems)
    assert _injected_text(decision) == REMINDER


def test_redirect_empty_text_with_tool_call_no_reminder(redirect_handler) -> None:
    """The glm-4.6 pattern: an empty output_text message alongside a tool call must NOT redirect."""
    redirect_handler.on_assistant_text_event(AssistantText(text=""))
    redirect_handler.on_tool_call_event(_tool_call())

    assert isinstance(redirect_handler.on_before_sample(), NoAction)


def test_redirect_text_preamble_with_tool_call_no_reminder(redirect_handler) -> None:
    """A prose preamble emitted in the same turn as a tool call is not 'instead of tools'."""
    redirect_handler.on_assistant_text_event(AssistantText(text="Let me inspect the workspace."))
    redirect_handler.on_tool_call_event(_tool_call())

    assert isinstance(redirect_handler.on_before_sample(), NoAction)


def test_redirect_empty_text_only_no_reminder(redirect_handler) -> None:
    """An empty message with no tool call carries no text — nothing to redirect."""
    redirect_handler.on_assistant_text_event(AssistantText(text="   "))

    assert isinstance(redirect_handler.on_before_sample(), NoAction)


def test_redirect_tool_call_only_no_reminder(redirect_handler) -> None:
    redirect_handler.on_tool_call_event(_tool_call())

    assert isinstance(redirect_handler.on_before_sample(), NoAction)


def test_redirect_nothing_no_reminder(redirect_handler) -> None:
    assert isinstance(redirect_handler.on_before_sample(), NoAction)


def test_redirect_flags_reset_between_turns(redirect_handler) -> None:
    """Flags are consumed each sample: a text-only turn redirects, a following tool-only turn does not."""
    redirect_handler.on_assistant_text_event(AssistantText(text="thinking out loud"))
    assert isinstance(redirect_handler.on_before_sample(), InjectItems)

    redirect_handler.on_tool_call_event(_tool_call())
    assert isinstance(redirect_handler.on_before_sample(), NoAction)


if __name__ == "__main__":
    pytest_bazel.main()
