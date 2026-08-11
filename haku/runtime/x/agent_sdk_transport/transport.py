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
    ClaudeLaunch,
    ClaudeMessage,
    EndInput,
    Progress,
    TextWebSocket,
    decode_frame,
    decode_object,
    encode_frame,
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
        await self._websocket.send_text(encode_frame(self._launch))
        self._ready = True

    async def write(self, data: str) -> None:
        if not self._ready:
            raise RuntimeError("WebSocket transport is not connected")
        await self._websocket.send_text(encode_frame(ClaudeMessage(payload=decode_object(data.strip()))))

    def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        return self._read_messages()

    async def _read_messages(self) -> AsyncIterator[dict[str, Any]]:
        if not self._ready:
            raise RuntimeError("WebSocket transport is not connected")
        try:
            while self._ready:
                match decode_frame(await self._websocket.receive_text()):
                    case ClaudeMessage(payload=payload):
                        yield payload
                    case Progress(detail=detail):
                        # Narration about the sandbox, not part of the conversation: it must
                        # not reach the SDK, which would see an unknown message shape.
                        if self._on_progress is not None:
                            await self._on_progress(detail)
                    case other:
                        # `start` and `end_input` only ever travel console → runner, so a
                        # runner sending one is a protocol bug rather than something to route.
                        raise ValueError(f"runner sent {type(other).__name__}, which is not a conversation frame")
        except (EOFError, anyio.EndOfStream):
            self._ready = False

    async def end_input(self) -> None:
        if self._ready:
            await self._websocket.send_text(encode_frame(EndInput()))

    async def close(self) -> None:
        if self._closed:
            return
        self._ready = False
        self._closed = True
        await self._websocket.close()

    def is_ready(self) -> bool:
        return self._ready and not self._closed
