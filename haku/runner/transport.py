"""Tunnel the CLI's newline-delimited JSON protocol over a text WebSocket.

This module only moves that protocol across a WebSocket, inside the runner protocol envelope `protocol`
defines: one CLI frame travels as one `HarnessFrame`, and Haku's own control frames travel
beside it without sharing its key namespace. What a native frame means belongs to the selected
provider adapter, not this transport.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable

import anyio

from haku.runner.protocol import (
    RUNNER_TO_CONSOLE,
    SUPPORTED_VERSIONS,
    EndInput,
    HarnessFrame,
    HarnessLaunch,
    Hello,
    SetupOutput,
    TextWebSocket,
)

# Called for each complete line the sandbox bootstrap printed. Unset drops them; they are narration,
# and the conversation is unaffected.
ProgressSink = Callable[[str], Awaitable[None]]

logger = logging.getLogger(__name__)

# `Hello` is the runner's first write once the socket is open, so anything past a round trip is a
# runner that is not going to speak at all.
HELLO_TIMEOUT_SECONDS = 30.0


class WebSocketTransport:
    """A harness client's frame channel backed by an already-authenticated WebSocket.

    Structural rather than declared: `FrameChannel` is a Protocol, so this satisfies it by shape.
    `end_input` and `is_ready` are wider than that Protocol — they exist because the runner protocol has an
    `EndInput` frame the runner answers, and today only the tests reach them.
    """

    def __init__(
        self,
        websocket: TextWebSocket,
        launch: HarnessLaunch,
        on_progress: ProgressSink | None = None,
        *,
        hello_timeout: float = HELLO_TIMEOUT_SECONDS,
    ):
        self._websocket = websocket
        self._launch = launch
        self._on_progress = on_progress
        self._hello_timeout = hello_timeout
        # The runner ships bootstrap output as it arrives, so a line can span chunks and a chunk can
        # hold several; this is the end that knows what a line is for.
        self._setup_output = b""
        self._ready = False
        self._closed = False

    async def connect(self) -> None:
        """Settle the version, then send the launch in it.

        Read-before-write is the whole of the negotiation: the runner speaks first because it is the
        end that cannot adapt — its image is fixed when its claim is created, while the console is
        whatever rolled most recently.
        """
        if self._closed:
            raise RuntimeError("WebSocket transport is closed")
        version = await self._negotiate()
        await self._websocket.send_text(self._launch.model_copy(update={"protocol_version": version}).model_dump_json())
        self._ready = True

    async def _negotiate(self) -> int:
        """The highest version both ends speak.

        Every runner says `Hello`, so silence is a runner that will never launch rather than one to
        guess a version for; a guess is a `start` frame the peer may not be able to parse, failing
        later and for a stranger reason.
        """
        try:
            with anyio.fail_after(self._hello_timeout):
                first = RUNNER_TO_CONSOLE.validate_json(await self._websocket.receive_text())
        except TimeoutError as silence:
            raise RuntimeError(f"runner sent no hello in {self._hello_timeout}s") from silence
        match first:
            case Hello(supported=supported):
                if not (common := set(supported) & set(SUPPORTED_VERSIONS)):
                    raise RuntimeError(
                        f"no protocol version in common: runner speaks {supported}, this console "
                        f"speaks {SUPPORTED_VERSIONS}"
                    )
                return max(common)
            case other:
                # A sequencing problem, not a version one: this runner skipped the handshake, and
                # reading its next frame as a launch response would compound the confusion.
                raise RuntimeError(f"runner sent {other.kind} before saying hello")

    async def write(self, frame: HarnessFrame) -> None:
        if not self._ready:
            raise RuntimeError("WebSocket transport is not connected")
        await self._websocket.send_text(frame.model_dump_json())

    def read_messages(self) -> AsyncIterator[HarnessFrame]:
        return self._read_messages()

    async def _read_messages(self) -> AsyncIterator[HarnessFrame]:
        if not self._ready:
            raise RuntimeError("WebSocket transport is not connected")
        try:
            while self._ready:
                # Exhaustive over `RunnerToConsole`. A `start` or `end_input` coming back the
                # wrong way never reaches here — the decoder refuses it.
                match RUNNER_TO_CONSOLE.validate_json(await self._websocket.receive_text()):
                    case HarnessFrame() as message:
                        # The whole envelope: `seq` orders the console's log and is what a reconnect
                        # is computed from, so unwrapping here would drop it.
                        yield message
                    case Hello():
                        # `connect` consumed this connection's one hello. A second is the runner
                        # restarting a handshake mid-conversation, which nothing here can honour.
                        raise RuntimeError("runner said hello again mid-conversation")
                    case SetupOutput(data=data):
                        # Narration about the sandbox, not part of the conversation: it must not
                        # reach the provider client, which reads only native harness frames.
                        await self._report_setup_output(data)
        except (EOFError, anyio.EndOfStream):
            self._ready = False

    async def _report_setup_output(self, data: bytes) -> None:
        """Emit whatever complete lines `data` finished, holding any partial one back.

        Decode errors are replaced rather than raised: a mangled byte in a progress notice is not
        worth ending a session over, and the runner's own log still has the original.

        A trailing line with no newline is held until one arrives, so a script that ends without one
        loses its last line — which beats guessing that every chunk boundary ends a line.
        """
        self._setup_output += data
        while b"\n" in self._setup_output:
            line, self._setup_output = self._setup_output.split(b"\n", 1)
            # Blank lines are not reports; a script that spaces its output would otherwise post
            # empty notices into the room.
            if self._on_progress is not None and (text := line.decode(errors="replace").strip()):
                # Narration is not worth a session. The sink posts into a Matrix room, which can
                # rate-limit or be down, and this is awaited inside the read loop — so an unguarded
                # raise would end the conversation and file a rate-limited bootstrap as a runtime
                # failure.
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
