"""Reusable Hamcrest matchers for agent test assertions."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from hamcrest import assert_that, has_item, has_items, has_properties, instance_of

from skills.info_gathering.evals.agent_runtime.events import ToolCall, ToolCallOutput


def assert_function_call_output_structured(
    records: list[ToolCall | ToolCallOutput], structured_content_matcher: Any
) -> None:
    """Assert that a records list contains a function_call_output
    whose structured_content matches the provided matcher.

    Expects Pydantic models (ToolCallOutput), not dicts.

    Example:
        assert_function_call_output_structured(
            records,
            has_entries(echo="hello")
        )
    """
    # Break down nested matchers with explicit Any types for PyHamcrest compatibility
    result_matcher: Any = has_properties(structured_content=structured_content_matcher)
    entry_matcher: Any = has_properties(type="function_call_output", result=result_matcher)
    assert_that(records, has_item(entry_matcher))


def assert_items_include_instances(items: Sequence[Any], *types: type[object]) -> None:
    """Assert that items contains instances of each provided type."""
    if not types:
        raise ValueError("at least one type is required")
    matchers = [instance_of(tp) for tp in types]
    assert_that(items, has_items(*matchers))
