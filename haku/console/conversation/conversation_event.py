"""The conversation-event vocabulary: every shape a `conversation_event` row's body is written or
read as, and the enums the row's own columns discriminate on.

One vocabulary, in the neutral-operation verbs (#4667): items are **opened / segment / completed**,
a turn is **opened** and **ended**, and a failed turn states its **`failure`**. The runner's wire
spelling of the same verbs is `haku/runtime/x/bridge/neutral_operations.py` — its own module
because the Console depends on the runtime and never the reverse, so the wire cannot import this
record layer.

**Four of an event's fields are columns rather than body, because readers address rows by them**:
the provenance union, the frame range it discriminates, the item the row is about, and the position
itself. What is left is the body, and it is per kind.

**Prose is only ever a segment's.** No completion body carries text, which is what makes
`conversation_item.text` a fold of these rows rather than a second authority for the same prose.

**No body forbids unknown fields, and none may start.** The console rolls with `maxUnavailable: 0`,
so the release that adds a field to a body writes rows the previous image is still reading; under
`extra="forbid"` every one of them raises in `conversation_log.body_of`, which runs on every row of
every conversation read. A field an older reader ignores is exactly what makes an addition
shippable in one release. The rule and its limits are <../README.md> § Vocabularies across a roll.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from haku.console.chat_models import ItemType, ToolOutcome
from haku.console.conversation.prompt_origin import PromptOrigin
from haku.console.session.status import LeaseExpiryReason, SessionStatus

# Whatever a provider put in a field this layer passes through rather than reads. Open by nature:
# a tool's structured result is per-tool, not per-protocol.
type Json = None | bool | int | float | str | list[Json] | dict[str, Json]


class ConversationEventKind(StrEnum):
    """The item lifecycle, as log rows.

    **These three take either provenance arm**, which is what separates them from
    `AuthoredEventKind`: an assistant message is folded out of frames, a prompt is a console fact
    that crossed no wire, and both are items with the same three-row lifecycle. Which arm a given
    row may take follows from the item's `item_type`, not from the kind.

    **Prose exists only as segments, and a completion carries none.** A backend that streams has
    its adapter cut the stream into `ITEM_SEGMENT` rows; one that produces only a final string
    emits exactly one segment and then completes. So an item's text is the concatenation of its
    segments by construction, and a consumer replaying from a position can never reprint prose it
    already printed.
    """

    ITEM_OPENED = "item_opened"
    ITEM_SEGMENT = "item_segment"
    ITEM_COMPLETED = "item_completed"


class AuthoredEventKind(StrEnum):
    """What a row records that no frame carries — the other category.

    **The console is the only witness**, so these carry `EventProvenance.AUTHORED` and no frame
    range. That is the whole membership test: not whether the fact is about the session rather than
    the conversation, but whether it reached the console over the wire.

    A prompt is **not** here, and that is a change from what this arm used to hold: a prompt is an
    item like any other, so it takes the item lifecycle in `ConversationEventKind` and carries this
    arm's provenance. The reason it is authored is unchanged — a prompt is accepted before it is
    asked, since a session holds no sandbox until a prompt buys one — but that is a fact about
    which arm its rows take, not about what kind of thing it is.

    An abort is likewise gone: it is a turn's `outcome`, which is where every backend puts it.

    Several members **name their turn**: the exchange's own two ends. So a reader of this arm
    cannot assume `turn_id IS NULL`.

    **A turn's two ends are here rather than in `ConversationEventKind`, and the bracket is why.**
    A turn is the console's construct, not the wire's: it is a range because the CLI folds a prompt
    sent mid-turn into the running one, so one `result` frame can answer two of these. A turn opens
    before anything crosses the wire, and closes on no frame at all when it failed or was aborted
    before its `result` arrived. So neither end can name the frames it was read from, which is what
    the frame-derived arm requires.
    """

    # A prompt admission refused. Recorded rather than only announced because the refusal is
    # terminal — the message is not delivered and is not coming back — so this row is the only
    # copy of what was said, and it is written in the transaction that acknowledges the message to
    # the channel it arrived on.
    PROMPT_REJECTED = "prompt_rejected"
    # Something arrived on a channel that the console has no way to read: an image, a voice memo,
    # a msgtype invented after this release. One row per event.
    UNREADABLE_INPUT = "unreadable_input"
    SESSION_ADOPTED = "session_adopted"
    LEASE_EXPIRED = "lease_expired"
    # A sandbox is being provisioned for this thread. The only account of it today is the Matrix
    # supervisor's stack frame, so a thread whose session failed before a room was bound has none.
    SESSION_PROVISIONING = "session_provisioning"
    # How a session ended, with the reason it ended for. The session row states only its own end,
    # so without this row the conversation's account of why a *replaced* predecessor died lives
    # nowhere a reader of the stream can see.
    SESSION_ENDED = "session_ended"
    # One line the sandbox printed while coming up. A `SetupOutput` envelope does cross the wire, but
    # what is stored is one decoded line of it rather than the frame, so the console is the witness
    # to the row (`session/setup_output.py`).
    SETUP_NARRATION = "setup_narration"
    # The two ends of one exchange, as the stream states them: without these a reader outside the
    # session has to open `conversation_turn` to know a turn is running.
    TURN_OPENED = "turn_opened"
    TURN_ENDED = "turn_ended"


# What `conversation_event.kind` holds, over both categories of the one ordered stream.
type StoredEventKind = ConversationEventKind | AuthoredEventKind


class EventProvenance(StrEnum):
    """Which arm of an event's provenance a stored row carries.

    A discriminator rather than a nullable frame range. An event the console authored crossed no
    wire and never will, so "no frames" and "frames not recorded" are different states, where on
    `session_messages` both are NULL and no constraint can tell them apart (#4143).
    """

    FRAME_RANGE = "frame_range"
    AUTHORED = "authored"


class TurnOutcome(StrEnum):
    """How one exchange ended. Absent while it is still running.

    A turn is one exchange — the harness handing the agent a prompt through to a final answer
    or a failure — containing many assistant messages, many tool uses and many model round
    trips. It is deliberately not the CLI's own `num_turns`, which counts those round trips and
    so lives *inside* one of these.
    """

    ANSWERED = "answered"
    ABORTED = "aborted"
    FAILED = "failed"


class ReasoningDisclosure(StrEnum):
    """How much of a reasoning item's thinking the backend actually handed back.

    No backend we adapt returns raw chain of thought — Anthropic returns summarised thinking,
    OpenAI a generated summary over content it keeps encrypted, Codex a summary too — so the
    distinction worth storing is not summary-versus-reasoning but whether anything was disclosed
    at all. Without it a withheld item is an empty string no surface can explain.
    """

    SUMMARY = "summary"
    WITHHELD = "withheld"


@dataclass(frozen=True, slots=True)
class FrameRange:
    """The inclusive span of provider frames one event was projected from.

    A span, not a set: a message interrupted by a tool result spans the interruption too. Stored
    as the row's own `source_first_frame_seq`/`source_last_frame_seq` columns rather than in any
    body, so an operator can appeal any event to the raw JSON that produced it — a dataclass, not
    a model, because it never crosses a serialization boundary itself.
    """

    first_frame_seq: int
    last_frame_seq: int


class MessageOpened(BaseModel):
    """An agent message opened. Its prose arrives as segments."""

    item_type: Literal[ItemType.MESSAGE] = ItemType.MESSAGE


class ReasoningOpened(BaseModel):
    item_type: Literal[ItemType.REASONING] = ItemType.REASONING


class ToolCallOpened(BaseModel):
    """A call was asked. Its arguments are whole here — a half-composed call is not expressible."""

    item_type: Literal[ItemType.TOOL_CALL] = ItemType.TOOL_CALL
    call_id: str = Field(description="Correlates this call to its answer. Unique within a conversation.")
    tool_name: str
    arguments: dict[str, Json]


class PromptOpened(BaseModel):
    """A prompt was accepted. Authored: it is admitted before it crosses any wire."""

    item_type: Literal[ItemType.PROMPT] = ItemType.PROMPT
    # What reads this is cross-surface prompt visibility: every attached surface shows the
    # operator's prompts wherever they were sent from, so each asks "did this arrive through me?"
    # — an equality test against the origin, never a look inside one — and posts only what did not.
    #
    # **Required, with no default**, because every default is a lie a reader acts on: guessing the
    # SPA tells an attached room it owes a copy of a prompt it may already be showing.
    origin: PromptOrigin = Field(
        discriminator="kind", description="The surface this prompt arrived through, and its own address for it."
    )


type ItemOpened = MessageOpened | ReasoningOpened | ToolCallOpened | PromptOpened


class ItemSegment(BaseModel):
    """A run of an item's prose. The item's whole text is these, concatenated in `event_seq` order."""

    text: str


class MessageCompleted(BaseModel):
    item_type: Literal[ItemType.MESSAGE] = ItemType.MESSAGE
    backend_item_id: str | None = Field(description="What the frames called this message — provenance, not identity.")


class ReasoningCompleted(BaseModel):
    item_type: Literal[ItemType.REASONING] = ItemType.REASONING
    disclosure: ReasoningDisclosure


class ToolCallCompleted(BaseModel):
    item_type: Literal[ItemType.TOOL_CALL] = ItemType.TOOL_CALL
    structured: Json = Field(description="The exit code, the patch, the MCP structuredContent — an open set.")
    outcome: ToolOutcome


class PromptCompleted(BaseModel):
    """A prompt closes in the same breath it opens: its whole text is known when it is accepted."""

    item_type: Literal[ItemType.PROMPT] = ItemType.PROMPT


type ItemCompleted = MessageCompleted | ReasoningCompleted | ToolCallCompleted | PromptCompleted


class PromptRejection(StrEnum):
    """Which state refused a prompt, in the vocabulary a surface answers its sender in.

    Admission is `enqueue_prompt`'s decision under `SELECT … FOR UPDATE`, and a refusal is
    terminal: nothing queues behind it and the sender is told to send again. The member is what
    the answer says, so it is coarse on purpose — an operator acts on "Haku is busy" and on "there
    is nothing behind this room yet" differently, and on nothing finer.
    """

    # No session behind the surface at all: none has ever been provisioned, or the row has gone
    # while the supervisor mints its replacement. Recorded like every other refusal — what a
    # rejection is about is the conversation, which exists whether or not a session does.
    NO_SESSION = "no_session"
    SESSION_NOT_READY = "session_not_ready"
    TURN_IN_FLIGHT = "turn_in_flight"
    PROMPT_QUEUED = "prompt_queued"


class PromptRejected(BaseModel):
    reason: PromptRejection
    text: str = Field(description="What was said and not delivered — this row is its only copy.")


class UnreadableInput(BaseModel):
    media_type: str = Field(description="The channel's own name for what arrived, verbatim: `m.image`, a MIME type.")


class SessionAdopted(BaseModel):
    previous_holder: str | None = Field(
        description="The replica whose lease this one took, or None where the session was unowned."
    )
    holder: str = Field(description="The replica that holds it now — `session_store.REPLICA`.")


class LeaseExpired(BaseModel):
    reason: LeaseExpiryReason
    last_holder: str | None = Field(description="The replica whose lease lapsed, where one held it.")


class SessionProvisioning(BaseModel):
    """No fields: that this session is being provisioned for its conversation is the whole fact."""


class SessionEnded(BaseModel):
    """How a session ended, kept because the row it is read off is overwritten.

    `sessions.status` and `sessions.error` are the current values, and the session that replaces
    this one is the next thing to write them — so a thread's account of why its predecessor died
    exists only here.
    """

    status: SessionStatus
    error: str | None = Field(description="The sentence the ending path recorded, where it recorded one.")


class SetupNarration(BaseModel):
    """One line the sandbox printed while coming up."""

    text: str


class TurnOpened(BaseModel):
    """No fields: the turn this row names and the instant it was written are the whole fact.

    What a channel needs from it is the span to fold into — when the exchange began, so a status
    line can wait out its lazy-creation threshold, and which `turn_id` the events that follow belong
    to. The turn's frame bracket is deliberately absent: frame numbers are one session's, and a
    reader outside the session it came from cannot compare them.
    """


class TurnAnswered(BaseModel):
    """The agent finished the exchange."""

    outcome: Literal[TurnOutcome.ANSWERED] = TurnOutcome.ANSWERED


class TurnAborted(BaseModel):
    """Someone stopped it — an abort is a turn's outcome, which is where every backend protocol
    puts it. It carries no reason because nothing went wrong."""

    outcome: Literal[TurnOutcome.ABORTED] = TurnOutcome.ABORTED


class TurnFailed(BaseModel):
    """The exchange could not finish, and why.

    **`failure` is required**, so a failed turn cannot be stored without saying what failed. Three
    bodies rather than one with an optional reason, because that one would also spell an answered
    turn carrying a failure and a failed turn carrying none — the second being exactly the state
    this replaced.
    """

    outcome: Literal[TurnOutcome.FAILED] = TurnOutcome.FAILED
    failure: str = Field(description="Bounded prose for an operator, in the words the runtime used.")


type TurnEnd = TurnAnswered | TurnAborted | TurnFailed


type AuthoredEvent = (
    PromptRejected
    | UnreadableInput
    | SessionAdopted
    | LeaseExpired
    | SessionProvisioning
    | SessionEnded
    | SetupNarration
    | TurnOpened
    | TurnEnd
)


@dataclass(frozen=True, slots=True)
class UnknownEventBody:
    """A row of a kind this release has no words for: what a reader older than its writer reads.

    Not a Pydantic model, because it is never written and never parsed — it is minted on read and
    carries the row through unread. The kind is kept as the string it was stored as, and the body
    unparsed, so a reader that logs one says which vocabulary it was missing.

    **A reader must decide what to do with one, and skipping is not free.** A surface rendering the
    stream can leave it out; a reader deciding something from the stream cannot, because "a kind I
    do not know" and "no such event" are the same to it and only one of them is true
    (<../README.md> § Vocabularies across a roll).
    """

    kind: str
    body: dict[str, Any]


# Every shape `conversation_event.body` is ever written or read back as. A reader dispatches on
# these rather than on `kind`, which keeps the discriminator and the payload from disagreeing.
#
# `ConversationEvent` is what this release can put in a row, so a fold over it is exhaustive and a
# kind added here without an arm fails the type check. `UnknownEventBody` is the arm for a kind
# added by a release *later* than this one — read-only by construction, since nothing here can
# write a kind it cannot name.
type ConversationEvent = ItemOpened | ItemSegment | ItemCompleted | AuthoredEvent
type StoredEvent = ConversationEvent | UnknownEventBody
