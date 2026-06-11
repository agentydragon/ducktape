from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from hamcrest import assert_that, has_item, has_items, has_length, has_properties, instance_of, is_not
from hamcrest.core.base_matcher import BaseMatcher
from hamcrest.core.description import Description

from agent_server.server.state import ExecContent, JsonContent


def is_user_message(text: str | None = None):
    props = {"kind": "UserMessage"}
    if text is not None:
        props["text"] = text
    return has_properties(**props)


def is_assistant_markdown(md: str | None = None):
    props = {"kind": "AssistantMarkdown"}
    if md is not None:
        props["md"] = md
    return has_properties(**props)


def is_tool_item(tool: str | None = None, call_id: str | None = None):
    props = {"kind": "Tool"}
    # ToolItem has tool_call with name and call_id inside
    tool_call_props = {}
    if tool is not None:
        tool_call_props["name"] = tool
    if call_id is not None:
        tool_call_props["call_id"] = call_id
    if tool_call_props:
        props["tool_call"] = has_properties(**tool_call_props)
    return has_properties(**props)


def is_end_turn_item():
    """Matcher for EndTurn UI items."""
    return has_properties(kind="EndTurn")


class IsExecContent(BaseMatcher):
    """Matcher for ExecContent with optional property checks."""

    def __init__(self, *, cmd_starts: str | None = None, stdout: str | None = None, exit_code: int | None = None):
        self._cmd_starts = cmd_starts
        self._stdout = stdout
        self._exit_code = exit_code

    def _matches(self, item) -> bool:
        if not isinstance(item, ExecContent):
            return False
        if self._cmd_starts is not None and not (item.cmd or "").startswith(self._cmd_starts):
            return False
        if self._stdout is not None and item.stdout != self._stdout:
            return False
        return self._exit_code is None or item.exit_code == self._exit_code

    def describe_to(self, description: Description) -> None:
        description.append_text("ExecContent")
        if self._cmd_starts:
            description.append_text(f" with cmd starting '{self._cmd_starts}'")
        if self._stdout is not None:
            description.append_text(f" stdout={self._stdout!r}")
        if self._exit_code is not None:
            description.append_text(f" exit_code={self._exit_code}")


def is_exec_content(*, cmd_starts: str | None = None, stdout: str | None = None, exit_code: int | None = None):
    """Matcher for ExecContent with optional property checks."""
    return IsExecContent(cmd_starts=cmd_starts, stdout=stdout, exit_code=exit_code)


class IsJsonContent(BaseMatcher):
    """Matcher for JsonContent with optional property checks."""

    def __init__(self, *, args: dict | None = None):
        self._args = args

    def _matches(self, item) -> bool:
        if not isinstance(item, JsonContent):
            return False
        return self._args is None or item.args == self._args

    def describe_to(self, description: Description) -> None:
        description.append_text("JsonContent")
        if self._args is not None:
            description.append_text(f" args={self._args!r}")


def is_json_content(args: dict | None = None):
    """Matcher for JsonContent with optional args check."""
    return IsJsonContent(args=args)


# --- Assertion helpers ---


def assert_typed_items_have(items: Sequence[Any], *matchers) -> None:
    assert_that(items, has_items(*matchers))


def assert_typed_items_have_one(items: Sequence[Any], matcher) -> None:
    assert_that(items, has_item(matcher))


def assert_items_count(items: Sequence[Any], n: int) -> None:
    """Assert items has exactly n elements."""
    assert_that(items, has_length(n))


def assert_empty(items: Sequence[Any]) -> None:
    """Assert items is empty."""
    assert_that(items, has_length(0))


def assert_items_include_instances(items: Sequence[Any], *types: type[object]) -> None:
    """Assert that ``items`` contains instances of each provided type."""

    if not types:
        raise ValueError("at least one type is required")
    matchers = [instance_of(tp) for tp in types]
    assert_that(items, has_items(*matchers))


def assert_items_exclude_instance(items: Sequence[Any], typ: type[object]) -> None:
    """Assert that ``items`` contains no instance of ``typ``."""

    assert_that(items, is_not(has_item(instance_of(typ))))
