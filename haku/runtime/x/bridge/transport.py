"""Tunnel the CLI's newline-delimited JSON protocol over a text WebSocket.

This module only moves that protocol across a WebSocket, inside the bridge envelope `protocol`
defines: one CLI frame travels as one `ClaudeMessage`, and Haku's own control frames travel
beside it without sharing its key namespace. What the frames mean is
<../../../cli_protocol/README.md>; what reads them is `cli_client.ClaudeCli`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable

import anyio

from haku.runtime.x.bridge.protocol import (
    PROTOCOL_VERSION,
    RUNNER_TO_CONSOLE,
    SUPPORTED_VERSIONS,
    ClaudeLaunch,
    ClaudeMessage,
    EndInput,
    Hello,
    SetupOutput,
    TextWebSocket,
    decode_object,
)

# Called for each complete line the sandbox bootstrap printed. Unset drops them, which is what
# a caller with nowhere to show them should do — they are narration, and the conversation is
# unaffected.
ProgressSink = Callable[[str], Awaitable[None]]

logger = logging.getLogger(__name__)

# CLEANUP(added 2026-08-14): Delete this wait, and the fallback below it, once no runner image
#   predating `Hello` can still be running — i.e. once every live SandboxClaim was created after
#   the release that added it, which `session_ttl_seconds` bounds at two hours. Until then a
#   runner that never says hello is not broken, only older, and the console must not hang on it.
#   The cost of keeping it is this wait on every legacy connection; the cost of deleting it early
#   is every such session failing to start.
HELLO_SECONDS = 2.0


class WebSocketTransport:
    """A `cli_client.FrameChannel` backed by an already-authenticated WebSocket.

    Structural rather than declared: `FrameChannel` is a Protocol, so this satisfies it by
    shape. It used to subclass the Agent SDK's `Transport` ABC, which is what `end_input` and
    `is_ready` were for — they stay because the bridge protocol has an `EndInput` frame the
    runner answers, not because a base class demands them.
    """

    def __init__(self, websocket: TextWebSocket, launch: ClaudeLaunch, on_progress: ProgressSink | None = None):
        self._websocket = websocket
        self._launch = launch
        self._on_progress = on_progress
        # The runner ships bootstrap output as it arrives, so a line can span chunks and a
        # chunk can hold several. Reassembly is here because this is the end that knows what
        # a line is for.
        self._setup_output = b""
        self._ready = False
        self._closed = False

    async def connect(self) -> None:
        """Settle the version, then send the launch in it.

        Read-before-write, which is the whole of the negotiation: the runner speaks first because
        it is the end that cannot adapt — its image is fixed when its claim is created, while the
        console is whatever rolled most recently. A console that chose before hearing would be
        choosing for a peer it knows nothing about.
        """
        if self._closed:
            raise RuntimeError("WebSocket transport is closed")
        version = await self._negotiate()
        await self._websocket.send_text(self._launch.model_copy(update={"protocol_version": version}).model_dump_json())
        self._ready = True

    async def _negotiate(self) -> int:
        """The highest version both ends speak, or `PROTOCOL_VERSION` for a runner that is older.

        Silence is the old runner: it waits for `start` and says nothing before it, so the only
        way to tell it from a slow one is to stop waiting. See `HELLO_SECONDS`, which carries the
        condition for deleting this whole branch.
        """
        with anyio.move_on_after(HELLO_SECONDS):
            match RUNNER_TO_CONSOLE.validate_json(await self._websocket.receive_text()):
                case Hello(supported=supported):
                    if not (common := set(supported) & set(SUPPORTED_VERSIONS)):
                        raise RuntimeError(
                            f"no protocol version in common: runner speaks {supported}, this console "
                            f"speaks {SUPPORTED_VERSIONS}"
                        )
                    return max(common)
                case first:
                    # Not a version problem but a sequencing one, and worth saying so: a runner
                    # that sends anything before its hello has skipped the handshake, and reading
                    # its next frame as a launch response would compound the confusion.
                    raise RuntimeError(f"runner sent {first.kind} before saying hello")
        logger.info("Runner said no hello in %ss; assuming protocol %d", HELLO_SECONDS, PROTOCOL_VERSION)
        return PROTOCOL_VERSION

    async def write(self, data: str) -> None:
        if not self._ready:
            raise RuntimeError("WebSocket transport is not connected")
        await self._websocket.send_text(ClaudeMessage(payload=decode_object(data.strip())).model_dump_json())

    def read_messages(self) -> AsyncIterator[ClaudeMessage]:
        return self._read_messages()

    async def _read_messages(self) -> AsyncIterator[ClaudeMessage]:
        if not self._ready:
            raise RuntimeError("WebSocket transport is not connected")
        try:
            while self._ready:
                # Exhaustive over `RunnerToConsole`. A `start` or `end_input` coming back the
                # wrong way never reaches here — the decoder refuses it.
                match RUNNER_TO_CONSOLE.validate_json(await self._websocket.receive_text()):
                    case ClaudeMessage() as message:
                        # The whole envelope rather than its payload: `seq` is the runner's number
                        # for this frame and the console's log is ordered by it, so unwrapping here
                        # would drop the one field a reconnect is computed from.
                        yield message
                    case Hello():
                        # `connect` consumed the one hello this connection has. A second is the
                        # runner restarting a handshake mid-conversation, which nothing here can
                        # honour — and without this case the `match` would drop it in silence.
                        raise RuntimeError("runner said hello again mid-conversation")
                    case SetupOutput(data=data):
                        # Narration about the sandbox, not part of the conversation: it must
                        # not reach `ClaudeCli`, which would see an unknown frame type.
                        await self._report_setup_output(data)
        except (EOFError, anyio.EndOfStream):
            self._ready = False

    async def _report_setup_output(self, data: bytes) -> None:
        """Emit whatever complete lines `data` finished, holding any partial one back.

        Decoding happens here, on a whole line, and only to hand the sink a `str` — errors are
        replaced rather than raised because a mangled byte in a progress notice is not worth
        ending a session over, and the runner's own log still has the original.

        A trailing line with no newline is held until one arrives. In practice none does not:
        the bootstrap's every line comes from `echo`. If a script ever ends without one, that
        last line is the cost, which beats guessing that every chunk boundary ends a line.
        """
        self._setup_output += data
        while b"\n" in self._setup_output:
            line, self._setup_output = self._setup_output.split(b"\n", 1)
            # Blank lines are not reports; a script that spaces its output would otherwise
            # post empty notices into the room.
            if self._on_progress is not None and (text := line.decode(errors="replace").strip()):
                # Narration is not worth a session. The sink posts into a Matrix room, which
                # can rate-limit, lose its binding, or simply be down — and this is awaited
                # inside the read loop, so an unguarded raise here ends the conversation and
                # records the sink's error over whatever the narration was reporting. That is
                # exactly how a rate-limited bootstrap comes to be filed as a runtime failure.
                try:
                    await self._on_progress(text)
                except Exception:
                    logger.warning("Progress report failed for %r", text, exc_info=True)

    async def end_input(self) -> None:
        if self._ready:
            await self._websocket.send_text(EndInput().model_dump_json())

    async def close(self) -> None:
        if self._closed:
            return
        self._ready = False
        self._closed = True
        await self._websocket.close()

    def is_ready(self) -> bool:
        return self._ready and not self._closed
