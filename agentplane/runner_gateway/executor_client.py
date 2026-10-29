"""Client for the Runner Gateway's opt-in central Codex executor stream."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiohttp
import grpc

from agentplane.proto.sandboxes_pb2 import SandboxDestination
from agentplane.runner_gateway import executor_gateway_pb2, executor_gateway_pb2_grpc

# gazelle:include_dep //agentplane/runner_gateway:executor_gateway_pb2
# gazelle:include_dep //agentplane/runner_gateway:executor_gateway_pb2_grpc
# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//types_protobuf


class ExecutorSocket:
    """WebSocket-shaped adapter over the Gateway's message-preserving gRPC stream."""

    def __init__(self, call: grpc.aio.StreamStreamCall) -> None:
        self._call = call
        self._closed = False

    async def send_json(self, value: dict[str, Any]) -> None:
        """Send a JSON text message to the configured exec-server."""
        if self._closed:
            raise ConnectionError("executor channel is closed")
        payload = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        await self._call.write(
            executor_gateway_pb2.ExecutorFrame(
                message=executor_gateway_pb2.ExecutorMessage(
                    opcode=executor_gateway_pb2.ExecutorMessage.OPCODE_TEXT, payload=payload
                )
            )
        )

    async def receive(self) -> aiohttp.WSMessage:
        """Receive one executor message as the equivalent aiohttp WebSocket message."""
        frame = await self._call.read()
        if not isinstance(frame, executor_gateway_pb2.ExecutorFrame):
            self._closed = True
            raise ConnectionError("executor Gateway stream ended")
        kind = frame.WhichOneof("frame")
        if kind == "message":
            if frame.message.opcode == executor_gateway_pb2.ExecutorMessage.OPCODE_TEXT:
                return aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, frame.message.payload.decode("utf-8"), "")
            if frame.message.opcode == executor_gateway_pb2.ExecutorMessage.OPCODE_BINARY:
                return aiohttp.WSMessage(aiohttp.WSMsgType.BINARY, frame.message.payload, "")
            raise ValueError(f"unknown executor opcode {frame.message.opcode}")
        if kind == "close":
            self._closed = True
            return aiohttp.WSMessage(aiohttp.WSMsgType.CLOSE, frame.close.code, frame.close.reason)
        raise ConnectionError(f"unexpected executor frame {kind!r}")

    async def close(self, code: int = 1000, reason: str = "") -> None:
        """Ask the Runner to close the executor WebSocket, then half-close this RPC."""
        if self._closed:
            return
        self._closed = True
        await self._call.write(
            executor_gateway_pb2.ExecutorFrame(close=executor_gateway_pb2.ExecutorClose(code=code, reason=reason))
        )
        await self._call.done_writing()


@asynccontextmanager
async def connect(
    channel: grpc.aio.Channel,
    token_file: Path,
    destination: SandboxDestination,
    expected_incarnation_uid: str,
    session_id: str,
    *,
    setup_timeout_s: float = 20,
    close_timeout_s: float = 1,
) -> AsyncIterator[ExecutorSocket]:
    """Connect to the currently authenticated Runner and yield its configured exec-server socket.

    The public protocol deliberately carries a Sandbox destination and incarnation fence instead of
    accepting a caller-selected host, port, or WebSocket URL.
    """
    if not expected_incarnation_uid or not session_id or len(session_id) > 128:
        raise ValueError("expected incarnation UID and a Session ID of at most 128 characters are required")
    token = (await asyncio.to_thread(token_file.read_text)).strip()
    if not token:
        raise ValueError("executor Gateway bearer token file is empty")
    call = executor_gateway_pb2_grpc.ExecutorGatewayStub(channel).Connect(
        metadata=(("authorization", f"Bearer {token}"),)
    )
    socket: ExecutorSocket | None = None
    try:
        async with asyncio.timeout(setup_timeout_s):
            await call.write(
                executor_gateway_pb2.ExecutorFrame(
                    open=executor_gateway_pb2.ExecutorOpen(
                        destination=destination,
                        expected_incarnation_uid=expected_incarnation_uid,
                        session_id=session_id,
                    )
                )
            )
            first = await call.read()
        if not isinstance(first, executor_gateway_pb2.ExecutorFrame) or first.WhichOneof("frame") != "ready":
            raise ConnectionError("executor Gateway did not confirm the channel")
        if not first.ready.accepted:
            raise ConnectionError("Sandbox executor is unavailable")
        socket = ExecutorSocket(call)
        yield socket
    finally:
        try:
            if socket is not None and not socket._closed:
                try:
                    async with asyncio.timeout(close_timeout_s):
                        await socket.close()
                except grpc.aio.AioRpcError, TimeoutError:
                    pass
        finally:
            # Always release the bidi stream, including setup failures and cancellation while
            # writing a frame. Closing is best effort; cancellation is definitive.
            call.cancel()
