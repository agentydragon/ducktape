"""Tests for the Console-side WebSocket-backed Agent SDK transport."""

from __future__ import annotations

import anyio
import pytest
import pytest_bazel

from haku.runtime.x.agent_sdk_transport.protocol import END_INPUT_FRAME, ClaudeLaunch, encode_object
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


def test_launch_frame_round_trips_and_rejects_unknown_versions() -> None:
    launch = ClaudeLaunch(arguments=("--verbose",), cwd="/workspace", environment={"SAFE": "value"})

    assert ClaudeLaunch.from_frame(launch.to_frame()) == launch

    unsupported = {**launch.to_frame(), "protocol_version": 2}
    with pytest.raises(ValueError, match="protocol version"):
        ClaudeLaunch.from_frame(unsupported)


async def test_transport_preserves_fine_grained_tool_input_stream_events() -> None:
    console_socket, runner_socket = memory_websocket_pair()
    launch = ClaudeLaunch(arguments=("--verbose",), cwd="/workspace", environment={"SAFE": "value"})
    transport = WebSocketTransport(console_socket, launch)
    await transport.connect()
    assert await runner_socket.receive_text() == encode_object(launch.to_frame())

    prompt = {"type": "user", "message": {"role": "user", "content": "search"}}
    await transport.write(encode_object(prompt) + "\n")
    assert await runner_socket.receive_text() == encode_object(prompt)

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
        await runner_socket.send_text(encode_object(event))
        with anyio.fail_after(1):
            assert await anext(messages) == event

    await transport.end_input()
    assert await runner_socket.receive_text() == encode_object(END_INPUT_FRAME)
    await transport.close()

    assert not transport.is_ready()
    assert console_socket.closed


async def test_transport_rejects_non_object_frames() -> None:
    console_socket, runner_socket = memory_websocket_pair()
    launch = ClaudeLaunch(arguments=(), cwd="/workspace", environment={})
    transport = WebSocketTransport(console_socket, launch)
    await transport.connect()
    assert await runner_socket.receive_text() == encode_object(launch.to_frame())
    await runner_socket.send_text("[]")

    with pytest.raises(ValueError, match="one JSON object"):
        await anext(transport.read_messages())

    await transport.close()


if __name__ == "__main__":
    pytest_bazel.main()
