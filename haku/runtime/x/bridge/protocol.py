"""Envelope framing for the incompatible v4 bridge between Console and a harness runner.

Two protocols share this socket: the CLI's own newline-delimited JSON
(<../../../cli_protocol/README.md>), and the small control protocol Haku needs around it — what
to launch, when input ends, and what the sandbox is doing before a harness exists to say anything.
Native harness frames are opaque to this module and always travel in ``HarnessFrame.frame``. The
outer ``kind`` is backend-neutral: the complete inner frame (including its own discriminator) is
never flattened into the bridge vocabulary or the database's ``kind`` column.

**v4 is the neutral-operation generation** (#4667). The runner interprets native frames and the
conversation crosses this envelope as the acknowledged journal of <neutral_operations.py>, ridden
in ``RunnerJournal``/``ConsoleJournal``; the Console dispatches prompts by durable id
(``PromptDispatch``) instead of writing native input, and native frames still travel — as the
durable record (`session_frames`), no longer as a projection input. v3 peers fail closed here, at
the version negotiation both ends already enforce: ``SUPPORTED_VERSIONS`` holds only 4, so an old
runner and a new Console (or the reverse) find no common version and refuse the connection —
which is the exact-generation peering the maintenance-gated cutover relies on, doubled inside the
journal handshake by `RunnerHello.generation` against the migration-set active generation.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Final, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from haku.runtime.x.bridge.neutral_operations import BatchAck, ConsoleResume, OperationBatch, RunnerHello

FINE_GRAINED_TOOL_STREAMING_ENV = "CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING"
KUBERNETES_PROXY_URL_ENV = "HAKU_KUBERNETES_PROXY_URL"
RUNNER_SETUP_ENV = "HAKU_RUNNER_SETUP"

# Carried on the ``start`` frame only: the version is a property of the connection that its first
# frame settles.
PROTOCOL_VERSION: Final = 4

# Every version this image can speak, not the one it prefers. A runner's image is fixed when its
# claim is created and a live session may outlast several console releases, so console and runner
# sit on different numbers for hours at a time. Deliberately only 4: the generation cut carries no
# dual-protocol period, and the empty intersection with a v3 peer is the fail-closed gate.
SUPPORTED_VERSIONS: Final = (4,)

# What ending the socket means. **The runner redials after every disconnection except a refusal**,
# which is the console rejecting *this runner* — a consumed credential, a session already over — and
# no retry can change that.
#
# **Gotcha: a refusal reaches the runner as an HTTP status, not as this close code.** Refusals are
# decided before the socket exists, and an ASGI server answers a handshake closed before `accept()`
# with `403 Forbidden`, so `NOT_ADMITTED_CODE` never goes on the wire. The runner therefore keys on
# the handshake status: 4xx is a refusal, 5xx is a Gateway with no ready backend — a console roll.
#
# `GOING_AWAY_CODE` does travel, sent after `accept()` when only *this replica* is leaving. Both
# live here because they are rules spanning two ends that ship as separate images.
NOT_ADMITTED_CODE: Final = 1008
GOING_AWAY_CODE: Final = 1001


class _Frame(BaseModel):
    # **Unknown kind rejects; unknown field is ignored.** An unknown `kind` fails the union parse
    # below, which is where a must-understand change belongs. `forbid` is not an option for fields:
    # console and runner are separate images that roll independently, so refusing an unknown field
    # would kill every live session on the release that added one. Must-understand changes arrive as
    # new kinds instead.
    #
    # base64 for `bytes`, so raw program output crosses a JSON text frame without a decode step that
    # could mangle it.
    model_config = ConfigDict(extra="ignore", frozen=True, ser_json_bytes="base64", val_json_bytes="base64")


class HarnessLaunch(_Frame):
    """Console → runner, once, first: the selected harness process to run.

    Built by the trusted provider adapter, never by the runner: the
    argv decides the session's permissions and which MCP servers it reaches, so it is not the
    sandbox's to choose.
    """

    kind: Literal["start"] = "start"
    # The version the console settled on after hearing the runner's `Hello`. Validated against
    # `SUPPORTED_VERSIONS` so a peer on a version this image cannot speak fails here rather than
    # further in with a stranger symptom.
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

    # The highest `seq` the console has recorded **for this session**, or None from a console that
    # does not number (or has recorded nothing yet). The runner replays only what is above it and
    # continues numbering from there.
    #
    # Per session rather than per connection, so two consoles adopting one runner's window during a
    # roll compute it from the same rows and agree. It rides on `start` because that frame is sent
    # on every connection, reconnects included, where the runner ignores the launch but reads the
    # frame.
    resume_from: int | None = None


class HarnessFrame(_Frame):
    """One opaque native harness frame, in either direction, passed through untouched."""

    kind: Literal["harness_frame"] = "harness_frame"
    # The exact JSON object emitted by or sent to the selected harness. The bridge adds no inner
    # wrapper and does not interpret a provider discriminator such as Claude's ``type`` or a
    # JSON-RPC ``method``.
    frame: dict[str, Any]
    # **The runner's own number for this frame, dense and monotonic per session.** Set only on the
    # runner → console direction; None on the console's writes, which the runner does not number.
    #
    # This is what makes a reconnect "send me everything after N": the number is the peer's, so the
    # peer can act on a cursor built from it, and it is dense, so a hole in it is evidence of loss
    # rather than of a gap an `Identity` was always free to leave. The console records it and still
    # orders its log by its own `frame_seq`; swapping those two over is planned
    # conversation-layers work (<../../../console/plans/conversation_layers.md>).
    #
    # Assigned where the frame is put on the wire, not where it is built, so a frame re-sent from
    # the replay window keeps the number it first went out under.
    #
    # Optional rather than a `PROTOCOL_VERSION` bump, which would refuse peers on the other number
    # and end every session in flight. `extra="ignore"` drops it for a peer that predates it, and
    # None is what a console reading an older runner sees.
    seq: int | None = None
    # **True for the runner's numbered echo of native input it wrote to the CLI itself** — the
    # dispatched prompt's user frame, the interrupt control request. Under v4 the Console composes
    # no native input, so the durable record's `to_agent` half has to come from the end that wrote
    # it; the echo travels runner → console like everything else the runner numbers, and the
    # Console records it with the direction this flag names instead of `from_agent`.
    injected: bool = False


class EndInput(_Frame):
    """Console → runner: `WebSocketTransport.end_input()`, i.e. close the CLI's stdin."""

    kind: Literal["end_input"] = "end_input"


class Hello(_Frame):
    """Runner → console, once, before anything else: the versions this runner can speak.

    **The runner speaks first, and this shape is frozen forever.** Negotiation needs a fixed point,
    and the runner is the end that cannot adapt — its image is fixed when its claim is created. It
    carries only a list of integers; anything richer would itself need agreeing on.

    The console replies with the highest version both ends have, on its `start`.
    """

    kind: Literal["hello"] = "hello"
    supported: tuple[int, ...] = SUPPORTED_VERSIONS


class SetupOutput(_Frame):
    """Runner → console: bytes the sandbox bootstrap wrote, as they arrived.

    Setup runs after the socket is open so it can be said out loud: a clone is the longest thing
    between "Haku is provisioning" and an answer, and without this the room shows a silent gap with
    no way to tell a slow clone from a wedged one.

    **Raw, and unsplit.** The runner does not decode these bytes, split them into lines, or filter
    them; that is the console's, and it means the transport cannot mangle what it did not
    understand. Contrast `HarnessFrame`, which stays parsed: that stream is newline-delimited JSON
    by contract and the client needs objects anyway.

    Every line goes, uncurated — on a failure the error text is the thing worth having in the room.
    Curation would buy a tidiness the room deliberately does not want yet: while this is new, a room
    that over-explains itself is the debugging surface
    (<../../../console/channels/matrix/SPEC.md> § What the room shows while a turn runs).
    """

    kind: Literal["setup_output"] = "setup_output"
    data: bytes
    # As `HarnessFrame.seq`: numbered like everything else the runner sends. Setup output remains
    # outside the native harness log, but its position is still preserved on the wire so a replay
    # cannot silently change ordering.
    seq: int | None = None


class RunnerJournal(_Frame):
    """Runner → console: one message of the neutral-operation journal, in the bridge envelope.

    A wrapper kind rather than flattening the journal's own messages into this union, because the
    two vocabularies collide (`RunnerHello` and `Hello` both discriminate on ``hello``) and because
    the journal is its own versioned contract (<neutral_operations.py>): what rides here is decided
    by the hello/resume negotiation inside it, not by `PROTOCOL_VERSION`.

    The first journal message on every connection is the `RunnerHello`; after the Console's
    `ConsoleResume` comes back, every further one is an `OperationBatch`.
    """

    kind: Literal["journal"] = "journal"
    message: Annotated[RunnerHello | OperationBatch, Field(discriminator="kind")]


class ConsoleJournal(_Frame):
    """Console → runner: the journal's answers — the resume on every connection, then ACKs."""

    kind: Literal["journal"] = "journal"
    message: Annotated[ConsoleResume | BatchAck, Field(discriminator="kind")]


class PromptDispatch(_Frame):
    """Console → runner: inject this pending prompt at the runner's native-input fence.

    The whole authority split in one frame: the text rides here so the runner can compose the
    native input, but the durable truth stays the Console's `submitted_prompt` row — the runner
    echoes only `prompt_id` back in `prompt.admitted`, and the Console materialises the transcript
    item from its own row. Idempotent by `prompt_id`: the Console re-dispatches
    dispatched-but-unadmitted prompts after a reconnect, and the runner ignores an id it has
    already taken.
    """

    kind: Literal["prompt"] = "prompt"
    prompt_id: UUID
    text: str = Field(min_length=1)


class Interrupt(_Frame):
    """Console → runner: the operator asked the running exchange to stop.

    The runner interrupts the CLI in the CLI's own vocabulary and, because it asked, ends the open
    turn `aborted` whatever the harness calls the result — the journal's `TurnAborted` is minted by
    the side that knows an abort happened, which under v4 is the runner.
    """

    kind: Literal["interrupt"] = "interrupt"


# The two directions carry different frames. Not request/response: both ends speak unprompted and
# nothing at this layer pairs a reply with a call. (The CLI's own control_request/control_response
# do correlate, by an id that rides inside `HarnessFrame.frame` and is opaque here.)
#
# `HarnessFrame` stays writable console → runner for the legacy Console fold still in-tree behind
# the generation gate; the journal path never sends one. It leaves with that fold (#4667 stage 5).
ConsoleToRunner = HarnessLaunch | HarnessFrame | EndInput | ConsoleJournal | PromptDispatch | Interrupt
RunnerToConsole = HarnessFrame | Hello | SetupOutput | RunnerJournal

# Read with the adapter for the direction you are reading; write with the model's own
# `model_dump_json`.
CONSOLE_TO_RUNNER: TypeAdapter[ConsoleToRunner] = TypeAdapter(Annotated[ConsoleToRunner, Field(discriminator="kind")])
RUNNER_TO_CONSOLE: TypeAdapter[RunnerToConsole] = TypeAdapter(Annotated[RunnerToConsole, Field(discriminator="kind")])


class TextWebSocket(Protocol):
    """Small adapter surface shared by accepted and outbound WebSockets."""

    async def send_text(self, data: str) -> None: ...

    async def receive_text(self) -> str: ...

    async def close(self) -> None: ...


def decode_object(data: str) -> dict[str, Any]:
    """One JSON object, or a `ValueError`.

    For the raw protocol lines either end reads — the CLI's stdout, and the string the client hands
    `FrameChannel.write` — which arrive outside this protocol and are only ever wrapped into it.
    """
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("bridge frames must contain one JSON object")
    return value


def encode_object(value: dict[str, Any]) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
