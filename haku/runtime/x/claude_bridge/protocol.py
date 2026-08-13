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

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

FINE_GRAINED_TOOL_STREAMING_ENV = "CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING"

# Bumped to 2 by the envelope: a v1 peer and a v2 peer cannot understand each other's frames.
# Carried on the ``start`` frame only, because the version is a property of the connection
# that its first frame settles — repeating it on every CLI payload would be noise.
PROTOCOL_VERSION: Final = 2


class _Frame(BaseModel):
    # `forbid` because a frame this end does not fully understand is a version mismatch, and
    # `PROTOCOL_VERSION` is how that is meant to be reported — silently dropping an unknown
    # field would let the two ends disagree about what was said. Additive evolution therefore
    # costs a version bump, which is honest: the console and the runner are separate images
    # that roll independently, so no frame change is ever atomic in production anyway.
    # base64 for `bytes` fields, so raw program output crosses a JSON text frame without a
    # decode step that could mangle it. Only `SetupOutput` carries bytes, and it is a handful
    # of short lines per session, so the ~33% is nothing here.
    model_config = ConfigDict(extra="forbid", frozen=True, ser_json_bytes="base64", val_json_bytes="base64")


class ClaudeLaunch(_Frame):
    """Console → runner, once, first: the CLI process to run.

    Built by the trusted process (`options.build_claude_launch`), never by the runner: the
    argv decides the session's permissions and which MCP servers it reaches, so it is not the
    sandbox's to choose.
    """

    kind: Literal["start"] = "start"
    # `Literal[2]` rather than a plain int, so a peer on another version fails validation
    # here rather than somewhere further in with a stranger symptom. The default keeps the
    # two spellings of the number checked against each other by the type checker.
    protocol_version: Literal[2] = PROTOCOL_VERSION
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
RunnerToConsole = ClaudeMessage | SetupOutput

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
