"""Envelope framing for the bridge between Haku Console and the sandbox runner.

Two protocols share this socket: the Claude Agent SDK's own stream-JSON, and the small
control protocol Haku needs around it — what to launch, when input ends, and what the sandbox
is doing before Claude exists to say anything. Every frame on the wire is one of the models
below, discriminated on ``kind``; an SDK message travels as `ClaudeMessage.payload`, the one
field whose contents this module does not interpret.

**Deviation from what this used to be.** Both protocols shared one JSON namespace, with
Haku's frames marked by ``"type": "haku_transport"`` — a reserved value inside the *SDK's*
own ``type`` key. That holds only for as long as the SDK never emits that value, which is a
promise nobody made; it makes "is this ours?" a guess about someone else's vocabulary rather
than a property of the frame; and it leaves nowhere to put a frame that is neither side's
conversation — which ``Progress`` is. Nesting the SDK's blob in a named field makes the
demultiplex explicit, so its payload can be anything at all without colliding.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Annotated, Any, Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

FINE_GRAINED_TOOL_STREAMING_ENV = "CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING"

# Bumped to 2 by the envelope: a v1 peer and a v2 peer cannot understand each other's frames.
# Carried on the ``start`` frame only, because the version is a property of the connection
# that its first frame settles — repeating it on every SDK payload would be noise.
PROTOCOL_VERSION: Final = 2


class FrameKind(StrEnum):
    """What one frame carries. The discriminator of `BridgeFrame`."""

    START = "start"
    CLAUDE = "claude"
    END_INPUT = "end_input"
    PROGRESS = "progress"


class _Frame(BaseModel):
    # `forbid` because a frame this end does not fully understand is a version mismatch, and
    # `PROTOCOL_VERSION` is how that is meant to be reported — silently dropping an unknown
    # field would let the two ends disagree about what was said. Additive evolution therefore
    # costs a version bump, which is honest: the console and the runner are separate images
    # that roll independently, so no frame change is ever atomic in production anyway.
    model_config = ConfigDict(extra="forbid", frozen=True)


class ClaudeLaunch(_Frame):
    """Console → runner, once, first: the CLI process to run.

    Built by the trusted process from SDK options, because a custom `Transport` never sees
    the arguments `SubprocessCLITransport` would have assembled.
    """

    kind: Literal[FrameKind.START] = FrameKind.START
    # `Literal[2]` rather than a plain int, so a peer on another version fails validation
    # here rather than somewhere further in with a stranger symptom. The default keeps the
    # two spellings of the number checked against each other by the type checker.
    protocol_version: Literal[2] = PROTOCOL_VERSION
    arguments: tuple[str, ...]
    cwd: str = Field(min_length=1)
    environment: dict[str, str]


class ClaudeMessage(_Frame):
    """One Agent SDK stream-JSON object, in either direction, passed through untouched."""

    kind: Literal[FrameKind.CLAUDE] = FrameKind.CLAUDE
    # The one field this module does not model: it is the SDK's vocabulary, not ours, and
    # the whole point of nesting it is that it may contain anything — including keys named
    # after our own.
    payload: dict[str, Any]


class EndInput(_Frame):
    """Console → runner: `Transport.end_input()`, i.e. close the CLI's stdin."""

    kind: Literal[FrameKind.END_INPUT] = FrameKind.END_INPUT


class Progress(_Frame):
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

    kind: Literal[FrameKind.PROGRESS] = FrameKind.PROGRESS
    line: str


# The two directions carry different frames, and saying so in the types is what keeps the
# difference enforced. It is *not* request/response — this is a duplex stream where both ends
# speak unprompted, and nothing at this layer pairs a reply with a call. (The SDK's own
# control_request/control_response do correlate, by an id inside `ClaudeMessage.payload`,
# which is the SDK's business and deliberately opaque here.)
ConsoleToRunner = ClaudeLaunch | ClaudeMessage | EndInput
RunnerToConsole = ClaudeMessage | Progress

# A `TypeAdapter` rather than a model's own `model_validate`, because the thing being parsed
# is a union and there is no outer model to hang it on — adding one would put a wrapper on
# the wire that carries nothing.
_TO_RUNNER: TypeAdapter[ConsoleToRunner] = TypeAdapter(Annotated[ConsoleToRunner, Field(discriminator="kind")])
_TO_CONSOLE: TypeAdapter[RunnerToConsole] = TypeAdapter(Annotated[RunnerToConsole, Field(discriminator="kind")])


class TextWebSocket(Protocol):
    """Small adapter surface shared by accepted and outbound WebSockets."""

    async def send_text(self, data: str) -> None: ...

    async def receive_text(self) -> str: ...

    async def close(self) -> None: ...


# Serializing is direction-agnostic — a model writes the same bytes whoever holds it — so the
# two encoders exist for their signatures. That is the point: they make "the console tried to
# send a Progress" a type error at the call site, where nothing else would catch it.
def encode_to_runner(frame: ConsoleToRunner) -> str:
    return frame.model_dump_json()


def encode_to_console(frame: RunnerToConsole) -> str:
    return frame.model_dump_json()


def decode_from_console(data: str) -> ConsoleToRunner:
    """Read one frame the console sent, as the runner. Raises `ValidationError` on anything else."""
    return _TO_RUNNER.validate_json(data)


def decode_from_runner(data: str) -> RunnerToConsole:
    """Read one frame the runner sent, as the console.

    A `start` or `end_input` arriving here is refused by the discriminator rather than by a
    hand-written check further in — the direction is a property of the type, not something
    each reader has to remember to assert.
    """
    return _TO_CONSOLE.validate_json(data)


def decode_object(data: str) -> dict[str, Any]:
    """One JSON object, or a `ValueError`.

    The raw SDK stream-JSON lines either end reads — the CLI's stdout and the string the SDK
    hands `Transport.write`. Same shape as a frame's `payload`, but not a frame: it arrives
    outside this protocol and is only ever wrapped into one.
    """
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("Agent SDK transport frames must contain one JSON object")
    return value


def encode_object(value: dict[str, Any]) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
