"""What the console's own protocol client promises, over a scripted channel."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_bazel

from haku.console.x.claude_code.client import ClaudeCli, ClaudeCliError
from haku.runner.client import RecordedFrame
from haku.runner.protocol import HarnessFrame

# The gap between one frame's number and the next. Deliberately not 1: the real sink is a Postgres
# `Identity` column, which skips, so nothing may come to depend on adjacency.
_SEQ_STRIDE = 3


class CountingSink:
    """A `FrameSink` that only numbers, which is all this file needs one for.

    Recognising a replayed frame is `RolloutRecorder`'s job, covered against a real database in
    `haku/console/x/test_session_runtime.py`.
    """

    def __init__(self) -> None:
        self.numbered: list[tuple[int, HarnessFrame]] = []
        # What the runner said each received frame's number was, which the sink keeps rather than
        # mints — None where the frame came from nowhere that numbers.
        self.runner_seqs: list[int | None] = []
        self._next = 1

    def _number(self, frame: HarnessFrame) -> int:
        seq = self._next
        self._next += _SEQ_STRIDE
        self.numbered.append((seq, frame))
        return seq

    async def sent(self, frame: HarnessFrame) -> int:
        return self._number(frame)

    async def received(self, frame: HarnessFrame) -> RecordedFrame:
        self.runner_seqs.append(frame.seq)
        return RecordedFrame(fresh=True, frame_seq=self._number(frame))


class ScriptedChannel:
    """A `FrameChannel` whose far end is a list, plus whatever the test pushes later."""

    def __init__(self, *frames: dict[str, Any]):
        self.written: list[dict[str, Any]] = []
        self.closed = False
        self._inbound: asyncio.Queue[HarnessFrame | None] = asyncio.Queue()
        for frame in frames:
            self._inbound.put_nowait(HarnessFrame(frame=frame))

    def deliver(self, frame: dict[str, Any] | None, *, seq: int | None = None) -> None:
        self._inbound.put_nowait(None if frame is None else HarnessFrame(frame=frame, seq=seq))

    async def connect(self) -> None:
        pass

    async def write(self, frame: HarnessFrame) -> None:
        self.written.append(frame.frame)

    async def read_messages(self) -> AsyncIterator[HarnessFrame]:
        while (message := await self._inbound.get()) is not None:
            yield message

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
    cli = ClaudeCli(channel, CountingSink(), control_timeout=5)
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
    sink = CountingSink()
    cli = ClaudeCli(channel, sink, control_timeout=5)
    connecting = asyncio.create_task(cli.connect())
    await asyncio.sleep(0)
    channel.deliver(_answer(channel.written[0]))
    await connecting

    assistant = {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}}
    channel.deliver(assistant)
    channel.deliver({"type": "result", "is_error": False})
    frames = cli.frames()

    first, second = await anext(frames), await anext(frames)
    assert (first.envelope.frame, second.envelope.frame["type"]) == (assistant, "result")
    # Each carries the sink's number for it, which is what a reader addresses a frame by. The
    # control response the sink numbered before them is plumbing and reached nobody.
    assert [(first.frame_seq, first.envelope.frame), (second.frame_seq, second.envelope.frame)] == [
        (seq, frame.frame) for seq, frame in sink.numbered[-2:]
    ]
    await cli.aclose()


async def test_the_number_the_runner_put_on_a_frame_reaches_the_sink() -> None:
    """The sink's own number orders the log; the runner's is what a reconnect is computed from.

    Both channels, because the sequence is dense over everything the runner sent — a control
    response left unnumbered would read at the other end as a frame that went missing. A frame
    from a channel with no runner behind it carries None, which is not zero and not a guess.
    """
    channel = ScriptedChannel()
    sink = CountingSink()
    cli = ClaudeCli(channel, sink, control_timeout=5)
    connecting = asyncio.create_task(cli.connect())
    await asyncio.sleep(0)
    channel.deliver(_answer(channel.written[0]), seq=7)
    await connecting

    channel.deliver({"type": "result", "is_error": False}, seq=8)
    channel.deliver({"type": "stream_event", "event": {"type": "content_block_delta"}})
    frames = cli.frames()
    await anext(frames)
    await anext(frames)

    assert sink.runner_seqs == [7, 8, None]
    await cli.aclose()


async def test_a_written_frame_is_numbered_before_it_can_be_answered() -> None:
    """The log is the ordered record of the conversation, so a request has to precede its response
    in it. Two tasks number them — this one the write, the reader the answer — against a sink that
    serialises nothing, so numbering only once a frame is on the wire leaves that order to a race
    the peer can win.
    """

    class RecordingCosts(CountingSink):
        """A sink whose write side takes a turn of the loop, as a Postgres round trip does."""

        async def sent(self, frame: HarnessFrame) -> int:
            await asyncio.sleep(0)
            return self._number(frame)

    class AnsweringChannel(ScriptedChannel):
        """A peer that answers as the request lands, closing the round-trip margin this race would
        otherwise be decided by."""

        async def write(self, frame: HarnessFrame) -> None:
            await super().write(frame)
            self.deliver(_answer(self.written[-1]))

    channel, sink = AnsweringChannel(), RecordingCosts()
    cli = ClaudeCli(channel, sink, control_timeout=5)
    await cli.connect()

    numbered = {frame.frame["type"]: seq for seq, frame in sink.numbered}
    assert numbered["control_request"] < numbered["control_response"]
    await cli.aclose()


async def test_a_prompt_carries_the_id_its_lifecycle_will_be_reported_under() -> None:
    """Without a `uuid` the CLI reports no `command_lifecycle` for the prompt at all, and
    `interrupt`'s `cancel_queued` cannot reach it."""
    channel = ScriptedChannel()
    sink = CountingSink()
    cli = ClaudeCli(channel, sink, control_timeout=5)
    connecting = asyncio.create_task(cli.connect())
    await asyncio.sleep(0)
    channel.deliver(_answer(channel.written[0]))
    await connecting

    prompt = await cli.query("hello")

    assert isinstance(channel.written[-1]["uuid"], str)
    # The number the prompt reports is the sink's own for the frame just written, which is what
    # lets the console point the operator's message row at the frame it went out as.
    assert sink.numbered[-1] == (prompt.frame_seq, HarnessFrame(frame=channel.written[-1]))
    assert channel.written[-1]["message"] == {"role": "user", "content": "hello"}
    await cli.aclose()


async def test_an_abort_also_drops_the_prompts_queued_behind_the_turn() -> None:
    """A bare `interrupt` cancels the running turn and the CLI starts the next queued prompt,
    which is not what an operator saying "stop" means."""
    channel = ScriptedChannel()
    cli = ClaudeCli(channel, CountingSink(), control_timeout=5)
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
    cli = ClaudeCli(channel, CountingSink(), control_timeout=5)
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
    cli = ClaudeCli(channel, CountingSink(), control_timeout=5)
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
        async def read_messages(self) -> AsyncIterator[HarnessFrame]:
            await self._inbound.get()
            raise ConnectionResetError("socket went away")
            yield  # pragma: no cover - marks this an async generator

    channel = BreakingChannel()
    cli = ClaudeCli(channel, CountingSink(), control_timeout=0.2)
    connecting = asyncio.create_task(cli.connect())
    await asyncio.sleep(0)
    channel.deliver(_answer(channel.written[0]))
    with pytest.raises(ClaudeCliError, match="initialize"):
        await connecting

    await asyncio.wait_for(cli.wait_closed(), timeout=1)
    await cli.aclose()


async def test_an_unanswered_control_request_times_out_rather_than_waiting_forever() -> None:
    cli = ClaudeCli(ScriptedChannel(), CountingSink(), control_timeout=0.05)
    with pytest.raises(ClaudeCliError, match="initialize"):
        await cli.connect()
    await cli.aclose()


async def test_an_error_response_is_raised_to_the_caller() -> None:
    channel = ScriptedChannel()
    cli = ClaudeCli(channel, CountingSink(), control_timeout=5)
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
    cli = ClaudeCli(channel, CountingSink(), control_timeout=5)
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
