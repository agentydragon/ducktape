from __future__ import annotations

from agent_server.server.reducer import fold_events_to_ui_state
from agent_server.testing.typed_asserts import assert_typed_items_have, is_assistant_markdown, is_user_message
from mcp_infra.prefix import MCPMountPrefix


def test_fold_events_typed_ui_message(make_user_text_event, make_tool_call_event, make_function_output_event) -> None:
    """Verify ui.send_message tool output becomes AssistantMarkdown in UI state."""
    ui_message_content = {"kind": "UiMessage", "mime": "text/markdown", "content": "**hello**"}

    tool_call_event = make_tool_call_event(2, MCPMountPrefix("ui"), "send_message")
    events = [
        make_user_text_event(1, "hi"),
        tool_call_event,
        make_function_output_event(3, tool_call_event.payload.call_id, ui_message_content),
    ]

    state = fold_events_to_ui_state(events)

    assert_typed_items_have(state.items, is_user_message("hi"), is_assistant_markdown("**hello**"))
