"""The stream's two categories as `conversation_event` rows.

The one place the vocabularies meet the table in <../database_schema.py>: a `ConversationEvent`
from <conversation_events.py>, or one of the console's own facts, in — and a row out. Nothing else
reads or writes `conversation_event.body`, so the stored spelling of an event is settled here.

**Four of an event's fields are columns rather than body, because readers address rows by them**:
the provenance union, the frame range it discriminates, the item the row is about, and the position
itself. What is left is the body, and it is per kind.

**Prose is only ever a segment's.** No completion body carries text, which is what makes
`conversation_item.text` a fold of these rows rather than a second authority for the same prose.

**An item's key is not stored as such.** `ItemKey` groups a fold's output while the fold runs; what
survives it is `conversation_event.item_id`, which the store assigns when it sees the item open.

**No body forbids unknown fields, and none may start.** The console rolls with `maxUnavailable: 0`,
so the release that adds a field to a body writes rows the previous image is still reading; under
`extra="forbid"` every one of them raises in `body_of`, which runs on every row of every
conversation read. A field an older reader ignores is exactly what makes an addition shippable in
one release. The rule and its limits are <../README.md> § Vocabularies across a roll.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from haku.console.chat_models import (
    AuthoredEventKind,
    ConversationEventKind,
    EventProvenance,
    ItemType,
    LeaseExpiryReason,
    PromptOrigin,
    PromptRejection,
    ReasoningDisclosure,
    SessionStatus,
    ToolOutcome,
    TurnOutcome,
)

# The ORM row and the neutral vocabulary's union share a name, because they are the same concept at
# two layers. The row is aliased here so both can be named in one module.
from haku.console.database_schema import ConversationEvent as ConversationEventRow
from haku.console.x.conversation_events import (
    ConversationEvent,
    FrameRange,
    ItemSegment,
    Json,
    MessageCompleted,
    MessageStarted,
    ReasoningCompleted,
    ReasoningStarted,
    ToolCallCompleted,
    ToolCallStarted,
    TurnCompleted,
)
from util.sqlalchemy_types import UnknownValue


class MessageStartedBody(BaseModel):
    """An agent message opened. Its prose arrives as segments."""

    item_type: Literal[ItemType.MESSAGE] = ItemType.MESSAGE


class ReasoningStartedBody(BaseModel):
    item_type: Literal[ItemType.REASONING] = ItemType.REASONING


class ToolCallStartedBody(BaseModel):
    """A call was asked. Its arguments are whole here — a half-composed call is not expressible."""

    item_type: Literal[ItemType.TOOL_CALL] = ItemType.TOOL_CALL
    call_id: str = Field(description="Correlates this call to its answer. Unique within a conversation.")
    tool_name: str
    arguments: dict[str, Json]


class PromptStartedBody(BaseModel):
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


type ItemStartedBody = MessageStartedBody | ReasoningStartedBody | ToolCallStartedBody | PromptStartedBody


class SegmentBody(BaseModel):
    """A run of an item's prose. The item's whole text is these, concatenated in `event_seq` order."""

    text: str


class MessageCompletedBody(BaseModel):
    item_type: Literal[ItemType.MESSAGE] = ItemType.MESSAGE
    backend_item_id: str | None = Field(description="What the frames called this message — provenance, not identity.")


class ReasoningCompletedBody(BaseModel):
    item_type: Literal[ItemType.REASONING] = ItemType.REASONING
    disclosure: ReasoningDisclosure


class ToolCallCompletedBody(BaseModel):
    item_type: Literal[ItemType.TOOL_CALL] = ItemType.TOOL_CALL
    structured: Json = Field(description="The exit code, the patch, the MCP structuredContent — an open set.")
    outcome: ToolOutcome


class PromptCompletedBody(BaseModel):
    """A prompt closes in the same breath it opens: its whole text is known when it is accepted."""

    item_type: Literal[ItemType.PROMPT] = ItemType.PROMPT


type ItemCompletedBody = MessageCompletedBody | ReasoningCompletedBody | ToolCallCompletedBody | PromptCompletedBody


class PromptRejectedBody(BaseModel):
    reason: PromptRejection
    text: str = Field(description="What was said and not delivered — this row is its only copy.")


class UnreadableInputBody(BaseModel):
    media_type: str = Field(description="The channel's own name for what arrived, verbatim: `m.image`, a MIME type.")


class SessionAdoptedBody(BaseModel):
    previous_holder: str | None = Field(
        description="The replica whose lease this one took, or None where the session was unowned."
    )
    holder: str = Field(description="The replica that holds it now — `session_store.REPLICA`.")


class LeaseExpiredBody(BaseModel):
    reason: LeaseExpiryReason
    last_holder: str | None = Field(description="The replica whose lease lapsed, where one held it.")


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


class TurnStartedBody(BaseModel):
    """No fields: the turn this row names and the instant it was written are the whole fact.

    What a channel needs from it is the span to fold into — when the exchange began, so a status
    line can wait out its lazy-creation threshold, and which `turn_id` the events that follow belong
    to. The turn's frame bracket is deliberately absent: frame numbers are one session's, and a
    reader outside the session it came from cannot compare them.
    """


class TurnAnsweredBody(BaseModel):
    """The agent finished the exchange."""

    outcome: Literal[TurnOutcome.ANSWERED] = TurnOutcome.ANSWERED


class TurnAbortedBody(BaseModel):
    """Someone stopped it — an abort is a turn's outcome, which is where every backend protocol
    puts it. It carries no reason because nothing went wrong."""

    outcome: Literal[TurnOutcome.ABORTED] = TurnOutcome.ABORTED


class TurnFailedBody(BaseModel):
    """The exchange could not finish, and why.

    **`failure` is required**, so a failed turn cannot be stored without saying what failed. Three
    bodies rather than one with an optional reason, because that one would also spell an answered
    turn carrying a failure and a failed turn carrying none — the second being exactly the state
    this replaced.
    """

    outcome: Literal[TurnOutcome.FAILED] = TurnOutcome.FAILED
    failure: str = Field(description="Bounded prose for an operator, in the words the runtime used.")


type TurnEndedBody = TurnAnsweredBody | TurnAbortedBody | TurnFailedBody


class SessionProvisioningBody(BaseModel):
    """No fields: that this session is being provisioned for its conversation is the whole fact."""


class SessionEndedBody(BaseModel):
    """How a session ended, kept because the row it is read off is overwritten.

    `sessions.status` and `sessions.error` are the current values, and the session that replaces
    this one is the next thing to write them — so a thread's account of why its predecessor died
    exists only here.
    """

    status: SessionStatus
    error: str | None = Field(description="The sentence the ending path recorded, where it recorded one.")


class SetupNarrationBody(BaseModel):
    """One line the sandbox printed while coming up."""

    text: str


type AuthoredBody = (
    PromptRejectedBody
    | UnreadableInputBody
    | SessionAdoptedBody
    | LeaseExpiredBody
    | SessionProvisioningBody
    | SessionEndedBody
    | SetupNarrationBody
    | TurnStartedBody
    | TurnEndedBody
)

# Every shape `conversation_event.body` is ever read back as. A reader dispatches on these rather
# than on `kind`, which keeps the discriminator and the payload from disagreeing.
#
# `WrittenBody` is what this release can put in a row, so a fold over it is exhaustive and a kind
# added here without an arm fails the type check. `UnknownEventBody` is the arm for a kind added by
# a release *later* than this one — read-only by construction, since nothing here can write a kind
# it cannot name.
type WrittenBody = ItemStartedBody | SegmentBody | ItemCompletedBody | AuthoredBody
type StoredBody = WrittenBody | UnknownEventBody


def authored(
    body: AuthoredBody,
    *,
    conversation_id: UUID,
    event_seq: int,
    session_id: UUID | None,
    turn_id: UUID | None,
    now: datetime,
) -> ConversationEventRow:
    """The row one of the console's own facts is stored as.

    No frame range because it crossed no wire. `session_id` is absent for a fact the conversation
    holds that no session has taken, and `turn_id` is present only on the two that name an
    exchange. Written in the transaction that makes the fact true, like every other row here.
    """
    return ConversationEventRow(
        conversation_id=conversation_id,
        event_seq=event_seq,
        session_id=session_id,
        turn_id=turn_id,
        item_id=None,
        kind=_authored_kind(body),
        provenance=EventProvenance.AUTHORED,
        source_first_frame_seq=None,
        source_last_frame_seq=None,
        body=body.model_dump(mode="json"),
        created_at=now,
    )


def _authored_kind(body: AuthoredBody) -> AuthoredEventKind:
    match body:
        case PromptRejectedBody():
            return AuthoredEventKind.PROMPT_REJECTED
        case UnreadableInputBody():
            return AuthoredEventKind.UNREADABLE_INPUT
        case SessionAdoptedBody():
            return AuthoredEventKind.SESSION_ADOPTED
        case LeaseExpiredBody():
            return AuthoredEventKind.LEASE_EXPIRED
        case SessionProvisioningBody():
            return AuthoredEventKind.SESSION_PROVISIONING
        case SessionEndedBody():
            return AuthoredEventKind.SESSION_ENDED
        case SetupNarrationBody():
            return AuthoredEventKind.SETUP_NARRATION
        case TurnStartedBody():
            return AuthoredEventKind.TURN_STARTED
        case TurnAnsweredBody() | TurnAbortedBody() | TurnFailedBody():
            return AuthoredEventKind.TURN_ENDED


def item_row(
    kind: ConversationEventKind,
    body: BaseModel,
    *,
    conversation_id: UUID,
    event_seq: int,
    item_id: UUID,
    session_id: UUID | None,
    turn_id: UUID | None,
    provenance: FrameRange | None,
    now: datetime,
) -> ConversationEventRow:
    """One row of an item's lifecycle.

    `provenance` is the frame span this was folded from, or None for an item the console authored —
    a prompt, which is accepted before anything crosses a wire. Both arms are legal here, which is
    what separates an item kind from an authored one: the arm follows from what kind of item it is,
    not from which of the three events this is.
    """
    return ConversationEventRow(
        conversation_id=conversation_id,
        event_seq=event_seq,
        session_id=session_id,
        turn_id=turn_id,
        item_id=item_id,
        kind=kind,
        provenance=EventProvenance.FRAME_RANGE if provenance is not None else EventProvenance.AUTHORED,
        source_first_frame_seq=provenance.first_frame_seq if provenance is not None else None,
        source_last_frame_seq=provenance.last_frame_seq if provenance is not None else None,
        body=body.model_dump(mode="json"),
        created_at=now,
    )


def body_of(row: ConversationEventRow) -> StoredBody:
    """What a stored row says, back in the shape it was written from.

    The read half of `item_row` and `authored`, and the only one there is. A kind added without an
    arm fails the type check rather than the read.

    A kind added by a **newer release than this one** cannot be type-checked against, so it takes
    the `UnknownValue` arm instead of raising: the previous image reads every row of a conversation
    for the length of a roll, and one row it has no words for must not cost it the rest.
    """
    match row.kind:
        case UnknownValue():
            return UnknownEventBody(kind=row.kind.value, body=row.body)
        case ConversationEventKind.ITEM_STARTED:
            return _started_body(row.body)
        case ConversationEventKind.ITEM_SEGMENT:
            return SegmentBody.model_validate(row.body)
        case ConversationEventKind.ITEM_COMPLETED:
            return _completed_body(row.body)
        case AuthoredEventKind.PROMPT_REJECTED:
            return PromptRejectedBody.model_validate(row.body)
        case AuthoredEventKind.UNREADABLE_INPUT:
            return UnreadableInputBody.model_validate(row.body)
        case AuthoredEventKind.SESSION_ADOPTED:
            return SessionAdoptedBody.model_validate(row.body)
        case AuthoredEventKind.LEASE_EXPIRED:
            return LeaseExpiredBody.model_validate(row.body)
        case AuthoredEventKind.TURN_STARTED:
            return TurnStartedBody.model_validate(row.body)
        case AuthoredEventKind.TURN_ENDED:
            return _turn_ended_body(row.body)
        case AuthoredEventKind.SESSION_PROVISIONING:
            return SessionProvisioningBody.model_validate(row.body)
        case AuthoredEventKind.SESSION_ENDED:
            return SessionEndedBody.model_validate(row.body)
        case AuthoredEventKind.SETUP_NARRATION:
            return SetupNarrationBody.model_validate(row.body)


def _turn_ended_body(body: dict[str, Any]) -> TurnEndedBody:
    """Dispatched on the body's own `outcome`, as `_started_body` dispatches on `item_type`."""
    match body.get("outcome"):
        case TurnOutcome.ANSWERED:
            return TurnAnsweredBody.model_validate(body)
        case TurnOutcome.ABORTED:
            return TurnAbortedBody.model_validate(body)
        case TurnOutcome.FAILED:
            return TurnFailedBody.model_validate(body)
        case other:
            raise ValueError(f"turn_ended names no known outcome: {other=}")


def _started_body(body: dict[str, Any]) -> ItemStartedBody:
    """Dispatched on the body's own `item_type` rather than on the row's kind.

    The three item kinds say only where in a lifecycle a row sits; which shape its body has is the
    item's type, which the log carries in the body because `conversation_event` does not join to
    the item to read one.
    """
    match body.get("item_type"):
        case ItemType.MESSAGE:
            return MessageStartedBody.model_validate(body)
        case ItemType.REASONING:
            return ReasoningStartedBody.model_validate(body)
        case ItemType.TOOL_CALL:
            return ToolCallStartedBody.model_validate(body)
        case ItemType.PROMPT:
            return PromptStartedBody.model_validate(body)
        case other:
            raise ValueError(f"item_started names no known item type: {other=}")


def _completed_body(body: dict[str, Any]) -> ItemCompletedBody:
    match body.get("item_type"):
        case ItemType.MESSAGE:
            return MessageCompletedBody.model_validate(body)
        case ItemType.REASONING:
            return ReasoningCompletedBody.model_validate(body)
        case ItemType.TOOL_CALL:
            return ToolCallCompletedBody.model_validate(body)
        case ItemType.PROMPT:
            return PromptCompletedBody.model_validate(body)
        case other:
            raise ValueError(f"item_completed names no known item type: {other=}")


def stored(event: ConversationEvent) -> tuple[ConversationEventKind, BaseModel] | None:
    """The kind and body *event* is written as, or None for one whose durable home is elsewhere.

    A `TurnCompleted` has none: `conversation_turn` already holds the exchange's outcome and its
    frame bracket, and the log states the two ends as authored rows.

    **Every event that reaches a row here is frame-derived**, so one carrying `Authored` is an
    adapter that did not say where it read the fact. The caller checks that, because it is the one
    holding the provenance.
    """
    match event:
        case TurnCompleted():
            return None
        case MessageStarted():
            return ConversationEventKind.ITEM_STARTED, MessageStartedBody()
        case ReasoningStarted():
            return ConversationEventKind.ITEM_STARTED, ReasoningStartedBody()
        case ToolCallStarted():
            asked = ToolCallStartedBody(
                call_id=event.call_id, tool_name=event.tool_name, arguments=dict(event.arguments)
            )
            return ConversationEventKind.ITEM_STARTED, asked
        case ItemSegment():
            return ConversationEventKind.ITEM_SEGMENT, SegmentBody(text=event.text)
        case MessageCompleted():
            return ConversationEventKind.ITEM_COMPLETED, MessageCompletedBody(backend_item_id=event.backend_item_id)
        case ReasoningCompleted():
            return ConversationEventKind.ITEM_COMPLETED, ReasoningCompletedBody(disclosure=event.disclosure)
        case ToolCallCompleted():
            answered = ToolCallCompletedBody(structured=event.structured, outcome=event.outcome)
            return ConversationEventKind.ITEM_COMPLETED, answered
