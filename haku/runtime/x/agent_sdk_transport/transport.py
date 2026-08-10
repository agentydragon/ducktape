"""Tunnel the Claude Agent SDK transport over a text WebSocket.

The Agent SDK already defines the conversation and control protocol. This module
only moves that protocol across a WebSocket: ordinary frames contain exactly one
JSON object from the SDK/Claude CLI stream, while one reserved frame represents
``Transport.end_input()``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import anyio
from claude_agent_sdk import Transport

from haku.runtime.x.agent_sdk_transport.protocol import (
    END_INPUT_FRAME,
    ClaudeLaunch,
    TextWebSocket,
    decode_object,
    encode_object,
)


class WebSocketTransport(Transport):
    """Agent SDK ``Transport`` backed by an already-authenticated WebSocket."""

    def __init__(self, websocket: TextWebSocket, launch: ClaudeLaunch):
        self._websocket = websocket
        self._launch = launch
        self._ready = False
        self._closed = False

    async def connect(self) -> None:
        if self._closed:
            raise RuntimeError("WebSocket transport is closed")
        await self._websocket.send_text(encode_object(self._launch.to_frame()))
        self._ready = True

    async def write(self, data: str) -> None:
        if not self._ready:
            raise RuntimeError("WebSocket transport is not connected")
        await self._websocket.send_text(encode_object(decode_object(data.strip())))

    def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        return self._read_messages()

    async def _read_messages(self) -> AsyncIterator[dict[str, Any]]:
        if not self._ready:
            raise RuntimeError("WebSocket transport is not connected")
        try:
            while self._ready:
                yield decode_object(await self._websocket.receive_text())
        except (EOFError, anyio.EndOfStream):
            self._ready = False

    async def end_input(self) -> None:
        if self._ready:
            await self._websocket.send_text(encode_object(END_INPUT_FRAME))

    async def close(self) -> None:
        if self._closed:
            return
        self._ready = False
        self._closed = True
        await self._websocket.close()

    def is_ready(self) -> bool:
        return self._ready and not self._closed
