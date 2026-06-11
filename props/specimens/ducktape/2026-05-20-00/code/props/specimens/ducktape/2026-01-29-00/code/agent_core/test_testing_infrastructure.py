"""Tests for agent testing infrastructure: matchers, mocks, and test utilities."""

from __future__ import annotations

from typing import Any

import pytest
import pytest_bazel
from hamcrest import assert_that, contains_string, has_entries, has_item, has_items, has_properties
from hamcrest.core.base_matcher import BaseMatcher
from hamcrest.core.description import Description
from mcp import types as mcp_types

from agent_core.agent import Agent
from agent_core.events import ToolCall, ToolCallOutput
from agent_core.loop_control import RequireAnyTool
from agent_core.testing.matchers import assert_function_call_output_structured
from agent_core_testing.responses import EchoMock
from openai_utils.model import BoundOpenAIModel, OpenAIModelProto, UserMessage

# --- Hamcrest matchers ---


def is_ui_message(content: str | None = None, mime: str | None = None):
    """Matcher: ui_message with optional content and mime constraints."""
    kwargs: dict[str, object] = {}
    if content is not None:
        kwargs["content"] = content
    if mime is not None:
        kwargs["mime"] = mime
    return has_properties(type="ui_message", message=has_properties(**kwargs))


def has_function_call_output_structured(**kvs):
    """Matcher: function_call_output with structured_content containing kvs.

    Expects Pydantic models (ToolCallOutput), not dicts.
    """
    return has_properties(type="function_call_output", result=has_properties(structuredContent=has_entries(**kvs)))


def assert_payloads_have(payloads: list[object], *matchers):
    """Assert payloads contain all matchers using has_items."""
    assert_that(payloads, has_items(*matchers))


# Convenience alias for substring assertions
contains_err = contains_string


class HasErrorText(BaseMatcher):
    """Matcher for CallToolResult that verifies it's an error with TextContent."""

    def __init__(self, text_matcher):
        self.text_matcher = text_matcher

    def _matches(self, result):
        """Match CallToolResult with isError=True and TextContent."""
        if not isinstance(result, mcp_types.CallToolResult):
            return False
        if not result.isError:
            return False
        if not result.content or len(result.content) == 0:
            return False
        content_item = result.content[0]
        if not isinstance(content_item, mcp_types.TextContent):
            return False
        return self.text_matcher.matches(content_item.text)

    def describe_to(self, description: Description):
        description.append_text("error CallToolResult with text content matching ")
        self.text_matcher.describe_to(description)

    def describe_mismatch(self, result, mismatch_description: Description):
        if not isinstance(result, mcp_types.CallToolResult):
            mismatch_description.append_text("was not a CallToolResult")
            return
        if not result.isError:
            mismatch_description.append_text("was not an error (isError=False)")
            return
        if not result.content or len(result.content) == 0:
            mismatch_description.append_text("had empty content")
            return
        content_item = result.content[0]
        if not isinstance(content_item, mcp_types.TextContent):
            mismatch_description.append_text(f"first content was {type(content_item).__name__}, not TextContent")
            return
        mismatch_description.append_text("error text ")
        self.text_matcher.describe_mismatch(content_item.text, mismatch_description)


def tool_call_with_error_text(text_matcher):
    """Match CallToolResult with isError=True and text content matching the given matcher."""
    return HasErrorText(text_matcher)


def is_function_call_output(call_id: str | None = None, **structured_kvs):
    """Matcher: payload is a function_call_output with optional call_id and structuredContent entries.

    Example: is_function_call_output(call_id="call_x", ok=True, echo="hello")
    """
    props: dict[str, object] = {
        "type": "function_call_output",
        "result": has_entries(structured_content=has_entries(**structured_kvs)),
    }
    if call_id is not None:
        props["call_id"] = call_id
    return has_properties(**props)


def is_function_call_output_end_turn(call_id: str | None = None):
    """Matcher: function_call_output for ui.end_turn (kind == EndTurn)."""
    return is_function_call_output(call_id=call_id, kind="EndTurn")


def assert_function_call_output_structured_local(
    records: list[ToolCall | ToolCallOutput], structured_content_matcher: Any
) -> None:
    """Assert that a RecordingHandler-style records list contains a function_call_output
    whose structuredContent matches the provided matcher.

    Expects Pydantic models (ToolCallOutput), not dicts.

    Example:
        assert_function_call_output_structured(
            recording_handler.records,
            has_entries(echo="hello")
        )
    """
    # Break down nested matchers with explicit Any types for PyHamcrest compatibility
    result_matcher: Any = has_properties(structuredContent=structured_content_matcher)
    entry_matcher: Any = has_properties(type="function_call_output", result=result_matcher)
    assert_that(records, has_item(entry_matcher))


# --- Mock execution tests ---


async def test_minicodex_with_sdk_mocks_executes_tool_and_returns_text(
    responses_factory, live_openai, mcp_tool_provider_echo, test_handlers, recording_handler
) -> None:
    # Responses sequence:
    # 1) Model asks to call echo.echo with {"text": "hi"}
    # 2) Model returns a final assistant message "done"

    @EchoMock.mock()
    def mock(m: EchoMock):
        yield
        yield from m.echo_roundtrip("hi")
        yield m.assistant_text("done")

    client: OpenAIModelProto = mock

    agent = await Agent.create(
        tool_provider=mcp_tool_provider_echo, client=client, handlers=test_handlers, tool_policy=RequireAnyTool()
    )
    agent.process_message(UserMessage.text("say hi"))

    res = await agent.run()

    # Verify final text returned
    assert res.text.strip() == "done"
    # Verify the handler saw a function_call_output with the expected structured content
    assert_function_call_output_structured(recording_handler.records, has_entries(echo="hi"))


# --- Live execution tests ---


@pytest.mark.live_openai_api
async def test_minicodex_with_live_api_executes_tool_and_returns_text(
    responses_factory, live_openai, mcp_tool_provider_echo, test_handlers, recording_handler
) -> None:
    client = BoundOpenAIModel(client=live_openai, model=responses_factory.model)

    agent = await Agent.create(
        tool_provider=mcp_tool_provider_echo, client=client, handlers=test_handlers, tool_policy=RequireAnyTool()
    )
    agent.process_message(UserMessage.text("say hi"))

    res = await agent.run()

    assert res.text.strip() == "done"
    assert_function_call_output_structured(recording_handler.records, has_entries(echo="hi"))


if __name__ == "__main__":
    pytest_bazel.main()
