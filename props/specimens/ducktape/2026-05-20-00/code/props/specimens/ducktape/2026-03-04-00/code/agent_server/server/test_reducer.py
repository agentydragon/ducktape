from __future__ import annotations

import pytest_bazel
from hamcrest import assert_that, equal_to, has_properties

from agent_server.server.bus import MimeType
from agent_server.server.protocol import UiMessageEvt, UiMessagePayload, UserText
from agent_server.server.reducer import Reducer
from agent_server.server.state import ExecContent, JsonContent, ToolItem
from agent_server.testing.typed_asserts import (
    assert_typed_items_have_one,
    is_assistant_markdown,
    is_exec_content,
    is_json_content,
    is_tool_item,
    is_user_message,
)
from mcp_infra.exec.models import BaseExecResult, Exited


def test_user_text_appends_user_message(fresh_ui_state):
    reducer = Reducer(ui_mount=None)
    result = reducer.reduce(fresh_ui_state, UserText(text="hello"))

    assert_that(result.seq, equal_to(1))
    assert_typed_items_have_one(result.items, is_user_message("hello"))


def test_tool_call_exec_starts_exec_content_with_cmd(fresh_ui_state, make_tool_call):
    reducer = Reducer(ui_mount=None)
    tool_call = make_tool_call("seatbelt", "sandbox_exec", args={"argv": ["echo", "hi there"]})

    result = reducer.reduce(fresh_ui_state, tool_call)

    assert_that(result.seq, equal_to(1))
    assert_typed_items_have_one(result.items, is_tool_item(tool="seatbelt_sandbox_exec", call_id=tool_call.call_id))
    item = result.items[0]
    assert isinstance(item, ToolItem)
    assert_that(item, has_properties(decision=None))
    assert isinstance(item.content, ExecContent)
    assert_that(item.content, is_exec_content(cmd_starts="echo "))


def test_tool_call_json_starts_json_content_with_args(fresh_ui_state, make_tool_call):
    reducer = Reducer(ui_mount=None)
    args = {"foo": 1, "bar": "baz"}
    tool_call = make_tool_call("demo", "inspect", args=args)

    result = reducer.reduce(fresh_ui_state, tool_call)

    assert_that(result.seq, equal_to(1))
    assert_typed_items_have_one(result.items, is_tool_item(call_id=tool_call.call_id))
    item = result.items[0]
    assert isinstance(item, ToolItem)
    assert_that(item.content, is_json_content(args=args))


def test_function_output_updates_exec_stream(fresh_ui_state, make_tool_call, make_function_output):
    reducer = Reducer(ui_mount=None)
    tool_call = make_tool_call("seatbelt", "sandbox_exec", args={"argv": ["ls"]})
    s1 = reducer.reduce(fresh_ui_state, tool_call)

    exec_result = BaseExecResult(stdout="ok", stderr="", exit=Exited(exit_code=0), duration_ms=100)
    output = make_function_output(tool_call.call_id, exec_result.model_dump(mode="json"))
    result = reducer.reduce(s1, output)

    item = result.items[0]
    assert isinstance(item, ToolItem)
    assert isinstance(item.content, ExecContent)
    assert_that(item.content, is_exec_content(stdout="ok", exit_code=0))


def test_function_output_updates_json_output_when_not_exec(fresh_ui_state, make_tool_call, make_function_output):
    reducer = Reducer(ui_mount=None)
    tool_call = make_tool_call("kv", "get", args={"key": "k"})
    s1 = reducer.reduce(fresh_ui_state, tool_call)
    payload = {"value": {"a": 1}}

    output = make_function_output(tool_call.call_id, payload)
    result = reducer.reduce(s1, output)

    item = result.items[0]
    assert isinstance(item, ToolItem)
    assert_that(item.content, is_json_content())
    assert isinstance(item.content, JsonContent)
    assert item.content.result is not None
    assert_that(item.content.result.structuredContent, equal_to(payload))


def test_ui_message_becomes_assistant_markdown(fresh_ui_state):
    reducer = Reducer(ui_mount=None)
    evt = UiMessageEvt(message=UiMessagePayload(mime=MimeType.MARKDOWN, content="**hi**"))

    result = reducer.reduce(fresh_ui_state, evt)

    assert_that(result.seq, equal_to(1))
    assert_typed_items_have_one(result.items, is_assistant_markdown("**hi**"))


if __name__ == "__main__":
    pytest_bazel.main()
