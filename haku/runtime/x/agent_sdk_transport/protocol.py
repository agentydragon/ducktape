"""Envelope framing for the bridge between Haku Console and the sandbox runner.

Two protocols share this socket: the Claude Agent SDK's own stream-JSON, and the small
control protocol Haku needs around it — what to launch, when input ends, and what the sandbox
is doing before Claude exists to say anything. Every frame on the wire is a Haku envelope, and
an SDK message travels as one envelope's ``payload``.

**Deviation from what this used to be.** Both protocols shared one JSON namespace, with
Haku's frames marked by ``"type": "haku_transport"`` — a reserved value inside the *SDK's*
own ``type`` key. That holds only for as long as the SDK never emits that value, which is a
promise nobody made; it makes "is this ours?" a guess about someone else's vocabulary rather
than a property of the frame; and it leaves nowhere to put a frame that is neither side's
conversation — which ``Progress`` is. The envelope makes the demultiplex explicit, so the
SDK's payload can be anything at all without colliding.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

FINE_GRAINED_TOOL_STREAMING_ENV = "CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING"

# Bumped to 2 by the envelope: a v1 peer and a v2 peer cannot understand each other's frames.
# Carried on the ``start`` frame only, because the version is a property of the connection
# that its first frame settles — repeating it on every SDK payload would be noise.
PROTOCOL_VERSION = 2


class FrameKind(StrEnum):
    """What one envelope carries."""

    START = "start"
    CLAUDE = "claude"
    END_INPUT = "end_input"
    PROGRESS = "progress"


@dataclass(frozen=True)
class ClaudeLaunch:
    """Console → runner, once, first: the CLI process to run.

    Built by the trusted process from SDK options, because a custom `Transport` never sees
    the arguments `SubprocessCLITransport` would have assembled.
    """

    arguments: tuple[str, ...]
    cwd: str
    environment: dict[str, str]


@dataclass(frozen=True)
class ClaudeMessage:
    """One Agent SDK stream-JSON object, in either direction, passed through untouched."""

    payload: dict[str, Any]


@dataclass(frozen=True)
class EndInput:
    """Console → runner: `Transport.end_input()`, i.e. close the CLI's stdin."""


@dataclass(frozen=True)
class Progress:
    """Runner → console: one line the sandbox bootstrap printed, verbatim.

    Setup runs after the socket is open precisely so this can be said out loud — a clone is
    the longest thing between "Haku is provisioning" and an answer, and without this the room
    shows a silent gap with no way to tell a slow clone from a wedged one.

    Verbatim, and every line, rather than lines the script marked as interesting. The
    bootstrap's own output already *is* the human-readable account of what it did, it is three
    lines on this box because git writes no progress bar to a pipe, and on a failure the error
    text is the thing worth having in the room. A marker convention would be a second protocol
    to keep in sync across a language boundary, buying a tidiness the plan explicitly does not
    want yet: "while this is new, a room that over-explains itself is the debugging surface"
    (`haku/plans/matrix_chat_runtime.md` R7.1).
    """

    line: str


BridgeFrame = ClaudeLaunch | ClaudeMessage | EndInput | Progress


class TextWebSocket(Protocol):
    """Small adapter surface shared by accepted and outbound WebSockets."""

    async def send_text(self, data: str) -> None: ...

    async def receive_text(self) -> str: ...

    async def close(self) -> None: ...


def encode_frame(frame: BridgeFrame) -> str:
    match frame:
        case ClaudeLaunch():
            payload: dict[str, Any] = {
                "protocol_version": PROTOCOL_VERSION,
                "arguments": list(frame.arguments),
                "cwd": frame.cwd,
                "environment": frame.environment,
            }
            kind = FrameKind.START
        case ClaudeMessage():
            payload = frame.payload
            kind = FrameKind.CLAUDE
        case EndInput():
            payload = {}
            kind = FrameKind.END_INPUT
        case Progress():
            payload = {"line": frame.line}
            kind = FrameKind.PROGRESS
    return encode_object({"kind": kind, "payload": payload})


def decode_frame(data: str) -> BridgeFrame:
    envelope = decode_object(data)
    match envelope.get("kind"):
        case FrameKind.START:
            return _launch_from(_payload_of(envelope))
        case FrameKind.CLAUDE:
            return ClaudeMessage(payload=_payload_of(envelope))
        case FrameKind.END_INPUT:
            return EndInput()
        case FrameKind.PROGRESS:
            if not isinstance(line := _payload_of(envelope).get("line"), str):
                raise ValueError("bridge progress frame needs a string line")
            return Progress(line=line)
        case unknown:
            raise ValueError(f"unsupported bridge frame kind {unknown!r}")


def _payload_of(envelope: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload := envelope.get("payload"), dict):
        raise ValueError("bridge frame payload must be one JSON object")
    return payload


def _launch_from(payload: dict[str, Any]) -> ClaudeLaunch:
    if payload.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError(
            f"unsupported Haku bridge protocol version {payload.get('protocol_version')!r}, expected {PROTOCOL_VERSION}"
        )
    arguments = payload.get("arguments")
    cwd = payload.get("cwd")
    environment = payload.get("environment")
    if not isinstance(arguments, list) or not all(isinstance(value, str) for value in arguments):
        raise ValueError("bridge start arguments must be a list of strings")
    if not isinstance(cwd, str) or not cwd:
        raise ValueError("bridge start cwd must be a non-empty string")
    if not isinstance(environment, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in environment.items()
    ):
        raise ValueError("bridge start environment must map strings to strings")
    return ClaudeLaunch(arguments=tuple(arguments), cwd=cwd, environment=environment)


def decode_object(data: str) -> dict[str, Any]:
    """One JSON object, or a `ValueError`.

    Shared by the envelope and by the raw SDK stream-JSON lines either end reads, which are
    the same shape but not the same protocol.
    """
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("Agent SDK transport frames must contain one JSON object")
    return value


def encode_object(value: dict[str, Any]) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
