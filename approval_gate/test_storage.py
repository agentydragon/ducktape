from __future__ import annotations

import pytest_bazel
from mcp.types import CallToolResult, TextContent

from approval_gate.models import (
    ActionKey,
    ActionReceivedDetail,
    ActionStatus,
    ApprovedDetail,
    DeniedDetail,
    DoneState,
    PendingState,
    RejectedState,
    ToolCall,
)
from approval_gate.storage import ActionStorage


async def test_create_and_get(storage: ActionStorage):
    call = ToolCall(server_namespace="test", tool_name="exec", arguments={"argv": ["echo", "hi"]})
    action = await storage.create_action(session_key="sess-a", call=call, justification="testing")
    assert action.key == ActionKey(session_key="sess-a", action_seq=1)
    assert isinstance(action.state, PendingState)

    fetched = await storage.get_action(action.key)
    assert fetched is not None
    assert fetched.key == action.key
    assert fetched.call.tool_name == "exec"
    assert fetched.justification == "testing"


async def test_get_missing_returns_none(storage: ActionStorage):
    result = await storage.get_action(ActionKey(session_key="nonexistent", action_seq=999))
    assert result is None


async def test_update_state(storage: ActionStorage):
    call = ToolCall(server_namespace="test", tool_name="exec", arguments={})
    action = await storage.create_action(session_key="sess-a", call=call, justification="test")
    updated = await storage.update_state(
        action.key, DoneState(outcome=CallToolResult(content=[TextContent(type="text", text="ok")]))
    )
    assert updated is not None
    assert isinstance(updated.state, DoneState)
    assert isinstance(updated.state.outcome, CallToolResult)


async def test_list_actions_filter(storage: ActionStorage):
    call = ToolCall(server_namespace="test", tool_name="exec", arguments={})
    a1 = await storage.create_action(session_key="sess-b", call=call, justification="a")
    a2 = await storage.create_action(session_key="sess-b", call=call, justification="b")
    await storage.update_state(a2.key, RejectedState(reason="no"))

    pending = await storage.list_actions(ActionStatus.PENDING)
    keys = {a.key for a in pending}
    assert a1.key in keys
    assert a2.key not in keys

    rejected = await storage.list_actions(ActionStatus.REJECTED)
    assert any(a.key == a2.key for a in rejected)


async def test_list_actions_all(storage: ActionStorage):
    call = ToolCall(server_namespace="test", tool_name="exec", arguments={})
    a1 = await storage.create_action(session_key="sess-all", call=call, justification="x")
    a2 = await storage.create_action(session_key="sess-all", call=call, justification="y")

    all_actions = await storage.list_actions(None)
    keys = {a.key for a in all_actions}
    assert a1.key in keys
    assert a2.key in keys


async def test_action_seq_auto_assigned(storage: ActionStorage):
    call = ToolCall(server_namespace="test", tool_name="exec", arguments={})
    a1 = await storage.create_action(session_key="seq-sess", call=call, justification="first")
    assert a1.key.action_seq == 1

    a2 = await storage.create_action(session_key="seq-sess", call=call, justification="second")
    assert a2.key.action_seq == 2

    a3 = await storage.create_action(session_key="seq-sess", call=call, justification="third")
    assert a3.key.action_seq == 3

    # Different session starts from 1
    other = await storage.create_action(session_key="other-sess", call=call, justification="first")
    assert other.key.action_seq == 1


async def test_append_log_entry(storage: ActionStorage):
    entry1 = await storage.append_log_entry(session_key="log-sess", action_seq=1, detail=ActionReceivedDetail())
    assert entry1.entry_id == 1
    assert entry1.session_key == "log-sess"
    assert entry1.action_seq == 1
    assert isinstance(entry1.detail, ActionReceivedDetail)

    entry2 = await storage.append_log_entry(session_key="log-sess", action_seq=1, detail=DeniedDetail(reason="nope"))
    assert entry2.entry_id == 2
    assert isinstance(entry2.detail, DeniedDetail)
    assert entry2.detail.reason == "nope"

    # Different session starts from 1
    entry_other = await storage.append_log_entry(
        session_key="other-log-sess", action_seq=1, detail=ActionReceivedDetail()
    )
    assert entry_other.entry_id == 1


async def test_get_log_hwm(storage: ActionStorage):
    assert await storage.get_log_hwm("empty-session") == 0

    await storage.append_log_entry(session_key="hwm-sess", action_seq=1, detail=ActionReceivedDetail())
    assert await storage.get_log_hwm("hwm-sess") == 1

    await storage.append_log_entry(session_key="hwm-sess", action_seq=1, detail=ApprovedDetail())
    assert await storage.get_log_hwm("hwm-sess") == 2


async def test_get_log_entry(storage: ActionStorage):
    await storage.append_log_entry(session_key="entry-sess", action_seq=1, detail=ActionReceivedDetail())
    await storage.append_log_entry(session_key="entry-sess", action_seq=1, detail=ApprovedDetail())

    entry = await storage.get_log_entry("entry-sess", 2)
    assert entry is not None
    assert isinstance(entry.detail, ApprovedDetail)

    missing = await storage.get_log_entry("entry-sess", 99)
    assert missing is None


async def test_get_log_entries_since(storage: ActionStorage):
    for i in range(1, 6):
        await storage.append_log_entry(session_key="since-sess", action_seq=i, detail=ActionReceivedDetail())

    entries = await storage.get_log_entries_since("since-sess", after_entry_id=3)
    assert len(entries) == 2
    assert entries[0].entry_id == 4
    assert entries[1].entry_id == 5

    all_entries = await storage.get_log_entries_since("since-sess", after_entry_id=0)
    assert len(all_entries) == 5

    none_entries = await storage.get_log_entries_since("since-sess", after_entry_id=5)
    assert len(none_entries) == 0


if __name__ == "__main__":
    pytest_bazel.main()
