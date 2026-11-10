"""Tests for RedirectOnTextMessageHandler."""

from __future__ import annotations

import pytest
import pytest_bazel
from more_itertools import one

from agent_core.events import AssistantText, ToolCall
from agent_core.handler import RedirectOnTextMessageHandler
from agent_core.loop_control import InjectItems, NoAction
from openai_utils.model import UserMessage

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


@pytest.mark.parametrize(
    ("text", "has_tool_call"),
    [
        pytest.param("", True, id="glm-4.6-empty-output-text-with-tool-call"),
        pytest.param("Let me inspect the workspace.", True, id="prose-preamble-with-tool-call"),
        pytest.param("   ", False, id="whitespace-only-without-tool-call"),
    ],
)
def test_redirect_does_not_inject_without_text_only_response(redirect_handler, text: str, has_tool_call: bool) -> None:
    redirect_handler.on_assistant_text_event(AssistantText(text=text))
    if has_tool_call:
        redirect_handler.on_tool_call_event(_tool_call())

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
