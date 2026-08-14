"""Envelope framing for the bridge between Haku Console and the sandbox runner.

Two protocols share this socket: the CLI's own newline-delimited JSON
(<../../../cli_protocol/README.md>), and the small control protocol Haku needs around it — what
to launch, when input ends, and what the sandbox is doing before Claude exists to say anything.
Every frame on the wire is one of the models below, discriminated on ``kind``; a CLI frame
travels as `ClaudeMessage.payload`, the one field whose contents this module does not interpret.

**Deviation from what this used to be.** Both protocols shared one JSON namespace, with
Haku's frames marked by ``"type": "haku_transport"`` — a reserved value inside the *CLI's*
own ``type`` key. That holds only for as long as the CLI never emits that value, which is a
promise nobody made; it makes "is this ours?" a guess about someone else's vocabulary rather
than a property of the frame; and it leaves nowhere to put a frame that is neither side's
conversation — which ``SetupOutput`` is. Nesting the CLI's blob in a named field makes the
demultiplex explicit, so its payload can be anything at all without colliding.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

FINE_GRAINED_TOOL_STREAMING_ENV = "CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING"

# Bumped to 2 by the envelope: a v1 peer and a v2 peer cannot understand each other's frames.
# Carried on the ``start`` frame only, because the version is a property of the connection
# that its first frame settles — repeating it on every CLI payload would be noise.
PROTOCOL_VERSION: Final = 2

# Every version this image can speak, not the one it prefers. A runner's image is fixed when its
# claim is created and a live session may outlast several console releases, so "what the console
# is on" and "what the runner is on" are different numbers for hours at a time — an exact match
# cannot express that, and cannot negotiate its way out of it either.
#
# A range is only affordable because of the field policy below: the console emits frames as well
# as parsing them, so a peer that refused unknown fields would make a range mean one serializer
# per version. Ignoring them instead means one serializer, and the range costs a `min()`.
SUPPORTED_VERSIONS: Final = (2,)

# The part of this contract that is not a frame: what ending the socket means.
#
# **The runner redials after every disconnection except a refusal.** A refusal is the console
# rejecting *this runner* — a consumed credential, a session already over — which no retry can
# change, and retrying it anyway is a crashloop against a "no". Everything else, a replica going
# away mid-roll included, leaves a CLI worth reconnecting to.
#
# **Gotcha: a refusal reaches the runner as an HTTP status, not as this close code.** Every
# refusal is decided before the socket exists, and an ASGI server answers a handshake the
# application closed before accepting with `403 Forbidden` — the code below never goes on the
# wire. It is still worth naming at the console's three refusal sites, and worth naming *here*,
# next to the reason it does not travel. So the runner keys on the handshake status: a 4xx is a
# refusal, and a 5xx is a Gateway with no ready backend, which is what a console roll looks like.
#
# `GOING_AWAY_CODE` is the code that does travel, sent after `accept()` when the console is only
# saying that *this replica* is leaving. Here rather than at either end because these are rules
# spanning both, and they ship as separate images.
NOT_ADMITTED_CODE: Final = 1008
GOING_AWAY_CODE: Final = 1001


class _Frame(BaseModel):
    # **Unknown kind rejects; unknown field is ignored.** The two halves are one decision.
    #
    # An unknown `kind` already fails the union parse below, which is fail-closed exactly where a
    # must-understand change belongs: a peer that cannot name the frame cannot act on it, and
    # pretending otherwise is worse than refusing. An unknown *field* is the opposite case — a
    # peer that ignores it behaves as its own version correctly did, which is the whole point of
    # an optional addition.
    #
    # This was `forbid`, on the reasoning that silently dropping a field lets the two ends
    # disagree about what was said. True, and the wrong trade here: the console and the runner
    # are separate images that roll independently, a live session's runner keeps its image for
    # hours, so `forbid` made every additive field a fleet-wide break — every live session dying
    # on the release that added one. Additive changes now cost nothing; must-understand changes
    # arrive as new kinds, where the refusal still happens.
    # base64 for `bytes` fields, so raw program output crosses a JSON text frame without a
    # decode step that could mangle it. Only `SetupOutput` carries bytes, and it is a handful
    # of short lines per session, so the ~33% is nothing here.
    model_config = ConfigDict(extra="ignore", frozen=True, ser_json_bytes="base64", val_json_bytes="base64")


class ClaudeLaunch(_Frame):
    """Console → runner, once, first: the CLI process to run.

    Built by the trusted process (`options.build_claude_launch`), never by the runner: the
    argv decides the session's permissions and which MCP servers it reaches, so it is not the
    sandbox's to choose.
    """

    kind: Literal["start"] = "start"
    # The version the console settled on after hearing the runner's `Hello`, which is why this
    # is no longer `Literal[2]`: a negotiated number cannot be a constant. Validated against
    # `SUPPORTED_VERSIONS` rather than left open, so a peer on a version this image cannot speak
    # fails here rather than somewhere further in with a stranger symptom.
    protocol_version: int = PROTOCOL_VERSION

    @field_validator("protocol_version")
    @classmethod
    def _one_we_speak(cls, value: int) -> int:
        if value not in SUPPORTED_VERSIONS:
            raise ValueError(f"protocol version {value} is not one of {SUPPORTED_VERSIONS}")
        return value

    arguments: tuple[str, ...]
    cwd: str = Field(min_length=1)
    environment: dict[str, str]


class ClaudeMessage(_Frame):
    """One CLI protocol frame, in either direction, passed through untouched."""

    kind: Literal["claude"] = "claude"
    # The one field this module does not model: it is the CLI's vocabulary, not ours, and
    # the whole point of nesting it is that it may contain anything — including keys named
    # after our own.
    payload: dict[str, Any]


class EndInput(_Frame):
    """Console → runner: `Transport.end_input()`, i.e. close the CLI's stdin."""

    kind: Literal["end_input"] = "end_input"


class Hello(_Frame):
    """Runner → console, once, before anything else: the versions this runner can speak.

    **The runner speaks first, and this shape is frozen forever.** Negotiation needs a fixed
    point. The version used to ride on the console's first frame, which put the choice at the end
    that cannot adapt — the console had to pick before hearing anything, and the runner could not
    state its range until it had decoded a frame whose shape was the very thing in question. So
    the first frame is the runner's, and it carries only a list of integers: anything richer would
    be a shape that itself needs agreeing on, which is the regress this exists to stop.

    The console replies with the highest version both ends have, on its `start`.
    """

    kind: Literal["hello"] = "hello"
    supported: tuple[int, ...] = SUPPORTED_VERSIONS


class SetupOutput(_Frame):
    """Runner → console: bytes the sandbox bootstrap wrote, as they arrived.

    Setup runs after the socket is open precisely so it can be said out loud — a clone is the
    longest thing between "Haku is provisioning" and an answer, and without this the room shows
    a silent gap with no way to tell a slow clone from a wedged one.

    **Raw, and unsplit.** The runner is a pipe: it does not decode this, does not divide it into
    lines, and does not judge which of them are interesting. That is all the console's, which
    is the only end that knows what it wants to do with it — and it means the transport cannot
    mangle a byte it did not understand. Contrast `ClaudeMessage`, which stays parsed: that
    stream is newline-delimited JSON by contract and the client needs objects anyway.

    Every line rather than a marked subset, because the bootstrap's own output already *is* the
    account of what it did — three lines on this box, since git writes no progress bar to a
    pipe — and on a failure the error text is the thing worth having in the room. Curation
    would buy a tidiness the plan explicitly does not want yet: "while this is new, a room that
    over-explains itself is the debugging surface" (`haku/plans/matrix_chat_runtime.md` R7.1).
    """

    kind: Literal["setup_output"] = "setup_output"
    data: bytes


# The two directions carry different frames, and saying so in the types is what keeps the
# difference enforced. It is *not* request/response — this is a duplex stream where both ends
# speak unprompted, and nothing at this layer pairs a reply with a call. (The CLI's own
# control_request/control_response do correlate, by an id inside `ClaudeMessage.payload`,
# which rides inside `ClaudeMessage.payload` and is deliberately opaque here.)
ConsoleToRunner = ClaudeLaunch | ClaudeMessage | EndInput
RunnerToConsole = ClaudeMessage | Hello | SetupOutput

# Read with the adapter for the direction you are reading; write with the model's own
# `model_dump_json`. A `TypeAdapter` rather than a model's `model_validate` because the parsed
# thing is a union with no outer model to hang it on — wrapping it in one would put a carrier
# on the wire that means nothing.
CONSOLE_TO_RUNNER: TypeAdapter[ConsoleToRunner] = TypeAdapter(Annotated[ConsoleToRunner, Field(discriminator="kind")])
RUNNER_TO_CONSOLE: TypeAdapter[RunnerToConsole] = TypeAdapter(Annotated[RunnerToConsole, Field(discriminator="kind")])


class TextWebSocket(Protocol):
    """Small adapter surface shared by accepted and outbound WebSockets."""

    async def send_text(self, data: str) -> None: ...

    async def receive_text(self) -> str: ...

    async def close(self) -> None: ...


def decode_object(data: str) -> dict[str, Any]:
    """One JSON object, or a `ValueError`.

    The raw protocol lines either end reads — the CLI's stdout and the string the client
    hands `Transport.write`. Same shape as a frame's `payload`, but not a frame: it arrives
    outside this protocol and is only ever wrapped into one.
    """
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("bridge frames must contain one JSON object")
    return value


def encode_object(value: dict[str, Any]) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
