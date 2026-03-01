from __future__ import annotations

import uuid

import pytest_bazel
from mcp.types import CallToolResult, TextContent

from approval_gate.models import ActionStatus, DoneState, PendingState, RejectedState, ToolCall
from approval_gate.storage import ActionStorage

_ID1 = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ID2 = uuid.UUID("00000000-0000-0000-0000-000000000002")
_ID3 = uuid.UUID("00000000-0000-0000-0000-000000000003")
_ID4 = uuid.UUID("00000000-0000-0000-0000-000000000004")


async def test_create_and_get(storage: ActionStorage):
    call = ToolCall(server_namespace="test", tool_name="exec", arguments={"argv": ["echo", "hi"]})
    action = await storage.create(action_id=_ID1, call=call, justification="testing", session_key=None)
    assert action.id == _ID1
    assert isinstance(action.state, PendingState)

    fetched = await storage.get(_ID1)
    assert fetched is not None
    assert fetched.id == _ID1
    assert fetched.call.tool_name == "exec"
    assert fetched.justification == "testing"
    assert fetched.session_key is None


async def test_get_missing_returns_none(storage: ActionStorage):
    result = await storage.get(uuid.uuid4())
    assert result is None


async def test_update_state(storage: ActionStorage):
    call = ToolCall(server_namespace="test", tool_name="exec", arguments={})
    await storage.create(action_id=_ID2, call=call, justification="test", session_key="sk-1")
    updated = await storage.update_state(
        _ID2, DoneState(outcome=CallToolResult(content=[TextContent(type="text", text="ok")]))
    )
    assert updated is not None
    assert isinstance(updated.state, DoneState)
    assert isinstance(updated.state.outcome, CallToolResult)


async def test_list_actions_filter(storage: ActionStorage):
    call = ToolCall(server_namespace="test", tool_name="exec", arguments={})
    await storage.create(action_id=_ID3, call=call, justification="a", session_key=None)
    await storage.create(action_id=_ID4, call=call, justification="b", session_key=None)
    await storage.update_state(_ID4, RejectedState(reason="no"))

    pending = await storage.list_actions(ActionStatus.PENDING)
    ids = {a.id for a in pending}
    assert _ID3 in ids
    assert _ID4 not in ids

    rejected = await storage.list_actions(ActionStatus.REJECTED)
    assert any(a.id == _ID4 for a in rejected)


async def test_list_actions_all(storage: ActionStorage):
    call = ToolCall(server_namespace="test", tool_name="exec", arguments={})
    id1 = uuid.uuid4()
    id2 = uuid.uuid4()
    await storage.create(action_id=id1, call=call, justification="x", session_key=None)
    await storage.create(action_id=id2, call=call, justification="y", session_key=None)

    all_actions = await storage.list_actions(None)
    ids = {a.id for a in all_actions}
    assert id1 in ids
    assert id2 in ids


async def test_session_key_stored(storage: ActionStorage):
    call = ToolCall(server_namespace="test", tool_name="exec", arguments={})
    sk_id = uuid.uuid4()
    await storage.create(action_id=sk_id, call=call, justification="need session", session_key="my-session-key")
    fetched = await storage.get(sk_id)
    assert fetched is not None
    assert fetched.session_key == "my-session-key"


if __name__ == "__main__":
    pytest_bazel.main()
