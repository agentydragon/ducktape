"""Typed Claude operations over a lossless native transport.

This is intentionally not a conversation projection.  It constructs and recognizes Claude's
native control/input frames; the runner adapter remains responsible for translating its native
event stream into durable Agentplane observations.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from agentplane.native.claude import driver, wire
from agentplane.native.transport import Frame, NativeReceipt, NativeTransport

type ClaudeResponse = wire.ControlResponseFrame | wire.ResultFrame


@dataclass(frozen=True)
class ClaudeReceipt:
    response: ClaudeResponse
    sequence: int


class ClaudeHarness:
    """Claude-native requests over a transport which retains every raw receipt."""

    def __init__(self, transport: NativeTransport) -> None:
        self.transport = transport

    async def initialize(
        self,
        *,
        hooks: dict[str, list[str]] | None = None,
        instructions: str = "",
        sdk_mcp_servers: list[str] | None = None,
        sdk_mcp_server_configs: dict[str, wire.SdkMcpServerConfig] | None = None,
    ) -> ClaudeReceipt:
        request = driver.initialize(
            hooks=hooks,
            instructions=instructions,
            sdk_mcp_servers=sdk_mcp_servers,
            sdk_mcp_server_configs=sdk_mcp_server_configs,
        )
        receipt = await self.transport.request(
            request, matches=lambda frame: _is_initialize_response(frame, request.request_id)
        )
        return _receipt(receipt)

    async def submit(self, text: str, *, message_uuid: str | None = None) -> wire.UserInput:
        frame = driver.user_frame(text, message_uuid=message_uuid)
        await self.transport.send(frame)
        return frame

    async def submit_many(self, texts: list[str]) -> list[wire.UserInput]:
        """Submit a batch without exposing Claude's raw user-frame constructor to callers."""
        frames = [driver.user_frame(text) for text in texts]
        for frame in frames:
            await self.transport.send(frame)
        return frames

    async def interrupt(self, *, cancel_queued: bool, reason: str = "capture") -> ClaudeReceipt:
        request = driver.interrupt(cancel_queued=cancel_queued, reason=reason)
        receipt = await self.transport.request(
            request, matches=lambda frame: _is_control_response(frame, request.request_id)
        )
        return _receipt(receipt)

    async def signal_interrupt(self, *, cancel_queued: bool, reason: str) -> None:
        """Send an interrupt without waiting for Claude's control acknowledgement."""
        await self.transport.send(driver.interrupt(cancel_queued=cancel_queued, reason=reason))

    async def set_model(self, model: str) -> ClaudeReceipt:
        request = driver.set_model(model)
        receipt = await self.transport.request(
            request, matches=lambda frame: _is_control_response(frame, request.request_id)
        )
        return _receipt(receipt)

    async def send(self, frame: BaseModel) -> None:
        """Send a typed Claude response generated while translating a native event."""
        await self.transport.send(frame)


def _receipt(receipt: NativeReceipt) -> ClaudeReceipt:
    parsed = wire.parse_frame(receipt.frame)
    if not isinstance(parsed, (wire.ControlResponseFrame, wire.ResultFrame)):
        raise RuntimeError(f"Claude control request received an unexpected frame: {parsed}")
    return ClaudeReceipt(response=parsed, sequence=receipt.sequence)


def _is_control_response(frame: Frame, request_id: str) -> bool:
    response = frame.get("response")
    return (
        frame.get("type") == "control_response"
        and isinstance(response, dict)
        and response.get("request_id") == request_id
    )


def _is_initialize_response(frame: Frame, request_id: str) -> bool:
    return _is_control_response(frame, request_id) or frame.get("type") == "result"
