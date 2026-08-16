"""What the console's own protocol client promises, over a scripted channel."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_bazel

from haku.runtime.x.bridge.cli_client import ClaudeCli, ClaudeCliError


class ScriptedChannel:
    """A `FrameChannel` whose far end is a list, plus whatever the test pushes later."""

    def __init__(self, *frames: dict[str, Any]):
        self.written: list[dict[str, Any]] = []
        self.closed = False
        self._inbound: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        for frame in frames:
            self._inbound.put_nowait(frame)

    def deliver(self, frame: dict[str, Any] | None) -> None:
        self._inbound.put_nowait(frame)

    async def connect(self) -> None:
        pass

    async def write(self, data: str) -> None:
        self.written.append(json.loads(data))

    async def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        while (frame := await self._inbound.get()) is not None:
            yield frame

    async def close(self) -> None:
        self.closed = True


def _answer(request: dict[str, Any], **response: Any) -> dict[str, Any]:
    """The response shape the CLI actually sends: the id is nested *inside* `response`."""
    return {
        "type": "control_response",
        "response": {"subtype": "success", "request_id": request["request_id"], **response},
    }


async def test_a_control_request_is_answered_by_its_own_response() -> None:
    channel = ScriptedChannel()
    cli = ClaudeCli(channel, control_timeout=5)
    connecting = asyncio.create_task(cli.connect())
    await asyncio.sleep(0)

    initialize = channel.written[0]
    assert initialize["request"]["subtype"] == "initialize"
    channel.deliver(_answer(initialize, response={"commands": []}))

    assert await connecting == {"commands": []}
    await cli.aclose()


async def test_conversation_frames_are_delivered_verbatim_and_control_is_not() -> None:
    """The record is the wire. A control response is plumbing and must not reach a reader."""
    channel = ScriptedChannel()
    cli = ClaudeCli(channel, control_timeout=5)
    connecting = asyncio.create_task(cli.connect())
    await asyncio.sleep(0)
    channel.deliver(_answer(channel.written[0]))
    await connecting

    assistant = {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}}
    channel.deliver(assistant)
    channel.deliver({"type": "result", "is_error": False})
    frames = cli.frames()

    assert (await anext(frames)).payload == assistant
    assert (await anext(frames)).payload["type"] == "result"
    await cli.aclose()


async def test_a_prompt_carries_the_id_its_lifecycle_will_be_reported_under() -> None:
    """Without a `uuid` the CLI reports no `command_lifecycle` for the prompt at all, and
    `interrupt`'s `cancel_queued` cannot reach it."""
    channel = ScriptedChannel()
    cli = ClaudeCli(channel, control_timeout=5)
    connecting = asyncio.create_task(cli.connect())
    await asyncio.sleep(0)
    channel.deliver(_answer(channel.written[0]))
    await connecting

    prompt = await cli.query("hello")

    assert channel.written[-1]["uuid"] == prompt.command_uuid
    # No sink, so nothing numbered the frame and the prompt says so rather than inventing one.
    assert prompt.frame_seq is None
    assert channel.written[-1]["message"] == {"role": "user", "content": "hello"}
    await cli.aclose()


async def test_an_abort_also_drops_the_prompts_queued_behind_the_turn() -> None:
    """A bare `interrupt` cancels the running turn and the CLI starts the next queued prompt,
    which is not what an operator saying "stop" means."""
    channel = ScriptedChannel()
    cli = ClaudeCli(channel, control_timeout=5)
    connecting = asyncio.create_task(cli.connect())
    await asyncio.sleep(0)
    channel.deliver(_answer(channel.written[0]))
    await connecting

    aborting = asyncio.create_task(cli.interrupt())
    await asyncio.sleep(0)
    channel.deliver(_answer(channel.written[-1]))
    await aborting

    assert channel.written[-1]["request"] == {"subtype": "interrupt", "reason": "user-cancel", "cancel_queued": True}
    await cli.aclose()


async def test_the_stream_ending_ends_the_frames_rather_than_hanging() -> None:
    """A consumer waiting for this turn's `result` has to learn the CLI is gone."""
    channel = ScriptedChannel()
    cli = ClaudeCli(channel, control_timeout=5)
    connecting = asyncio.create_task(cli.connect())
    await asyncio.sleep(0)
    channel.deliver(_answer(channel.written[0]))
    await connecting

    channel.deliver(None)

    assert [frame async for frame in cli.frames()] == []
    await cli.aclose()


async def test_wait_closed_resolves_when_the_stream_ends() -> None:
    """The reader is a detached task, so a lost socket can only be observed, not caught: an idle
    owner races `wait_closed` to learn the stream is gone rather than parking (console handler)."""
    channel = ScriptedChannel()
    cli = ClaudeCli(channel, control_timeout=5)
    connecting = asyncio.create_task(cli.connect())
    await asyncio.sleep(0)
    channel.deliver(_answer(channel.written[0]))
    await connecting

    channel.deliver(None)
    await asyncio.wait_for(cli.wait_closed(), timeout=1)
    await cli.aclose()


async def test_wait_closed_resolves_when_the_socket_breaks() -> None:
    """A broken transport is swallowed by the reader — logged, not re-raised, since a detached task
    cannot hand its failure back — so `wait_closed` is the signal that the stream is over."""

    class BreakingChannel(ScriptedChannel):
        async def read_messages(self) -> AsyncIterator[dict[str, Any]]:
            await self._inbound.get()
            raise ConnectionResetError("socket went away")
            yield  # pragma: no cover - marks this an async generator

    channel = BreakingChannel()
    cli = ClaudeCli(channel, control_timeout=0.2)
    connecting = asyncio.create_task(cli.connect())
    await asyncio.sleep(0)
    channel.deliver(_answer(channel.written[0]))
    with pytest.raises(ClaudeCliError, match="initialize"):
        await connecting

    await asyncio.wait_for(cli.wait_closed(), timeout=1)
    await cli.aclose()


async def test_an_unanswered_control_request_times_out_rather_than_waiting_forever() -> None:
    cli = ClaudeCli(ScriptedChannel(), control_timeout=0.05)
    with pytest.raises(ClaudeCliError, match="initialize"):
        await cli.connect()
    await cli.aclose()


async def test_an_error_response_is_raised_to_the_caller() -> None:
    channel = ScriptedChannel()
    cli = ClaudeCli(channel, control_timeout=5)
    connecting = asyncio.create_task(cli.connect())
    await asyncio.sleep(0)
    channel.deliver(
        {
            "type": "control_response",
            "response": {"subtype": "error", "request_id": channel.written[0]["request_id"], "error": "nope"},
        }
    )

    with pytest.raises(ClaudeCliError, match="nope"):
        await connecting
    await cli.aclose()


async def test_a_request_we_cannot_serve_is_refused_rather_than_left_hanging() -> None:
    """This client registers no hooks and no `can_use_tool`, so an inbound request is a bug —
    but an unanswered one blocks the CLI forever, which is a room that goes quiet for no
    recorded reason."""
    channel = ScriptedChannel()
    cli = ClaudeCli(channel, control_timeout=5)
    connecting = asyncio.create_task(cli.connect())
    await asyncio.sleep(0)
    channel.deliver(_answer(channel.written[0]))
    await connecting

    channel.deliver({"type": "control_request", "request_id": "cli_1", "request": {"subtype": "can_use_tool"}})
    for _ in range(10):
        await asyncio.sleep(0)
        if len(channel.written) > 1:
            break

    assert channel.written[-1] == {
        "type": "control_response",
        "response": {
            "subtype": "error",
            "request_id": "cli_1",
            "error": "can_use_tool is not supported by this client",
        },
    }
    await cli.aclose()


if __name__ == "__main__":
    pytest_bazel.main()
