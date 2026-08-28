"""The neutral conversation-operation journal between a runtime runner and the Console.

The next-generation vocabulary at the seam <protocol.py> frames today (#4667, as amended by the
operator rulings there): the runner owns native-frame interpretation and emits harness-neutral
operations; the Console stops parsing native payloads entirely. Nothing speaks these shapes yet —
the runner-side projector and the Console journal consumer both build against them, and the
maintenance-gated generation cut (`GENERATION`) activates the two together.

**Authority split.** The Console owns operator identity, prompt text/origin, authorization, durable
prompt delivery, conversation storage, and subscribers; a submitted prompt is first a durable
Console row, never a transcript item. The runner owns the serialization point at which the native
harness accepts input, native-frame interpretation, and output ordering: it says `prompt.admitted`
at the exact fence where it injected a prompt, and the Console materializes the authored prompt
item there from its own row. Who said what is the Console's; when the LLM saw it is the runner's.

**Materialized identities only.** Every item and turn travels under an id the runner mints and
every later operation repeats. No Console fold state crosses the wire — nothing addresses "the
currently open item of this type", which would prohibit concurrent streamed items. An item's text
is the concatenation of its `item.segment` texts, the sole authority for it; completions carry no
prose. `backend_item_id` is provenance, never identity: it is what the native protocol called the
item, absent whenever the wire supplied none.

**The journal, and how it commits.** Runner → Console is a monotonic journal of batches, numbered
densely by `runner_batch_seq` from 1, so a hole is evidence of loss rather than a gap the numbering
was free to leave. The Console commits a whole batch and its seq atomically, then ACKs
cumulatively. The runner retains unacknowledged batches in memory and, after a reconnect, replays
every retained batch above `ConsoleResume.acked_batch_seq`; a batch seen twice is the same batch,
so commit is idempotent by seq and replay changes nothing. A runner/session stays terminal on
runner loss — no Console-side re-projection or recovery path is part of this contract.

**Batching is behavior, not fields.** The runner flushes operations immediately, coalescing into
one batch only what accumulated while the previous ACK was in flight. No accumulation timers and no
delay knob exist, deliberately: on a healthy link, partial text tracks the native stream at
per-frame-ish latency (one RTT plus commit), and a slow consumer grows batches gracefully under
backpressure instead of stalling the stream.

**Frames are the record beside the journal, never an input to it.** Native frames continue to be
persisted durably (`session_frames`), keyed by the runner-assigned frame seq that `FrameRange`
provenance names. The Console never parses them: they are record/debug/provenance, correlated to
operations by `{first_frame_seq, last_frame_seq}`, and not a recovery path. Operation commits never
gate on frame arrival — the two streams are independent, so a provenance range may momentarily
dangle ahead of frame persistence.

**Unknown kind rejects; unknown field is ignored.** This seam is version-negotiated
(<../console/README.md> § Vocabularies across a roll), so after the handshake settles
`neutral_protocol_version` a kind the settled version does not define is a defect, and the union
parse below is where it fails. New operation kinds therefore ship as a new negotiated version;
additive fields are ignorable and may ship freely. The generation cut means no peer of this
protocol predates it: both ends fail closed on a generation mismatch instead of guessing.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Final, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter, model_validator

# The transport generation this build belongs to. Set globally by the cutover migration and
# presented by both peers on the handshake: an exact match admits, anything else fails closed —
# the gate that keeps an old bridge peer and a new one from ever serving one conversation.
GENERATION: Final = "runner_projection_v1"

NEUTRAL_PROTOCOL_VERSION: Final = 1

# Every version this image can speak, not the one it prefers. As on the v3 bridge: a runner's
# image is fixed when its claim is created and a live session may outlast several Console
# releases, so the two ends sit on different numbers for hours at a time.
SUPPORTED_NEUTRAL_VERSIONS: Final = (1,)


def _one_we_speak(value: int) -> int:
    """Fail a version this image cannot speak at parse, rather than further in with a stranger
    symptom."""
    if value not in SUPPORTED_NEUTRAL_VERSIONS:
        raise ValueError(f"neutral protocol version {value} is not one of {SUPPORTED_NEUTRAL_VERSIONS}")
    return value


type SettledVersion = Annotated[int, AfterValidator(_one_we_speak)]

# Whatever the native protocol put in a field this layer passes through rather than reads. Its own
# alias rather than the record layer's (`haku/console/x/conversation_events.py`): the Console
# depends on the runtime and never the reverse, so the wire cannot import from it.
type Json = None | bool | int | float | str | list[Json] | dict[str, Json]


class _Message(BaseModel):
    # An unknown `kind` fails the union parse, which is where a must-understand change belongs on
    # this negotiated seam. `forbid` is not an option for fields: Console and runner are separate
    # images that roll independently within a generation, so refusing an unknown field would kill
    # every live session on the release that added one.
    model_config = ConfigDict(extra="ignore", frozen=True)


class ItemType(StrEnum):
    """What kind of runner-observed item an operation opens or completes.

    The wire half of the durable vocabulary (`haku/console/chat_models.py` `ItemType`), member for
    member — minus `prompt`, deliberately: a prompt item is authored by the Console from its own
    row when `prompt.admitted` names it, so a runner opening one is not expressible.
    """

    MESSAGE = "message"
    REASONING = "reasoning"
    TOOL_CALL = "tool_call"


class ToolOutcome(StrEnum):
    """How a tool call went, in the harness vocabulary rather than any one tool's.

    `UNKNOWN` is a real outcome and not a missing one: a call whose answer the backend reported
    without saying whether it succeeded, which every harness protocol permits.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ReasoningDisclosure(StrEnum):
    """How much of a reasoning item's thinking the backend actually handed back.

    No backend we adapt returns raw chain of thought, so the distinction worth carrying is not
    summary-versus-reasoning but whether anything was disclosed at all.
    """

    SUMMARY = "summary"
    WITHHELD = "withheld"


class TurnOutcome(StrEnum):
    """How one exchange ended."""

    ANSWERED = "answered"
    ABORTED = "aborted"
    FAILED = "failed"


class FrameRange(_Message):
    """The inclusive span of runner-numbered native frames one operation was projected from.

    A span, not a set: a message interrupted by a tool result spans the interruption too. The
    numbers are the runner's frame seqs — the key `session_frames` stores beside each persisted
    frame — so an operator can appeal any operation to the raw JSON that produced it. Per
    operation, not per entity: a turn's whole bracket is folded by the consumer from the ranges of
    its two end operations.
    """

    first_frame_seq: int = Field(ge=0)
    last_frame_seq: int = Field(ge=0)

    @model_validator(mode="after")
    def _a_span(self) -> FrameRange:
        if self.last_frame_seq < self.first_frame_seq:
            raise ValueError(f"a frame range runs forward: {self.first_frame_seq=} {self.last_frame_seq=}")
        return self


class PromptsCause(_Message):
    """The turn exists because these admitted prompts were handed to the harness."""

    kind: Literal["prompts"] = "prompts"
    prompt_ids: tuple[UUID, ...] = Field(
        min_length=1, description="The already-admitted prompts this turn answers, in admission order."
    )


class WakeCause(_Message):
    """The harness woke itself — a scheduled wakeup, a background task completing. Nobody spoke."""

    kind: Literal["wake"] = "wake"


class OpaqueCause(_Message):
    """The runner observed a turn begin without a native cause it can classify."""

    kind: Literal["opaque"] = "opaque"


type TurnCause = PromptsCause | WakeCause | OpaqueCause


class TurnAnswered(_Message):
    """The agent finished the exchange."""

    outcome: Literal[TurnOutcome.ANSWERED] = TurnOutcome.ANSWERED


class TurnAborted(_Message):
    """Someone stopped it. It carries no reason because nothing went wrong."""

    outcome: Literal[TurnOutcome.ABORTED] = TurnOutcome.ABORTED


class TurnFailed(_Message):
    """The exchange could not finish, and why.

    **`failure` is required**: three shapes rather than one with an optional reason, because that
    one would also spell an answered turn carrying a failure and a failed turn carrying none.
    """

    outcome: Literal[TurnOutcome.FAILED] = TurnOutcome.FAILED
    failure: str = Field(description="Bounded prose for an operator, in the words the runtime used.")


type TurnEnd = TurnAnswered | TurnAborted | TurnFailed


class TurnOpened(_Message):
    """One exchange began. Turns are brackets around output, not admission locks: nothing in this
    contract requires a turn to end before the next prompt is admitted."""

    kind: Literal["turn.opened"] = "turn.opened"
    turn_id: UUID = Field(description="Runner-minted, stable; every operation of this turn repeats it.")
    cause: TurnCause = Field(discriminator="kind", description="What began the exchange.")
    provenance: FrameRange | None = Field(
        description="The frames this opening was projected from; None for a turn opened on no frame"
        " at all, as when the runner brackets turns itself around an admission."
    )


class TurnEnded(_Message):
    kind: Literal["turn.ended"] = "turn.ended"
    turn_id: UUID
    end: TurnEnd = Field(discriminator="outcome", description="How the exchange ended.")
    provenance: FrameRange | None = Field(
        description="The frames this end was projected from; None for a turn ended on no frame at all."
    )


class PromptAdmitted(_Message):
    """The runner injected a Console prompt at its native-input fence.

    The runner never carries prompt text: the Console validates that `prompt_id` is a pending
    prompt of this session's conversation, takes text and origin from its own durable row, and
    materializes the authored prompt item at this position. `after_batch_seq` is the admission
    frontier that survives coalescing: batches coalesce while an ACK is in flight, so operations
    observed on either side of the fence may share this operation's batch, and the frontier pins
    the fence against already-numbered batches rather than leaving it to packaging.
    """

    kind: Literal["prompt.admitted"] = "prompt.admitted"
    prompt_id: UUID = Field(description="The Console's id for the pending prompt, echoed verbatim.")
    after_batch_seq: int | None = Field(
        ge=1,
        description="The last batch whose operations precede the injected prompt in native order;"
        " None when no batch does. The Console materializes the prompt only once its committed"
        " cursor covers this frontier.",
    )
    provenance: FrameRange | None = Field(
        description="The injected native input frame(s), where the injection is itself a numbered"
        " frame; None for an admission that produced none."
    )


class MessageOpen(_Message):
    """The agent began saying something. Its prose follows as segments."""

    item_type: Literal[ItemType.MESSAGE] = ItemType.MESSAGE


class ReasoningOpen(_Message):
    """The agent began thinking. Its own item, a sibling of the message rather than a part of one:
    only Claude nests reasoning inside an assistant message, and a shape that nested it would be
    one backend's promoted upward."""

    item_type: Literal[ItemType.REASONING] = ItemType.REASONING


class ToolCallOpen(_Message):
    """A call was asked, with the arguments it was asked with.

    **Arguments are complete or the call is not opened.** Backends stream arguments as partial
    JSON, so the runner emits this from the frame that finishes them — "a call is being composed"
    is deliberately not expressible, and a consumer learns of a call when there is something true
    to say about it.
    """

    item_type: Literal[ItemType.TOOL_CALL] = ItemType.TOOL_CALL
    tool_name: str
    arguments: dict[str, Json] = Field(description="The complete JSON object the call was asked with.")


type ItemOpen = MessageOpen | ReasoningOpen | ToolCallOpen


class ItemOpened(_Message):
    kind: Literal["item.opened"] = "item.opened"
    item_id: UUID = Field(description="Runner-minted, stable; every segment and completion names it.")
    turn_id: UUID | None = Field(
        default=None, description="The turn this item belongs to; None for one observed outside any turn."
    )
    item: ItemOpen = Field(discriminator="item_type", description="What opened, with the fields its type opens with.")
    backend_item_id: str | None = Field(
        default=None, description="What the native protocol called this item — provenance, never identity."
    )
    provenance: FrameRange


class ItemSegment(_Message):
    """A run of an item's prose that became visible — a message's text, a reasoning summary, a tool
    result's rendered output. The item's whole text is its segments concatenated in journal order,
    and nothing else states it. How finely the runner cuts them is its business: one segment for a
    backend that only produces a final string is as valid as hundreds for one that streams."""

    kind: Literal["item.segment"] = "item.segment"
    item_id: UUID
    text: str = Field(min_length=1)
    provenance: FrameRange


class MessageCompletion(_Message):
    """One agent message, finished. No text: the segments were the text."""

    item_type: Literal[ItemType.MESSAGE] = ItemType.MESSAGE


class ReasoningCompletion(_Message):
    item_type: Literal[ItemType.REASONING] = ItemType.REASONING
    disclosure: ReasoningDisclosure


class ToolCallCompletion(_Message):
    item_type: Literal[ItemType.TOOL_CALL] = ItemType.TOOL_CALL
    outcome: ToolOutcome
    structured: Json = Field(
        description="The exit code, the patch, the MCP structuredContent — an open set of per-tool"
        " shapes; None when the native protocol carried none. Not derivable from the segments: a"
        " rendered result and a tool's own output are different answers."
    )


type ItemCompletion = MessageCompletion | ReasoningCompletion | ToolCallCompletion


class ItemCompleted(_Message):
    kind: Literal["item.completed"] = "item.completed"
    item_id: UUID
    completion: ItemCompletion = Field(
        discriminator="item_type", description="The terminal fields the item's type owns."
    )
    backend_item_id: str | None = Field(
        default=None, description="As on `item.opened`, for a protocol that names the item only when closing it."
    )
    provenance: FrameRange


# One journal, so ordering across kinds is total: an operation's position in its batch, after the
# batches before it, is its position in the conversation.
type Operation = Annotated[
    TurnOpened | TurnEnded | PromptAdmitted | ItemOpened | ItemSegment | ItemCompleted, Field(discriminator="kind")
]


class BatchDiagnostics(_Message):
    """What the runner saw that this projection cannot say, so the default branch stays observable
    without costing an operation per frame."""

    unprojected: dict[str, Annotated[int, Field(ge=1)]] = Field(
        default_factory=dict,
        description="Count of unmapped native frames per frame class, in the runner's own"
        " vocabulary for its backend. Deliberately ignored classes are not in it: the actionable"
        " signal is 'the backend is sending something we do not map'.",
    )


class OperationBatch(_Message):
    """One journal entry: everything the runner has to say since the previous batch.

    The unit of commit, ACK, retention, and replay. The Console commits the operations and the
    seq in one transaction and never partially: a batch it has already committed (a replay after
    reconnect) changes nothing, keyed by `runner_batch_seq` alone.
    """

    kind: Literal["batch"] = "batch"
    # Restated per batch rather than left to the handshake, so the retained replay window stays
    # self-describing across a reconnect that settles a different version.
    neutral_protocol_version: SettledVersion
    runner_batch_seq: int = Field(
        ge=1, description="The runner's dense, monotonic number for this batch, from 1 per session."
    )
    operations: tuple[Operation, ...]
    diagnostics: BatchDiagnostics = Field(default_factory=BatchDiagnostics)

    @model_validator(mode="after")
    def _says_something(self) -> OperationBatch:
        if not self.operations and not self.diagnostics.unprojected:
            raise ValueError("an empty batch spends a seq saying nothing")
        return self


class RunnerHello(_Message):
    """Runner → Console, once, before anything else: what this runner image is.

    The runner speaks first, as on the v3 bridge: negotiation needs a fixed point and the runner
    is the end that cannot adapt — its image is fixed when its claim is created. The Console
    refuses a `generation` other than its own active one outright, and otherwise settles on the
    highest version both ends have.
    """

    kind: Literal["hello"] = "hello"
    generation: str = Field(default=GENERATION, description="The transport generation this image was built for.")
    supported: tuple[int, ...] = Field(
        default=SUPPORTED_NEUTRAL_VERSIONS, description="Every neutral protocol version this image can speak."
    )


class ConsoleResume(_Message):
    """Console → runner, once, in reply: the settled contract and where the journal stands.

    Sent on every connection, reconnects included. `acked_batch_seq` is computed from the
    session's durable cursor rather than any connection state, so two Console replicas adopting
    one runner during a roll answer from the same rows and agree.
    """

    kind: Literal["resume"] = "resume"
    generation: str = Field(default=GENERATION, description="The active transport generation, echoed for the gate.")
    neutral_protocol_version: SettledVersion = Field(
        description="The version the Console settled on after hearing the runner's hello."
    )
    acked_batch_seq: int | None = Field(
        ge=1,
        description="The highest batch the Console has durably committed for this session; None"
        " when it has committed nothing. The runner replays every retained batch above it and"
        " continues numbering from its own tail.",
    )


class BatchAck(_Message):
    """Console → runner: the batch is durably committed, and so is everything before it.

    Cumulative, so one ACK after a burst answers the whole burst and the runner drops every
    retained batch at or below it.
    """

    kind: Literal["ack"] = "ack"
    acked_batch_seq: int = Field(ge=1)


# The two directions carry different messages. Not request/response: after the opening
# hello/resume pair, batches and ACKs flow unpaired — the runner does not wait for an ACK to keep
# emitting, which is what lets batches coalesce only behind an ACK actually in flight.
type RunnerToConsole = RunnerHello | OperationBatch
type ConsoleToRunner = ConsoleResume | BatchAck

# Read with the adapter for the direction you are reading; write with the model's own
# `model_dump_json`.
RUNNER_TO_CONSOLE: TypeAdapter[RunnerToConsole] = TypeAdapter(Annotated[RunnerToConsole, Field(discriminator="kind")])
CONSOLE_TO_RUNNER: TypeAdapter[ConsoleToRunner] = TypeAdapter(Annotated[ConsoleToRunner, Field(discriminator="kind")])
