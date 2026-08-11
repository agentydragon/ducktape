"""Tests for the Console-side WebSocket-backed Agent SDK transport."""

from __future__ import annotations

import json

import anyio
import pytest
import pytest_bazel
from pydantic import ValidationError

from haku.runtime.x.agent_sdk_transport.protocol import (
    ClaudeLaunch,
    ClaudeMessage,
    EndInput,
    Progress,
    decode_frame,
    encode_frame,
    encode_object,
)
from haku.runtime.x.agent_sdk_transport.transport import WebSocketTransport


class MemoryWebSocket:
    def __init__(self, *, incoming: anyio.abc.ObjectReceiveStream[str], outgoing: anyio.abc.ObjectSendStream[str]):
        self._incoming = incoming
        self._outgoing = outgoing
        self.closed = False

    async def send_text(self, data: str) -> None:
        await self._outgoing.send(data)

    async def receive_text(self) -> str:
        try:
            return await self._incoming.receive()
        except anyio.EndOfStream as error:
            raise EOFError from error

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self._outgoing.aclose()
        await self._incoming.aclose()


def memory_websocket_pair() -> tuple[MemoryWebSocket, MemoryWebSocket]:
    left_to_right_send, left_to_right_receive = anyio.create_memory_object_stream[str](16)
    right_to_left_send, right_to_left_receive = anyio.create_memory_object_stream[str](16)
    return (
        MemoryWebSocket(incoming=right_to_left_receive, outgoing=left_to_right_send),
        MemoryWebSocket(incoming=left_to_right_receive, outgoing=right_to_left_send),
    )


def test_every_frame_kind_round_trips() -> None:
    launch = ClaudeLaunch(arguments=("--verbose",), cwd="/workspace", environment={"SAFE": "value"})
    message = ClaudeMessage(payload={"type": "user", "message": {"role": "user", "content": "hi"}})

    for frame in (launch, message, EndInput()):
        assert decode_frame(encode_frame(frame)) == frame


def test_an_sdk_payload_naming_our_control_frames_is_still_a_conversation_frame() -> None:
    """The whole point of the envelope: the SDK's vocabulary cannot collide with ours."""
    impostor = ClaudeMessage(payload={"kind": "end_input", "type": "haku_transport", "subtype": "start"})

    assert decode_frame(encode_frame(impostor)) == impostor


def test_launch_rejects_another_protocol_version() -> None:
    launch = ClaudeLaunch(arguments=("--verbose",), cwd="/workspace", environment={})
    older = {**json.loads(encode_frame(launch)), "protocol_version": 1}

    with pytest.raises(ValidationError, match="protocol_version"):
        decode_frame(encode_object(older))


def test_an_unknown_frame_kind_is_refused() -> None:
    """A kind from a newer peer is an error, not something to route somewhere plausible."""
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        decode_frame(encode_object({"kind": "a-kind-from-the-future"}))


def test_a_frame_missing_its_kind_is_refused() -> None:
    """What a pre-envelope peer sends: no discriminator at all, rather than a wrong one."""
    with pytest.raises(ValidationError, match="union_tag_not_found"):
        decode_frame(encode_object({"type": "haku_transport", "subtype": "end_input"}))


def test_a_frame_carrying_an_unknown_field_is_refused() -> None:
    """`extra=forbid`: a field this end does not understand is a version mismatch, not noise."""
    with pytest.raises(ValidationError, match="extra_forbidden"):
        decode_frame(encode_object({"kind": "progress", "line": "hi", "severity": "warning"}))


def test_a_frame_missing_a_required_field_is_refused() -> None:
    with pytest.raises(ValidationError, match="line"):
        decode_frame(encode_object({"kind": "progress"}))


async def test_transport_preserves_fine_grained_tool_input_stream_events() -> None:
    console_socket, runner_socket = memory_websocket_pair()
    launch = ClaudeLaunch(arguments=("--verbose",), cwd="/workspace", environment={"SAFE": "value"})
    transport = WebSocketTransport(console_socket, launch)
    await transport.connect()
    assert decode_frame(await runner_socket.receive_text()) == launch

    prompt = {"type": "user", "message": {"role": "user", "content": "search"}}
    await transport.write(encode_object(prompt) + "\n")
    assert decode_frame(await runner_socket.receive_text()) == ClaudeMessage(payload=prompt)

    partial_events = [
        {
            "type": "stream_event",
            "uuid": "message-1",
            "session_id": "session-1",
            "event": {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "tool_use", "id": "tool-1", "name": "search", "input": {}},
            },
        },
        {
            "type": "stream_event",
            "uuid": "message-1",
            "session_id": "session-1",
            "event": {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '{"query":'},
            },
        },
        {
            "type": "stream_event",
            "uuid": "message-1",
            "session_id": "session-1",
            "event": {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '"otters"}'},
            },
        },
    ]

    messages = transport.read_messages()
    for event in partial_events:
        await runner_socket.send_text(encode_frame(ClaudeMessage(payload=event)))
        with anyio.fail_after(1):
            assert await anext(messages) == event

    await transport.end_input()
    assert decode_frame(await runner_socket.receive_text()) == EndInput()
    await transport.close()

    assert not transport.is_ready()
    assert console_socket.closed


async def test_transport_rejects_non_object_frames() -> None:
    console_socket, runner_socket = memory_websocket_pair()
    launch = ClaudeLaunch(arguments=(), cwd="/workspace", environment={})
    transport = WebSocketTransport(console_socket, launch)
    await transport.connect()
    assert decode_frame(await runner_socket.receive_text()) == launch
    await runner_socket.send_text("[]")

    with pytest.raises(ValidationError, match="dict_type"):
        await anext(transport.read_messages())

    await transport.close()


async def test_progress_reaches_the_sink_and_not_the_conversation() -> None:
    """Sandbox narration must not reach the SDK, which would see an unknown message shape."""
    console_socket, runner_socket = memory_websocket_pair()
    reported: list[str] = []

    async def on_progress(line: str) -> None:
        reported.append(line)

    transport = WebSocketTransport(
        console_socket, ClaudeLaunch(arguments=(), cwd="/workspace", environment={}), on_progress
    )
    await transport.connect()
    assert decode_frame(await runner_socket.receive_text())

    messages = transport.read_messages()
    await runner_socket.send_text(encode_frame(Progress(line="Cloning into '/workspace/haku-state'...")))
    answer = {"type": "assistant", "message": {"role": "assistant", "content": "hi"}}
    await runner_socket.send_text(encode_frame(ClaudeMessage(payload=answer)))

    with anyio.fail_after(1):
        # The progress frame is consumed on the way to this, not yielded before it.
        assert await anext(messages) == answer
    assert reported == ["Cloning into '/workspace/haku-state'..."]

    await transport.close()


async def test_progress_with_nowhere_to_go_is_dropped_not_fatal() -> None:
    console_socket, runner_socket = memory_websocket_pair()
    transport = WebSocketTransport(console_socket, ClaudeLaunch(arguments=(), cwd="/workspace", environment={}))
    await transport.connect()
    assert decode_frame(await runner_socket.receive_text())

    messages = transport.read_messages()
    await runner_socket.send_text(encode_frame(Progress(line="ignored")))
    answer = {"type": "assistant", "message": {"role": "assistant", "content": "hi"}}
    await runner_socket.send_text(encode_frame(ClaudeMessage(payload=answer)))

    with anyio.fail_after(1):
        assert await anext(messages) == answer

    await transport.close()


async def test_transport_refuses_a_control_frame_from_the_runner() -> None:
    """`start` and `end_input` only travel console -> runner; one coming back is a bug."""
    console_socket, runner_socket = memory_websocket_pair()
    transport = WebSocketTransport(console_socket, ClaudeLaunch(arguments=(), cwd="/workspace", environment={}))
    await transport.connect()
    assert decode_frame(await runner_socket.receive_text())
    await runner_socket.send_text(encode_frame(EndInput()))

    with pytest.raises(ValueError, match="not a conversation frame"):
        await anext(transport.read_messages())

    await transport.close()


if __name__ == "__main__":
    pytest_bazel.main()
