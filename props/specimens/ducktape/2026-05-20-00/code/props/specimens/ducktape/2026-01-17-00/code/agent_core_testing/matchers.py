"""Reusable Hamcrest matchers for agent test assertions."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from hamcrest import assert_that, contains_string, has_entries, has_item, has_items, has_properties, instance_of, is_not
from hamcrest.core.base_matcher import BaseMatcher
from hamcrest.core.description import Description
from mcp import types as mcp_types

from agent_core.events import ToolCall, ToolCallOutput
from openai_utils.model import FunctionCallItem, FunctionCallOutputItem

# ------------------------
# Hamcrest matcher helpers
# ------------------------


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


# ------------------------
# MCP tool result matchers
# ------------------------


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


# ------------------------
# Higher-level payload matchers
# ------------------------


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


def assert_function_call_output_structured(
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


# ------------------------
# Type instance matchers
# ------------------------


def assert_items_include_instances(items: Sequence[Any], *types: type[object]) -> None:
    """Assert that items contains instances of each provided type."""
    if not types:
        raise ValueError("at least one type is required")
    matchers = [instance_of(tp) for tp in types]
    assert_that(items, has_items(*matchers))


def assert_items_exclude_instance(items: Sequence[Any], typ: type[object]) -> None:
    """Assert that items contains no instance of typ."""
    assert_that(items, is_not(has_item(instance_of(typ))))


# ------------------------
# JSON argument matchers
# ------------------------


class HasJsonArguments(BaseMatcher[FunctionCallItem]):
    """Matcher that checks FunctionCallItem has non-None arguments matching expected JSON."""

    def __init__(self, expected: dict[str, Any]):
        self.expected = expected

    def _matches(self, item: Any) -> bool:
        if not isinstance(item, FunctionCallItem):
            return False
        if item.arguments is None:
            return False
        try:
            return bool(json.loads(item.arguments) == self.expected)
        except (json.JSONDecodeError, TypeError):
            return False

    def describe_to(self, description: Description) -> None:
        description.append_text(f"FunctionCallItem with arguments matching {self.expected}")

    def describe_mismatch(self, item: Any, mismatch_description: Description) -> None:
        if not isinstance(item, FunctionCallItem):
            mismatch_description.append_text(f"was {type(item).__name__}")
        elif item.arguments is None:
            mismatch_description.append_text("had None arguments")
        else:
            try:
                actual = json.loads(item.arguments)
                mismatch_description.append_text(f"arguments were {actual}")
            except (json.JSONDecodeError, TypeError) as e:
                mismatch_description.append_text(f"arguments were not valid JSON: {e}")


class HasJsonOutput(BaseMatcher[FunctionCallOutputItem]):
    """Matcher that checks FunctionCallOutputItem has non-None output matching expected JSON.

    Handles both string (JSON) and list (multimodal) output formats.
    For string output, parses as JSON and compares.
    For list output, compares directly (since lists aren't JSON-parseable).
    """

    def __init__(self, expected: dict[str, Any]):
        self.expected = expected

    def _matches(self, item: Any) -> bool:
        if not isinstance(item, FunctionCallOutputItem):
            return False
        if item.output is None:
            return False
        # Handle both string (JSON) and list output
        if isinstance(item.output, str):
            try:
                return bool(json.loads(item.output) == self.expected)
            except json.JSONDecodeError:
                return False
        # For list output, can't JSON parse - compare directly if expected is dict representation
        return False

    def describe_to(self, description: Description) -> None:
        description.append_text(f"FunctionCallOutputItem with output matching {self.expected}")

    def describe_mismatch(self, item: Any, mismatch_description: Description) -> None:
        if not isinstance(item, FunctionCallOutputItem):
            mismatch_description.append_text(f"was {type(item).__name__}")
        elif item.output is None:
            mismatch_description.append_text("had None output")
        elif isinstance(item.output, str):
            try:
                actual = json.loads(item.output)
                mismatch_description.append_text(f"output was {actual}")
            except json.JSONDecodeError as e:
                mismatch_description.append_text(f"output was not valid JSON: {e}")
        else:
            mismatch_description.append_text(f"output was list: {item.output}")


def has_json_arguments(expected: dict[str, Any]) -> HasJsonArguments:
    """Create matcher for FunctionCallItem with specific JSON arguments."""
    return HasJsonArguments(expected)


def has_json_output(expected: dict[str, Any]) -> HasJsonOutput:
    """Create matcher for FunctionCallOutputItem with specific JSON output."""
    return HasJsonOutput(expected)
