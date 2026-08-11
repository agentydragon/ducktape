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
    Progress,
    TextWebSocket,
    decode_object,
)

# Called for each sandbox progress report. Unset drops them, which is what a caller with
# nowhere to show them should do — they are narration, and the conversation is unaffected.
ProgressSink = Callable[[str], Awaitable[None]]


class WebSocketTransport(Transport):
    """Agent SDK ``Transport`` backed by an already-authenticated WebSocket."""

    def __init__(self, websocket: TextWebSocket, launch: ClaudeLaunch, on_progress: ProgressSink | None = None):
        self._websocket = websocket
        self._launch = launch
        self._on_progress = on_progress
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
                    case Progress(line=line):
                        # Narration about the sandbox, not part of the conversation: it must
                        # not reach the SDK, which would see an unknown message shape.
                        if self._on_progress is not None:
                            await self._on_progress(line)
        except (EOFError, anyio.EndOfStream):
            self._ready = False

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
