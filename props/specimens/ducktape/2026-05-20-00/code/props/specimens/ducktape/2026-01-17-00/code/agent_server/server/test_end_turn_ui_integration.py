"""Test end_turn UI integration - ensures tool doesn't double-emit and renders as thick HR."""

from hamcrest import assert_that, has_length, instance_of

from agent_server.server.bus import ServerBus, UiEndTurn
from agent_server.server.protocol import UiEndTurnEvt
from agent_server.server.reducer import Reducer
from agent_server.server.state import new_state
from agent_server.testing.typed_asserts import assert_empty, assert_items_count, is_end_turn_item, is_tool_item


def test_end_turn_tool_filtering(make_tool_call, make_function_output):
    """end_turn tool calls should not create Tool items in UI state."""
    reducer = Reducer(ui_mount=None)
    state = new_state()

    # ToolCall for end_turn should be filtered out
    after_call = reducer.reduce(state, make_tool_call("ui", "end_turn", "test-123"))
    assert_empty(after_call.items)

    # Function output for end_turn should also be filtered out
    after_output = reducer.reduce(after_call, make_function_output("test-123", {"ok": True}))
    assert_empty(after_output.items)


def test_end_turn_event_creates_separator():
    """UiEndTurnEvt should create EndTurn items that render as thick HR."""
    reducer = Reducer(ui_mount=None)
    state = new_state()

    result = reducer.reduce(state, UiEndTurnEvt())

    assert_items_count(result.items, 1)
    assert_that(result.items[0], is_end_turn_item())


def test_ui_bus_end_turn_flow():
    """UiBus.push_end_turn should create UiEndTurn item."""
    bus = ServerBus()

    bus.push_end_turn()
    messages = bus.drain_messages()

    assert_that(messages, has_length(1))
    assert_that(messages[0], instance_of(UiEndTurn))
    assert_that(messages[0], is_end_turn_item())


def test_ui_send_message_still_filtered(make_tool_call):
    """send_message tool should still be filtered (regression test)."""
    reducer = Reducer(ui_mount=None)
    state = new_state()

    after_send = reducer.reduce(state, make_tool_call("ui", "send_message", "msg-123"))
    after_end = reducer.reduce(after_send, make_tool_call("ui", "end_turn", "end-123"))

    assert_empty(after_send.items)
    assert_empty(after_end.items)


def test_regular_tools_not_affected(make_tool_call):
    """Regular tools should still create Tool items."""
    reducer = Reducer(ui_mount=None)
    state = new_state()

    tool_call = make_tool_call("echo", "echo", args={"text": "test"})
    after_regular = reducer.reduce(state, tool_call)

    assert_items_count(after_regular.items, 1)
    assert_that(after_regular.items[0], is_tool_item(tool="echo_echo", call_id=tool_call.call_id))
