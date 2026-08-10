"""Shared text-WebSocket framing for the remote Claude stream-JSON transport."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

END_INPUT_FRAME = {"type": "haku_transport", "subtype": "end_input"}
FINE_GRAINED_TOOL_STREAMING_ENV = "CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING"
PROTOCOL_VERSION = 1


@dataclass(frozen=True)
class ClaudeLaunch:
    """CLI process configuration supplied by the trusted Agent SDK process."""

    arguments: tuple[str, ...]
    cwd: str
    environment: dict[str, str]

    def to_frame(self) -> dict[str, Any]:
        return {
            "type": "haku_transport",
            "subtype": "start",
            "protocol_version": PROTOCOL_VERSION,
            "arguments": list(self.arguments),
            "cwd": self.cwd,
            "environment": self.environment,
        }

    @classmethod
    def from_frame(cls, frame: dict[str, Any]) -> ClaudeLaunch:
        if frame.get("type") != "haku_transport" or frame.get("subtype") != "start":
            raise ValueError("first transport frame must be haku_transport/start")
        if frame.get("protocol_version") != PROTOCOL_VERSION:
            raise ValueError("unsupported Haku Agent SDK transport protocol version")

        arguments = frame.get("arguments")
        cwd = frame.get("cwd")
        environment = frame.get("environment")
        if not isinstance(arguments, list) or not all(isinstance(value, str) for value in arguments):
            raise ValueError("transport start arguments must be a list of strings")
        if not isinstance(cwd, str) or not cwd:
            raise ValueError("transport start cwd must be a non-empty string")
        if not isinstance(environment, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in environment.items()
        ):
            raise ValueError("transport start environment must map strings to strings")

        return cls(arguments=tuple(arguments), cwd=cwd, environment=environment)


class TextWebSocket(Protocol):
    """Small adapter surface shared by accepted and outbound WebSockets."""

    async def send_text(self, data: str) -> None: ...

    async def receive_text(self) -> str: ...

    async def close(self) -> None: ...


def decode_object(data: str) -> dict[str, Any]:
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("Agent SDK transport frames must contain one JSON object")
    return value


def encode_object(value: dict[str, Any]) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
