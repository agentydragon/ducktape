"""Bounded PostgreSQL notification chunks preserve exact terminal Dispatch snapshots."""

import asyncio
from uuid import uuid4

import pytest_bazel
from sqlalchemy.engine import make_url

from agentplane.proto import command_pb2, event_log_pb2, session_dispatcher_pb2
from agentplane.session_dispatcher.ended_dispatches import EndedDispatches, TerminalEvent
from agentplane.session_dispatcher.models import Reason
from agentplane.session_dispatcher.terminal_broadcast import CHANNEL, MAX_NOTIFY_BYTES, TerminalBroadcast, _payloads

# gazelle:include_dep @pypi//protobuf


def terminal_event() -> TerminalEvent:
    dispatch = session_dispatcher_pb2.Dispatch(
        command_id="large-command",
        session_id="session",
        sandbox="deleted-sandbox",
        sandbox_uid="sandbox-uid",
        state=session_dispatcher_pb2.DELIVERY_STATE_UNKNOWN,
        reason=Reason.SANDBOX_DELETED,
    )
    admission = event_log_pb2.EventEntry()
    admission.event.command_admitted.command.CopyFrom(
        command_pb2.Command(command_id="large-command", submit_input=command_pb2.SubmitInput(text="x" * 12000))
    )
    dispatch.admission.CopyFrom(admission)
    return TerminalEvent(str(uuid4()), dispatch)


async def test_terminal_dispatch_snapshots_are_bounded_and_reassembled_exactly() -> None:
    event = terminal_event()
    payloads = _payloads(event)
    assert len(payloads) > 1
    assert all(len(payload.encode("utf-8")) <= MAX_NOTIFY_BYTES for payload in payloads)

    ended = EndedDispatches()
    broadcast = TerminalBroadcast(make_url("postgresql://localhost/dispatcher"), ended)
    changed = asyncio.Event()
    with ended.subscribe(changed) as subscription:
        for payload in reversed(payloads):
            broadcast._notified(CHANNEL, payload)
        assert subscription.events == [event]
        assert subscription.events[0].dispatch == event.dispatch
        assert changed.is_set()


async def test_an_incomplete_terminal_snapshot_fails_active_streams() -> None:
    event = terminal_event()
    payloads = _payloads(event)
    assert len(payloads) > 1
    ended = EndedDispatches()
    broadcast = TerminalBroadcast(make_url("postgresql://localhost/dispatcher"), ended, assembly_timeout_s=0.01)
    changed = asyncio.Event()
    with ended.subscribe(changed) as subscription:
        broadcast._notified(CHANNEL, payloads[0])
        async with asyncio.timeout(1):
            await changed.wait()
        assert subscription.events == []
        assert subscription.failure is not None
        assert changed.is_set()


if __name__ == "__main__":
    pytest_bazel.main()
