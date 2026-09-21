"""Attachment ownership: graceful scope exit and deliberately abrupt cancellation."""

from __future__ import annotations

from typing import cast

import grpc
import pytest
import pytest_bazel

from agentplane.protocol import event_log_pb2
from agentplane.runner import protocol_pb2
from agentplane.runner.client import Attachment

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


class FakeAttachmentCall:
    def __init__(self) -> None:
        self.writes: list[protocol_pb2.ClientMessage] = []
        self.reads = 0
        self.cancelled = False
        self.events: list[protocol_pb2.ServerMessage] = []

    async def write(self, message: protocol_pb2.ClientMessage) -> None:
        self.writes.append(message)

    async def read(self) -> object:
        self.reads += 1
        return self.events.pop(0) if self.events else grpc.aio.EOF

    def cancel(self) -> bool:
        self.cancelled = True
        return True


def attachment(call: FakeAttachmentCall) -> Attachment:
    return Attachment(
        cast(grpc.aio.StreamStreamCall[protocol_pb2.ClientMessage, protocol_pb2.ServerMessage], call),
        protocol_pb2.Attached(session_id="test-session"),
    )


async def test_normal_context_exit_detaches_once_and_drains() -> None:
    call = FakeAttachmentCall()
    attached = attachment(call)

    async with attached:
        await attached.detach()

    assert call.writes == [protocol_pb2.ClientMessage(detach=protocol_pb2.Detach())]
    assert call.reads == 1
    assert not call.cancelled


async def test_exceptional_context_exit_cancels_without_hiding_the_caller_error() -> None:
    call = FakeAttachmentCall()
    attached = attachment(call)

    with pytest.raises(RuntimeError, match="keep the original failure"):
        async with attached:
            raise RuntimeError("keep the original failure")

    assert call.cancelled
    assert not call.writes
    assert call.reads == 0


async def test_cancel_remains_the_explicit_lost_connection_operation() -> None:
    call = FakeAttachmentCall()

    attachment(call).cancel()

    assert call.cancelled


async def test_cursor_is_independent_of_opt_in_history_capture() -> None:
    for capture in (False, True):
        call = FakeAttachmentCall()
        call.events = [
            protocol_pb2.ServerMessage(event_entry=event_log_pb2.EventEntry(cursor=cursor)) for cursor in range(41, 45)
        ]
        attached = Attachment(
            cast(grpc.aio.StreamStreamCall[protocol_pb2.ClientMessage, protocol_pb2.ServerMessage], call),
            protocol_pb2.Attached(session_id="test-session"),
            after_cursor=40,
            capture_history=capture,
        )
        assert attached.cursor == 40
        await attached.drain_until_end()
        assert attached.cursor == 44
        assert len(attached.seen) == (4 if capture else 0)
        attached.seen.clear()
        assert attached.cursor == 44


if __name__ == "__main__":
    pytest_bazel.main()
