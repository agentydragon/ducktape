"""Reusable Hamcrest matchers for agent test assertions."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from hamcrest import assert_that, has_item, has_items, has_properties, instance_of
from hamcrest.core.base_matcher import BaseMatcher
from hamcrest.core.description import Description

from agent_core.events import ToolCall, ToolCallOutput
from agent_core.tool_provider import TextContent, ToolResult

# ------------------------
# Tool result matchers
# ------------------------


class HasErrorText(BaseMatcher):
    """Matcher for ToolResult that verifies it's an error with TextContent."""

    def __init__(self, text_matcher):
        self.text_matcher = text_matcher

    def _matches(self, result):
        """Match ToolResult with is_error=True and a single TextContent."""
        if not isinstance(result, ToolResult):
            return False
        if not result.is_error:
            return False
        if len(result.content) != 1:
            return False
        content_item = result.content[0]
        if not isinstance(content_item, TextContent):
            return False
        return self.text_matcher.matches(content_item.text)

    def describe_to(self, description: Description):
        description.append_text("error ToolResult with single text content matching ")
        self.text_matcher.describe_to(description)

    def describe_mismatch(self, result, mismatch_description: Description):
        if not isinstance(result, ToolResult):
            mismatch_description.append_text("was not a ToolResult")
            return
        if not result.is_error:
            mismatch_description.append_text("was not an error (is_error=False)")
            return
        if len(result.content) != 1:
            mismatch_description.append_text(f"expected 1 content item, got {len(result.content)}")
            return
        content_item = result.content[0]
        if not isinstance(content_item, TextContent):
            mismatch_description.append_text(f"content was {type(content_item).__name__}, not TextContent")
            return
        mismatch_description.append_text("error text ")
        self.text_matcher.describe_mismatch(content_item.text, mismatch_description)


def tool_call_with_error_text(text_matcher):
    """Match ToolResult with is_error=True and text content matching the given matcher."""
    return HasErrorText(text_matcher)


# ------------------------
# Higher-level payload matchers
# ------------------------


def assert_function_call_output_structured(
    records: list[ToolCall | ToolCallOutput], structured_content_matcher: Any
) -> None:
    """Assert that a RecordingHandler-style records list contains a function_call_output
    whose structured_content matches the provided matcher.

    Expects Pydantic models (ToolCallOutput), not dicts.

    Example:
        assert_function_call_output_structured(
            recording_handler.records,
            has_entries(echo="hello")
        )
    """
    # Break down nested matchers with explicit Any types for PyHamcrest compatibility
    result_matcher: Any = has_properties(structured_content=structured_content_matcher)
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
