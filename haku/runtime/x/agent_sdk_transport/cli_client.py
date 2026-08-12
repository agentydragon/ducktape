"""A client for Claude Code's newline-delimited JSON protocol.

Replaces `ClaudeSDKClient` for the console's session runtime. The reasoning is in
<../../../plans/cli_protocol_ownership.md>; the short version is that the SDK's typed layer is
not what the rollout records, its `receive_response()` is request-scoped in a runtime where turns
are not requests, and it never stamps a prompt with the `uuid` that makes the CLI report that
prompt's lifecycle. What is left of it after those is a launch-argument builder, which
`options.py` still uses. The protocol itself is described in <../../../cli_protocol/README.md>.

Two channels are multiplexed on one stream, distinguished by the top-level `type`:

- **Conversation** — `user` in, and `assistant` / `user` / `system` / `result` / `stream_event`
  and friends back. Append-only. Delivered here as `frames()`, verbatim dicts, because the
  console's record of a session is the wire and not a parse of it.
- **Control** — `control_request` / `control_response` correlated by `request_id`, carrying
  `initialize`, `interrupt` and the rest.

**Gotcha this exists to avoid.** The SDK's message channel is a *blocking* 100-slot buffer, and
its reader routes control responses. A consumer that stops draining conversation frames
therefore stalls the reader and takes `interrupt` down with it — which also means the stream
cannot be tee'd, and is why owning the reader and owning the control channel is one change
rather than two. Here the two are separated: control responses are resolved by the reader
itself, and conversation frames go to an unbounded queue that cannot back-pressure onto it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any, Protocol

from pydantic import BaseModel

from haku.cli_protocol.frames import (
    ControlRequestFrame,
    ControlResponse,
    ControlSubtype,
    InitializeRequest,
    InterruptRequest,
)
from haku.runtime.x.agent_sdk_transport.protocol import ClaudeLaunch, TextWebSocket
from haku.runtime.x.agent_sdk_transport.transport import ProgressSink, WebSocketTransport

logger = logging.getLogger(__name__)

# Long enough for `initialize`, which waits on the CLI's MCP servers to come up.
CONTROL_TIMEOUT_SECONDS = 60.0


class FrameChannel(Protocol):
    """A bidirectional line channel to a CLI process, however it is reached.

    Narrow on purpose: in production the process is in a sandbox pod at the far end of the
    runner's websocket, and in a test or a probe it is a local subprocess. The client should
    not be able to tell, which is also what makes it exercisable against a real CLI without
    standing up the bridge.
    """

    async def connect(self) -> None: ...

    async def write(self, data: str) -> None: ...

    def read_messages(self) -> AsyncIterator[dict[str, Any]]: ...

    async def close(self) -> None: ...


class ClaudeCliError(Exception):
    """The CLI answered a control request with an error, or never answered it."""


class ClaudeCli:
    """One CLI process, addressed over an already-authenticated transport."""

    def __init__(self, channel: FrameChannel, *, control_timeout: float = CONTROL_TIMEOUT_SECONDS):
        self._channel = channel
        self._control_timeout = control_timeout
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._conversation: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._reader: asyncio.Task[None] | None = None

    async def connect(self) -> dict[str, Any]:
        """Launch the CLI, start reading, and complete the `initialize` handshake."""
        await self._channel.connect()
        self._reader = asyncio.create_task(self._read())
        return await self.control(InitializeRequest())

    async def query(self, text: str) -> str:
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
        await self._write(
            {
                "type": "user",
                "message": {"role": "user", "content": text},
                "parent_tool_use_id": None,
                "uuid": command_uuid,
            }
        )
        return command_uuid

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

    async def frames(self) -> AsyncIterator[dict[str, Any]]:
        """Every conversation frame, in order, until the CLI's stream ends.

        Session-scoped rather than request-scoped: one `result` ends a turn, not the stream, so
        a turn is a bracket a caller draws over this rather than a call it makes.
        """
        while (frame := await self._conversation.get()) is not None:
            yield frame

    async def aclose(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None
        for pending in self._pending.values():
            if not pending.done():
                pending.set_exception(ClaudeCliError("the CLI connection closed"))
        self._pending.clear()
        await self._channel.close()

    async def _write(self, payload: dict[str, Any]) -> None:
        await self._channel.write(json.dumps(payload) + "\n")

    async def _read(self) -> None:
        """Route the stream: control responses to their waiter, everything else to the queue."""
        try:
            async for frame in self._channel.read_messages():
                match frame.get("type"):
                    case "control_response":
                        self._resolve(frame)
                    case "control_cancel_request":
                        logger.info("Claude CLI cancelled control request %s", frame.get("request_id"))
                    case "control_request":
                        await self._refuse(frame)
                    case _:
                        self._conversation.put_nowait(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Claude CLI stream failed")
        finally:
            # A sentinel rather than closing the queue: a consumer mid-turn has to learn the
            # stream ended, and would otherwise wait for a `result` that cannot arrive.
            self._conversation.put_nowait(None)

    def _resolve(self, frame: dict[str, Any]) -> None:
        response = ControlResponse.model_validate(frame["response"])
        pending = self._pending.get(response.request_id)
        if pending is None or pending.done():
            logger.warning("Claude CLI answered unknown control request %s", response.request_id)
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
    websocket: TextWebSocket, launch: ClaudeLaunch, on_progress: ProgressSink | None = None
) -> ClaudeCli:
    """The console's composition: a CLI client over the runner's bridge socket."""
    return ClaudeCli(WebSocketTransport(websocket, launch, on_progress))
