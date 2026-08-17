"""A client for Claude Code's newline-delimited JSON protocol.

The console's session runtime speaks the protocol itself: nothing here imports the Agent SDK, and
`options.py` owns the launch arguments. The protocol is described in
<../../../cli_protocol/README.md>.

Two channels are multiplexed on one stream, distinguished by the top-level `type`:

- **Conversation** — `user` in, and `assistant` / `user` / `system` / `result` / `stream_event`
  and friends back. Append-only. Delivered here as `frames()`, verbatim dicts, because the
  console's record of a session is the wire and not a parse of it.
- **Control** — `control_request` / `control_response` correlated by `request_id`, carrying
  `initialize`, `interrupt` and the rest.

**The record is taken here**, by the `FrameSink` every client is built with, because this is where
each frame is already parsed and where both channels are still visible.

**Every frame the client hands on carries the sink's number for it**, which is what lets a reader
address one. The sink is therefore not optional: a client with nowhere to record is a client whose
frames cannot be pointed at, and the console's projection has to point at them.

**The conversation queue must stay unbounded.** The reader that routes control responses is the
same task that fills it, so a bounded queue plus a consumer that stops draining stalls the reader
and takes `interrupt` down with it — the one call an operator makes when a turn has gone wrong.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel

from haku.cli_protocol.frames import (
    ControlRequestFrame,
    ControlResponse,
    ControlSubtype,
    InitializeRequest,
    InterruptRequest,
)
from haku.runtime.x.bridge.protocol import ClaudeLaunch, ClaudeMessage, TextWebSocket
from haku.runtime.x.bridge.transport import ProgressSink, WebSocketTransport

logger = logging.getLogger(__name__)

# Long enough for `initialize`, which waits on the CLI's MCP servers to come up.
CONTROL_TIMEOUT_SECONDS = 60.0


class FrameChannel(Protocol):
    """A bidirectional line channel to a CLI process, however it is reached.

    Narrow on purpose: in production the process is in a sandbox pod at the far end of the
    runner's websocket, and in a test it is a scripted double. The client should not be able to
    tell, which is also what makes it exercisable against a real CLI without standing up the
    bridge.

    **A read yields the envelope, not the payload alone**, because the runner puts its own number
    on each frame (`ClaudeMessage.seq`) and that number has to reach the sink — it is the log's
    ordering and the cursor a reconnect hands back
    (<../../../plans/chat_runtime_projection.md> § 2b). A channel with no runner behind it — a
    scripted double, a local subprocess — leaves `seq` None, which is the honest answer: nobody
    numbered those frames.
    """

    async def connect(self) -> None: ...

    async def write(self, data: str) -> None: ...

    def read_messages(self) -> AsyncIterator[ClaudeMessage]: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class RecordedFrame:
    """Where a sink put one frame, and whether the caller should act on it.

    *fresh* is False when the sink has seen this exact frame before and the reader must not route
    it a second time; a sink with no notion of that reports True for everything. *frame_seq* is
    the sink's own number for the frame either way, since a replay names a row that exists.
    """

    fresh: bool
    frame_seq: int


class FrameSink(Protocol):
    """Where a session's frames are kept, and what numbers them.

    Two methods rather than one plus a direction enum, because the only direction vocabulary
    that exists lives in the console's own schema and this package must not depend on it.

    **The numbers it returns are an order, not a count.** The console's sink is a Postgres
    `Identity` column, which leaves gaps — so a reader may compare two of them and may not read a
    gap between them as a frame that went missing.

    *runner_seq* is the other number: the one the **peer** minted for a frame it sent, dense over
    everything that runner put on the wire, and None for a frame no runner numbered — this end's
    writes, and anything from a runner image predating the field. It is recorded beside the sink's
    own number rather than replacing it; what reads it is the resume cursor
    (<../../../plans/chat_runtime_projection.md> § 2b).

    Called from the client's write path and from its reader, so an implementation that raises
    takes the session down — which is the intent where the sink is the rollout: a record with
    quiet holes is the one that looks complete while being wrong.
    """

    async def sent(self, payload: dict[str, Any]) -> int: ...

    async def received(self, payload: dict[str, Any], *, runner_seq: int | None) -> RecordedFrame: ...


@dataclass(frozen=True, slots=True)
class SentPrompt:
    """One written prompt: the id its lifecycle is reported under, and where it was recorded."""

    command_uuid: str
    frame_seq: int


@dataclass(frozen=True, slots=True)
class ReceivedFrame:
    """One conversation frame, with the sink's number for it.

    **The number travels with the frame rather than being read back off the client**, because the
    reader is a detached task that runs ahead of whoever consumes `frames()`: a cursor on the
    client answers "the newest frame recorded", not "the frame you are holding", and the two
    differ for the whole of any burst — which is what a streamed answer is.
    """

    payload: dict[str, Any]
    frame_seq: int


class ClaudeCliError(Exception):
    """The CLI answered a control request with an error, or never answered it."""


class ClaudeCli:
    """One CLI process, addressed over an already-authenticated transport."""

    def __init__(
        self, channel: FrameChannel, frames_to: FrameSink, *, control_timeout: float = CONTROL_TIMEOUT_SECONDS
    ):
        self._channel = channel
        self._control_timeout = control_timeout
        self._frames_to = frames_to
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._conversation: asyncio.Queue[ReceivedFrame | None] = asyncio.Queue()
        self._reader: asyncio.Task[None] | None = None
        self._closed = asyncio.Event()

    async def connect(self) -> dict[str, Any]:
        """Launch the CLI, start reading, and complete the `initialize` handshake."""
        await self._channel.connect()
        self._reader = asyncio.create_task(self._read())
        return await self.control(InitializeRequest())

    async def query(self, text: str) -> SentPrompt:
        """Send one user message, returning the id its lifecycle will be reported under.

        Deliberately writable at any time, including while a turn is running: the CLI folds a
        prompt that arrives mid-turn into that turn at the next tool boundary
        (<../../../cli_protocol/probes/steering.py>).

        **The `uuid` is what turns `command_lifecycle` on**, and the SDK never sent one. The CLI
        reports `queued` → `started` → `completed`/`cancelled` for a prompt only when the
        inbound frame carries one, which is the only **confirmation** of a fold rather than an
        inference from behaviour: a command that starts a fresh turn reports `completed` after
        that turn's `result`, and one folded into a running turn reports it before. It is also
        what makes the prompt reachable by `interrupt`'s `cancel_queued`.
        """
        command_uuid = str(uuid.uuid4())
        return SentPrompt(
            command_uuid=command_uuid,
            frame_seq=await self._write(
                {
                    "type": "user",
                    "message": {"role": "user", "content": text},
                    "parent_tool_use_id": None,
                    "uuid": command_uuid,
                }
            ),
        )

    async def interrupt(self) -> None:
        """Abort the running turn **and** anything queued behind it.

        `cancel_queued` is not optional here because the console has one abort, and an operator
        saying "stop" does not mean "stop this and start the next thing I typed" — which is what
        a bare interrupt does: the CLI begins the next queued prompt the moment this one dies
        (<../../../cli_protocol/probes/steering.py>). Nothing is queued today, since the console
        writes one prompt per turn; this is what keeps that true once folding lands.
        """
        await self.control(InterruptRequest(reason="user-cancel", cancel_queued=True))

    async def control(self, request: BaseModel) -> dict[str, Any]:
        """One control request, awaited until the CLI answers it or the timeout passes."""
        frame = ControlRequestFrame(request=request.model_dump(exclude_none=True))
        pending: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[frame.request_id] = pending
        try:
            await self._write(frame.model_dump())
            return await asyncio.wait_for(pending, timeout=self._control_timeout)
        except TimeoutError as error:
            raise ClaudeCliError(f"control request {frame.request['subtype']} was never answered") from error
        finally:
            self._pending.pop(frame.request_id, None)

    async def frames(self) -> AsyncIterator[ReceivedFrame]:
        """Every conversation frame, in order, until the CLI's stream ends.

        Session-scoped rather than request-scoped: one `result` ends a turn, not the stream, so
        a turn is a bracket a caller draws over this rather than a call it makes.
        """
        while (received := await self._conversation.get()) is not None:
            yield received

    async def wait_closed(self) -> None:
        """Resolve once the reader has ended — the CLI's stream stopped, cleanly or on a broken
        socket. The reader is a detached task, so an exception in it cannot propagate to whoever
        owns this client; this is how a lost connection becomes observable to them instead of
        being swallowed. A caller races this against its idle wait so a dropped socket is handed
        back at once rather than after a graceful-shutdown timeout (see console `handle_runner`).
        """
        await self._closed.wait()

    async def aclose(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None
        for pending in self._pending.values():
            if not pending.done():
                pending.set_exception(ClaudeCliError("the CLI connection closed"))
        self._pending.clear()
        await self._channel.close()

    async def _write(self, payload: dict[str, Any]) -> int:
        """Record the frame, then put it on the wire — in that order, deliberately.

        A written frame is answerable at once, and its answer is numbered by `_read`, a separate
        task calling the same sink, which serialises nothing. Numbering after the write would
        therefore leave "a request precedes its response in the log" to a race between this end's
        sink — a Postgres `Identity` taken at the INSERT — and the peer's turnaround.

        The cost is which way an interrupted write falls: this end can now hold a frame it never
        sent. `session_store._prompt_left` reads that record as evidence the prompt was delivered,
        so it lands there as a turn never re-asked rather than one asked twice, which is the
        direction that function already prefers.
        """
        frame_seq = await self._frames_to.sent(payload)
        await self._channel.write(json.dumps(payload) + "\n")
        return frame_seq

    async def _read(self) -> None:
        """Route the stream: control responses to their waiter, everything else to the queue."""
        # Adoption re-sends the whole rollout for dedup, so a skip is one burst of dozens at
        # connect, not a per-frame event worth a line each. Count the burst and log it once when
        # it ends (the next real frame, or the stream doing so).
        skipped = 0
        try:
            async for message in self._channel.read_messages():
                frame = message.payload
                # Before the routing, deliberately: control frames never reach `frames()`, so a
                # recorder hung off the conversation queue would silently drop the control
                # channel from the record — invisible until someone tried to debug an interrupt.
                # The record is also what recognises a replay: an adopted connection re-sends
                # whatever the previous console may not have acknowledged, and a frame already in
                # the log must not be routed again — a second `assistant` would post the same
                # answer into the room twice.
                recorded = await self._frames_to.received(frame, runner_seq=message.seq)
                if not recorded.fresh:
                    skipped += 1
                    continue
                if skipped:
                    logger.info("Skipped %d replayed frame(s) already in the rollout", skipped)
                    skipped = 0
                match frame.get("type"):
                    case "control_response":
                        self._resolve(frame)
                    case "control_cancel_request":
                        logger.info("Claude CLI cancelled control request %s", frame.get("request_id"))
                    case "control_request":
                        await self._refuse(frame)
                    case _:
                        self._conversation.put_nowait(ReceivedFrame(payload=frame, frame_seq=recorded.frame_seq))
        except asyncio.CancelledError:
            raise
        except Exception:
            # Logged, not re-raised: a detached task cannot hand its failure to the owner, so the
            # useful signal is that the stream is *over*, delivered below to both the queue (for a
            # mid-turn consumer) and `wait_closed` (for an idle one). What broke it is in the log.
            logger.exception("Claude CLI stream failed")
        finally:
            if skipped:
                logger.info("Skipped %d replayed frame(s) already in the rollout", skipped)
            # A sentinel rather than closing the queue: a consumer mid-turn has to learn the
            # stream ended, and would otherwise wait for a `result` that cannot arrive.
            self._conversation.put_nowait(None)
            self._closed.set()

    def _resolve(self, frame: dict[str, Any]) -> None:
        response = ControlResponse.model_validate(frame["response"])
        pending = self._pending.get(response.request_id)
        if pending is None or pending.done():
            # No local waiter. Routine on an adopted connection: the runner replays control
            # responses to requests a *previous* console sent, which this one never had pending.
            # A response we genuinely awaited and lost surfaces as a control-request timeout
            # instead, so this is not the place that reports a broken control channel.
            logger.debug("Ignoring a control response with no local waiter: %s", response.request_id)
            return
        if response.subtype is ControlSubtype.ERROR:
            pending.set_exception(ClaudeCliError(response.error or "control request failed"))
            return
        pending.set_result(response.response or {})

    async def _refuse(self, frame: dict[str, Any]) -> None:
        """Answer an inbound control request we cannot serve, rather than leaving it hanging.

        This session registers no hooks, no `can_use_tool` and no SDK-hosted MCP server, so
        there is nothing the CLI should be asking us — but an unanswered request blocks it
        forever (measured: <../../../cli_protocol/probes/harness.py>), and a wedged CLI is a room
        that goes quiet with no reason recorded.
        """
        subtype = (frame.get("request") or {}).get("subtype")
        logger.error("Claude CLI asked for %s, which this client does not serve", subtype)
        refusal = ControlResponse(
            subtype=ControlSubtype.ERROR,
            request_id=frame["request_id"],
            error=f"{subtype} is not supported by this client",
        )
        await self._write({"type": "control_response", "response": refusal.model_dump(exclude_none=True)})


def cli_over_websocket(
    websocket: TextWebSocket, launch: ClaudeLaunch, on_progress: ProgressSink | None, frames_to: FrameSink
) -> ClaudeCli:
    """The console's composition: a CLI client over the runner's bridge socket."""
    return ClaudeCli(WebSocketTransport(websocket, launch, on_progress), frames_to)
