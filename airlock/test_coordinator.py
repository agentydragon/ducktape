from __future__ import annotations

from datetime import UTC, datetime

import pytest_bazel
from mcp.types import CallToolResult, TextContent

from airlock.coordinator import ActionCoordinator, ActionCreatedEvent, ActionUpdatedEvent, CoordinatorEvent
from airlock.models import (
    ActionKey,
    ActionReceivedDetail,
    ActionStatus,
    DeniedDetail,
    DoneState,
    ExecutionStartedDetail,
    PendingState,
    RejectedState,
    ToolCall,
)

_NOW = datetime.now(tz=UTC)


async def test_create_and_get(coordinator: ActionCoordinator):
    call = ToolCall(server_namespace="test", tool_name="exec", arguments={"argv": ["echo", "hi"]})
    action = await coordinator.create_action(
        session_key="sess-a", call=call, justification="testing", client_id=None, subject=None
    )
    assert action.key == ActionKey(session_key="sess-a", action_seq=1)
    assert isinstance(action.state, PendingState)

    fetched = await coordinator.get_action(action.key)
    assert fetched is not None
    assert fetched.key == action.key
    assert fetched.call.tool_name == "exec"
    assert fetched.justification == "testing"


async def test_get_missing_returns_none(coordinator: ActionCoordinator):
    result = await coordinator.get_action(ActionKey(session_key="nonexistent", action_seq=999))
    assert result is None


async def test_update_and_log(coordinator: ActionCoordinator):
    call = ToolCall(server_namespace="test", tool_name="exec", arguments={})
    action = await coordinator.create_action(
        session_key="sess-a", call=call, justification="test", client_id=None, subject=None
    )
    updated = await coordinator.update_and_log(
        action.key,
        DoneState(outcome=CallToolResult(content=[TextContent(type="text", text="ok")])),
        ExecutionStartedDetail(started_at=_NOW),
    )
    assert isinstance(updated.state, DoneState)
    assert isinstance(updated.state.outcome, CallToolResult)


async def test_list_actions_filter(coordinator: ActionCoordinator):
    call = ToolCall(server_namespace="test", tool_name="exec", arguments={})
    a1 = await coordinator.create_action(
        session_key="sess-b", call=call, justification="a", client_id=None, subject=None
    )
    a2 = await coordinator.create_action(
        session_key="sess-b", call=call, justification="b", client_id=None, subject=None
    )
    await coordinator.update_and_log(a2.key, RejectedState(reason="no"), DeniedDetail(reason="no"))

    pending = await coordinator.list_actions(ActionStatus.PENDING)
    keys = {a.key for a in pending}
    assert a1.key in keys
    assert a2.key not in keys

    rejected = await coordinator.list_actions(ActionStatus.REJECTED)
    assert any(a.key == a2.key for a in rejected)


async def test_list_actions_all(coordinator: ActionCoordinator):
    call = ToolCall(server_namespace="test", tool_name="exec", arguments={})
    a1 = await coordinator.create_action(
        session_key="sess-all", call=call, justification="x", client_id=None, subject=None
    )
    a2 = await coordinator.create_action(
        session_key="sess-all", call=call, justification="y", client_id=None, subject=None
    )

    all_actions = await coordinator.list_actions(None)
    keys = {a.key for a in all_actions}
    assert a1.key in keys
    assert a2.key in keys


async def test_action_seq_auto_assigned(coordinator: ActionCoordinator):
    call = ToolCall(server_namespace="test", tool_name="exec", arguments={})
    a1 = await coordinator.create_action(
        session_key="seq-sess", call=call, justification="first", client_id=None, subject=None
    )
    assert a1.key.action_seq == 1

    a2 = await coordinator.create_action(
        session_key="seq-sess", call=call, justification="second", client_id=None, subject=None
    )
    assert a2.key.action_seq == 2

    a3 = await coordinator.create_action(
        session_key="seq-sess", call=call, justification="third", client_id=None, subject=None
    )
    assert a3.key.action_seq == 3

    # Different session starts from 1
    other = await coordinator.create_action(
        session_key="other-sess", call=call, justification="first", client_id=None, subject=None
    )
    assert other.key.action_seq == 1


async def test_log_hwm(coordinator: ActionCoordinator):
    assert await coordinator.get_log_hwm("empty-session") == 0

    # create_action appends an ActionReceived log entry
    call = ToolCall(server_namespace="test", tool_name="exec", arguments={})
    action = await coordinator.create_action(
        session_key="hwm-sess", call=call, justification="x", client_id=None, subject=None
    )
    assert await coordinator.get_log_hwm("hwm-sess") == 1

    await coordinator.update_and_log(action.key, RejectedState(reason="no"), DeniedDetail(reason="no"))
    assert await coordinator.get_log_hwm("hwm-sess") == 2


async def test_get_log_entry(coordinator: ActionCoordinator):
    call = ToolCall(server_namespace="test", tool_name="exec", arguments={})
    action = await coordinator.create_action(
        session_key="entry-sess", call=call, justification="x", client_id=None, subject=None
    )

    # Entry 1 is ActionReceived from create_action
    entry = await coordinator.get_log_entry("entry-sess", 1)
    assert entry is not None
    assert isinstance(entry.detail, ActionReceivedDetail)

    # Add another log entry via update_and_log
    await coordinator.update_and_log(action.key, RejectedState(reason="no"), DeniedDetail(reason="no"))
    entry2 = await coordinator.get_log_entry("entry-sess", 2)
    assert entry2 is not None
    assert isinstance(entry2.detail, DeniedDetail)

    missing = await coordinator.get_log_entry("entry-sess", 99)
    assert missing is None


async def test_get_log_entries_since(coordinator: ActionCoordinator):
    call = ToolCall(server_namespace="test", tool_name="exec", arguments={})
    # Each create_action adds 1 log entry (ActionReceived)
    for i in range(5):
        await coordinator.create_action(
            session_key="since-sess", call=call, justification=f"j{i}", client_id=None, subject=None
        )

    entries = await coordinator.get_log_entries_since("since-sess", after_entry_id=3)
    assert len(entries) == 2
    assert entries[0].entry_id == 4
    assert entries[1].entry_id == 5

    all_entries = await coordinator.get_log_entries_since("since-sess", after_entry_id=0)
    assert len(all_entries) == 5

    none_entries = await coordinator.get_log_entries_since("since-sess", after_entry_id=5)
    assert len(none_entries) == 0


async def test_event_emitted_on_create(coordinator: ActionCoordinator):
    events: list[CoordinatorEvent] = []

    async def _collect(event: CoordinatorEvent) -> None:
        events.append(event)

    coordinator.add_listener(_collect)

    call = ToolCall(server_namespace="test", tool_name="exec", arguments={})
    action = await coordinator.create_action(
        session_key="evt-sess", call=call, justification="test", client_id=None, subject=None
    )

    assert len(events) == 1
    assert isinstance(events[0], ActionCreatedEvent)
    assert events[0].action.key == action.key


async def test_event_emitted_on_update(coordinator: ActionCoordinator):
    events: list[CoordinatorEvent] = []

    async def _collect(event: CoordinatorEvent) -> None:
        events.append(event)

    coordinator.add_listener(_collect)

    call = ToolCall(server_namespace="test", tool_name="exec", arguments={})
    action = await coordinator.create_action(
        session_key="evt-sess", call=call, justification="test", client_id=None, subject=None
    )
    events.clear()

    await coordinator.update_and_log(action.key, RejectedState(reason="no"), DeniedDetail(reason="no"))

    assert len(events) == 1
    assert isinstance(events[0], ActionUpdatedEvent)
    assert events[0].action.state.status == ActionStatus.REJECTED


if __name__ == "__main__":
    pytest_bazel.main()
