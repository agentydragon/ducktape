"""Tunnel the Claude Agent SDK transport over a text WebSocket.

The Agent SDK already defines the conversation and control protocol. This module only moves
that protocol across a WebSocket, inside the bridge envelope `protocol` defines: an SDK
message travels as one `ClaudeMessage`, and Haku's own control frames travel beside it
without sharing its key namespace.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import anyio
from claude_agent_sdk import Transport

from haku.runtime.x.agent_sdk_transport.protocol import (
    RUNNER_TO_CONSOLE,
    ClaudeLaunch,
    ClaudeMessage,
    EndInput,
    SetupOutput,
    TextWebSocket,
    decode_object,
)

# Called for each complete line the sandbox bootstrap printed. Unset drops them, which is what
# a caller with nowhere to show them should do — they are narration, and the conversation is
# unaffected.
ProgressSink = Callable[[str], Awaitable[None]]


class WebSocketTransport(Transport):
    """Agent SDK ``Transport`` backed by an already-authenticated WebSocket."""

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
        if self._closed:
            raise RuntimeError("WebSocket transport is closed")
        await self._websocket.send_text(self._launch.model_dump_json())
        self._ready = True

    async def write(self, data: str) -> None:
        if not self._ready:
            raise RuntimeError("WebSocket transport is not connected")
        await self._websocket.send_text(ClaudeMessage(payload=decode_object(data.strip())).model_dump_json())

    def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        return self._read_messages()

    async def _read_messages(self) -> AsyncIterator[dict[str, Any]]:
        if not self._ready:
            raise RuntimeError("WebSocket transport is not connected")
        try:
            while self._ready:
                # Exhaustive: `RunnerToConsole` is these two. A `start` or `end_input` coming
                # back the wrong way never reaches here — the decoder refuses it.
                match RUNNER_TO_CONSOLE.validate_json(await self._websocket.receive_text()):
                    case ClaudeMessage(payload=payload):
                        yield payload
                    case SetupOutput(data=data):
                        # Narration about the sandbox, not part of the conversation: it must
                        # not reach the SDK, which would see an unknown message shape.
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
                await self._on_progress(text)

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
