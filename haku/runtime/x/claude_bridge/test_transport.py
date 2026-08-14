"""Tests for the Console-side WebSocket-backed Agent SDK transport."""

from __future__ import annotations

import json

import anyio
import pytest
import pytest_bazel
from pydantic import ValidationError

from haku.runtime.x.claude_bridge.protocol import (
    CONSOLE_TO_RUNNER,
    RUNNER_TO_CONSOLE,
    ClaudeLaunch,
    ClaudeMessage,
    EndInput,
    SetupOutput,
    encode_object,
)
from haku.runtime.x.claude_bridge.transport import WebSocketTransport


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
    message = ClaudeMessage(payload={"type": "user", "message": {"role": "user", "content": "hi"}})

    for outbound in (ClaudeLaunch(arguments=("-v",), cwd="/workspace", environment={"SAFE": "v"}), message, EndInput()):
        assert CONSOLE_TO_RUNNER.validate_json(outbound.model_dump_json()) == outbound
    for inbound in (message, SetupOutput(data=b"Cloning into 'haku-state'...\n")):
        assert RUNNER_TO_CONSOLE.validate_json(inbound.model_dump_json()) == inbound


def test_each_direction_refuses_the_other_direction_only_frames() -> None:
    """The direction is a property of the type, not a check each reader has to remember."""
    for wrong_way in (ClaudeLaunch(arguments=(), cwd="/workspace", environment={}), EndInput()):
        with pytest.raises(ValidationError, match="union_tag_invalid"):
            RUNNER_TO_CONSOLE.validate_json(wrong_way.model_dump_json())

    with pytest.raises(ValidationError, match="union_tag_invalid"):
        CONSOLE_TO_RUNNER.validate_json(SetupOutput(data=b"not the console's to send").model_dump_json())


def test_an_sdk_payload_naming_our_control_frames_is_still_a_conversation_frame() -> None:
    """The whole point of the envelope: the SDK's vocabulary cannot collide with ours."""
    impostor = ClaudeMessage(payload={"kind": "end_input", "type": "haku_transport", "subtype": "start"})

    assert RUNNER_TO_CONSOLE.validate_json(impostor.model_dump_json()) == impostor


def test_launch_rejects_another_protocol_version() -> None:
    launch = ClaudeLaunch(arguments=("--verbose",), cwd="/workspace", environment={})
    older = {**json.loads(launch.model_dump_json()), "protocol_version": 1}

    with pytest.raises(ValidationError, match="protocol_version"):
        CONSOLE_TO_RUNNER.validate_json(encode_object(older))


def test_an_unknown_frame_kind_is_refused() -> None:
    """A kind from a newer peer is an error, not something to route somewhere plausible.

    The other half of the same decision as the unknown-*field* test below: a peer that cannot name
    the frame cannot act on it, so must-understand changes arrive as kinds and fail closed here,
    while optional additions arrive as fields and are ignored.
    """
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        CONSOLE_TO_RUNNER.validate_json(encode_object({"kind": "a-kind-from-the-future"}))


def test_a_frame_missing_its_kind_is_refused() -> None:
    """What a pre-envelope peer sends: no discriminator at all, rather than a wrong one."""
    with pytest.raises(ValidationError, match="union_tag_not_found"):
        CONSOLE_TO_RUNNER.validate_json(encode_object({"type": "haku_transport", "subtype": "end_input"}))


def test_a_frame_carrying_an_unknown_field_is_read_without_it() -> None:
    """An optional addition from a newer peer. Ignoring it leaves this end behaving as its own
    version correctly did, which is the whole point of adding one.

    This used to be `extra=forbid` and assert the opposite. That made every additive field a
    fleet-wide break: a live session's runner keeps its image for hours, so the release that added
    a field killed every session still on the previous one.
    """
    frame = RUNNER_TO_CONSOLE.validate_json(
        encode_object({"kind": "setup_output", "data": "aGk=", "severity": "warning"})
    )

    assert frame == SetupOutput(data=b"hi")


def test_a_frame_missing_a_required_field_is_refused() -> None:
    with pytest.raises(ValidationError, match="data"):
        RUNNER_TO_CONSOLE.validate_json(encode_object({"kind": "setup_output"}))


async def test_transport_preserves_fine_grained_tool_input_stream_events() -> None:
    console_socket, runner_socket = memory_websocket_pair()
    launch = ClaudeLaunch(arguments=("--verbose",), cwd="/workspace", environment={"SAFE": "value"})
    transport = WebSocketTransport(console_socket, launch)
    await transport.connect()
    assert CONSOLE_TO_RUNNER.validate_json(await runner_socket.receive_text()) == launch

    prompt = {"type": "user", "message": {"role": "user", "content": "search"}}
    await transport.write(encode_object(prompt) + "\n")
    assert CONSOLE_TO_RUNNER.validate_json(await runner_socket.receive_text()) == ClaudeMessage(payload=prompt)

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
        await runner_socket.send_text(ClaudeMessage(payload=event).model_dump_json())
        with anyio.fail_after(1):
            assert await anext(messages) == event

    await transport.end_input()
    assert CONSOLE_TO_RUNNER.validate_json(await runner_socket.receive_text()) == EndInput()
    await transport.close()

    assert not transport.is_ready()
    assert console_socket.closed


async def test_transport_rejects_non_object_frames() -> None:
    console_socket, runner_socket = memory_websocket_pair()
    launch = ClaudeLaunch(arguments=(), cwd="/workspace", environment={})
    transport = WebSocketTransport(console_socket, launch)
    await transport.connect()
    assert CONSOLE_TO_RUNNER.validate_json(await runner_socket.receive_text()) == launch
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
    assert CONSOLE_TO_RUNNER.validate_json(await runner_socket.receive_text())

    messages = transport.read_messages()
    await runner_socket.send_text(SetupOutput(data=b"Cloning into '/workspace/haku-state'...\n").model_dump_json())
    answer = {"type": "assistant", "message": {"role": "assistant", "content": "hi"}}
    await runner_socket.send_text(ClaudeMessage(payload=answer).model_dump_json())

    with anyio.fail_after(1):
        # The progress frame is consumed on the way to this, not yielded before it.
        assert await anext(messages) == answer
    assert reported == ["Cloning into '/workspace/haku-state'..."]

    await transport.close()


async def test_setup_output_is_reassembled_across_chunks() -> None:
    """The runner ships bytes as they arrive; a line can span chunks and a chunk hold several."""
    console_socket, runner_socket = memory_websocket_pair()
    reported: list[str] = []

    async def on_progress(line: str) -> None:
        reported.append(line)

    transport = WebSocketTransport(
        console_socket, ClaudeLaunch(arguments=(), cwd="/workspace", environment={}), on_progress
    )
    await transport.connect()
    assert CONSOLE_TO_RUNNER.validate_json(await runner_socket.receive_text())

    messages = transport.read_messages()
    # A line split mid-word, two lines in one chunk, a blank line, and — the case the raw bytes
    # are for — a multi-byte character split across the chunk boundary.
    for chunk in (b"Clon", b"ing into 'haku-state'...\nresolving \xc3", b"\xa9tape\n\nworkspace ready\n"):
        await runner_socket.send_text(SetupOutput(data=chunk).model_dump_json())
    answer = {"type": "assistant", "message": {"role": "assistant", "content": "hi"}}
    await runner_socket.send_text(ClaudeMessage(payload=answer).model_dump_json())

    with anyio.fail_after(1):
        assert await anext(messages) == answer
    assert reported == ["Cloning into 'haku-state'...", "resolving étape", "workspace ready"]

    await transport.close()


async def test_progress_with_nowhere_to_go_is_dropped_not_fatal() -> None:
    console_socket, runner_socket = memory_websocket_pair()
    transport = WebSocketTransport(console_socket, ClaudeLaunch(arguments=(), cwd="/workspace", environment={}))
    await transport.connect()
    assert CONSOLE_TO_RUNNER.validate_json(await runner_socket.receive_text())

    messages = transport.read_messages()
    await runner_socket.send_text(SetupOutput(data=b"ignored\n").model_dump_json())
    answer = {"type": "assistant", "message": {"role": "assistant", "content": "hi"}}
    await runner_socket.send_text(ClaudeMessage(payload=answer).model_dump_json())

    with anyio.fail_after(1):
        assert await anext(messages) == answer

    await transport.close()


async def test_a_failing_progress_sink_does_not_end_the_conversation() -> None:
    """Narration is not worth a session.

    The sink posts into a Matrix room, which rate-limits; this is awaited inside the read loop,
    so an unguarded raise here ended the conversation and recorded the room's error over
    whatever the narration was reporting.
    """
    console_socket, runner_socket = memory_websocket_pair()

    async def on_progress(line: str) -> None:
        raise RuntimeError(f"the room said no to {line!r}")

    transport = WebSocketTransport(
        console_socket, ClaudeLaunch(arguments=(), cwd="/workspace", environment={}), on_progress
    )
    await transport.connect()
    assert CONSOLE_TO_RUNNER.validate_json(await runner_socket.receive_text())

    messages = transport.read_messages()
    await runner_socket.send_text(SetupOutput(data=b"Cloning into '/workspace/haku-state'...\n").model_dump_json())
    answer = {"type": "assistant", "message": {"role": "assistant", "content": "hi"}}
    await runner_socket.send_text(ClaudeMessage(payload=answer).model_dump_json())

    with anyio.fail_after(1):
        assert await anext(messages) == answer

    await transport.close()


async def test_transport_refuses_a_control_frame_from_the_runner() -> None:
    """`end_input` only travels console -> runner; one coming back is refused at the read.

    The unit-level version of this is `test_each_direction_refuses_the_other_direction_only
    _frames`; this one is here because the property has to hold where it is actually load
    bearing — a wrong-direction frame must not reach the SDK's message iterator.
    """
    console_socket, runner_socket = memory_websocket_pair()
    transport = WebSocketTransport(console_socket, ClaudeLaunch(arguments=(), cwd="/workspace", environment={}))
    await transport.connect()
    assert CONSOLE_TO_RUNNER.validate_json(await runner_socket.receive_text())
    await runner_socket.send_text(EndInput().model_dump_json())

    with pytest.raises(ValidationError, match="union_tag_invalid"):
        await anext(transport.read_messages())

    await transport.close()


if __name__ == "__main__":
    pytest_bazel.main()
