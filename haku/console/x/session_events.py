"""The stream's two categories as `session_events` rows.

The one place the vocabularies meet the table in <../database_schema.py>: a `ConversationEvent`
from <conversation_events.py>, one of the console's own facts about a session, or an accepted
prompt, in — and a row out. Nothing else reads or writes `session_events.body`, so the stored
spelling of an event is settled here.

**Three of an event's fields are columns rather than body, because readers address rows by them**:
the provenance union, the frame range it discriminates, and a tool call's `call_id`. What is left
is the body, and it is per kind.

**The message key is not stored.** `MessageKey` groups a fold's output while the fold runs; what
survives it is the frame range, which is also what `session_messages` records its own span in and
so what joins the two tables.

**An authored row may name a turn**, and one kind does: an abort is the operator stopping an
exchange. `reprojection.check_session` therefore filters the arm out rather than relying on its
per-turn read never seeing one.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from haku.console.chat_models import (
    AuthoredEventKind,
    ConversationEventKind,
    EventProvenance,
    LeaseExpiryReason,
    PromptOrigin,
    PromptRejection,
)
from haku.console.database_schema import SessionEvent
from haku.console.x.conversation_events import (
    ConversationEvent,
    FrameRange,
    Json,
    MessageCompleted,
    Outcome,
    Reasoning,
    TextDelta,
    ToolCallCompleted,
    ToolCallStarted,
    TurnCompleted,
)


class ResultShape(StrEnum):
    """Which spelling of a result's content a stored row carries.

    One member, and it stays an enum because every stored row carries the discriminator and
    `TextResultBody` forbids extras.
    """

    TEXT = "text"


class TextResultBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shape: Literal[ResultShape.TEXT] = ResultShape.TEXT
    text: str


class MessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(description="The message's prose, joined. None for one that was all thinking and tools.")
    agent_message_id: str | None = Field(description="What the frames called this message — provenance, not identity.")


class ReasoningBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str | None


class ToolCallBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str
    arguments: dict[str, Json]


class ToolResultBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: TextResultBody = Field(description="The part a transcript prints.")
    structured: Json = Field(description="The exit code, the patch, the MCP structuredContent — an open set.")
    outcome: Outcome


class PromptBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: UUID = Field(description="The `session_messages` row this prompt is — its only join.")
    text: str
    # What reads this is cross-surface prompt visibility: every attached surface shows the
    # operator's prompts wherever they were sent from, so each asks "did this arrive through me?"
    # — an equality test against the origin, never a look inside one — and posts only what did not.
    # Nothing projects it yet; that is the step where a channel starts reading the record instead
    # of being handed what the turn loop produced (#4254 § 9 step 9).
    #
    # **Required, with no default**, because every default is a lie a reader acts on: guessing the
    # SPA tells an attached room it owes a copy of a prompt it may already be showing. `0078`
    # deleted the rows that had no key for this and constrains the table so none can come back, so
    # a body missing it is a bug rather than an era.
    origin: PromptOrigin = Field(
        discriminator="kind", description="The surface this prompt arrived through, and its own address for it."
    )


class PromptRejectedBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: PromptRejection
    text: str = Field(description="What was said and not delivered — this row is its only copy.")


class UnreadableInputBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    media_type: str = Field(description="The channel's own name for what arrived, verbatim: `m.image`, a MIME type.")


class SessionAdoptedBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    previous_holder: str | None = Field(
        description="The replica whose lease this one took, or None where the session was unowned."
    )
    holder: str = Field(description="The replica that holds it now — `session_store.REPLICA`.")


class LeaseExpiredBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: LeaseExpiryReason
    last_holder: str | None = Field(description="The replica whose lease lapsed, where one held it.")


class TurnAbortedBody(BaseModel):
    """No fields: the kind and the turn it names are the whole fact (see `turn_aborted`)."""

    model_config = ConfigDict(extra="forbid")


type AuthoredBody = PromptRejectedBody | UnreadableInputBody | SessionAdoptedBody | LeaseExpiredBody

# Every shape `session_events.body` is ever written from. A reader dispatches on these rather than
# on `kind`, which keeps the discriminator and the payload from disagreeing.
type StoredBody = (
    MessageBody
    | ReasoningBody
    | ToolCallBody
    | ToolResultBody
    | PromptBody
    | TurnAbortedBody
    | PromptRejectedBody
    | UnreadableInputBody
    | SessionAdoptedBody
    | LeaseExpiredBody
)


def authored(body: AuthoredBody, *, session_id: UUID, now: datetime) -> SessionEvent:
    """The row one of the console's own facts about *session_id* is stored as.

    No frame range because it crossed no wire, and no turn because none of these belongs to an
    exchange. Written in the transaction that makes the fact true, like every other row here.
    """
    return SessionEvent(
        session_id=session_id,
        turn_id=None,
        kind=_authored_kind(body),
        provenance=EventProvenance.AUTHORED,
        source_first_frame_seq=None,
        source_last_frame_seq=None,
        call_id=None,
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


def prompt_enqueued(
    *, session_id: UUID, message_id: UUID, text: str, origin: PromptOrigin, now: datetime
) -> SessionEvent:
    """The row an accepted prompt is stored as, written in `enqueue_prompt`'s own transaction.

    Authored because no frame carries it at this point: `next_prompt` hands the prompt to the CLI
    later, and a session that ends first never hands it over at all.

    **No turn**, and not for the reason an authored session fact has none: admission refuses a
    prompt while a turn is open, so at this moment there is no turn to name.
    """
    return SessionEvent(
        session_id=session_id,
        turn_id=None,
        kind=AuthoredEventKind.PROMPT_ENQUEUED,
        provenance=EventProvenance.AUTHORED,
        source_first_frame_seq=None,
        source_last_frame_seq=None,
        call_id=None,
        body=PromptBody(message_id=message_id, text=text, origin=origin).model_dump(mode="json"),
        created_at=now,
    )


def turn_aborted(*, session_id: UUID, turn_id: UUID, now: datetime) -> SessionEvent:
    """The row the operator's stop is stored as, written in `end_turn`'s own transaction.

    Authored because no frame carries it: the console interrupts the CLI, and the `result` frame
    that comes back says a turn ended without saying who ended it.

    **Written where the turn closes rather than where the abort is asked for**, so it lands after
    the turn's own events and a channel folding the stream in order renders the notice after the
    prose it interrupted.

    The kind and the turn are the whole fact, so the body is empty: who asked is not carried, and
    when it took effect is `created_at`.
    """
    return SessionEvent(
        session_id=session_id,
        turn_id=turn_id,
        kind=AuthoredEventKind.TURN_ABORTED,
        provenance=EventProvenance.AUTHORED,
        source_first_frame_seq=None,
        source_last_frame_seq=None,
        call_id=None,
        body=TurnAbortedBody().model_dump(mode="json"),
        created_at=now,
    )


def body_of(row: SessionEvent) -> StoredBody:
    """What a stored row says, back in the shape it was written from.

    The read half of `row` and `authored`, and the only one there is. A kind added without an arm
    fails the type check rather than the read.
    """
    match row.kind:
        case ConversationEventKind.MESSAGE_COMPLETED:
            return MessageBody.model_validate(row.body)
        case ConversationEventKind.REASONING:
            return ReasoningBody.model_validate(row.body)
        case ConversationEventKind.TOOL_CALL_STARTED:
            return ToolCallBody.model_validate(row.body)
        case ConversationEventKind.TOOL_CALL_COMPLETED:
            return ToolResultBody.model_validate(row.body)
        case AuthoredEventKind.PROMPT_ENQUEUED:
            return PromptBody.model_validate(row.body)
        case AuthoredEventKind.TURN_ABORTED:
            return TurnAbortedBody.model_validate(row.body)
        case AuthoredEventKind.PROMPT_REJECTED:
            return PromptRejectedBody.model_validate(row.body)
        case AuthoredEventKind.UNREADABLE_INPUT:
            return UnreadableInputBody.model_validate(row.body)
        case AuthoredEventKind.SESSION_ADOPTED:
            return SessionAdoptedBody.model_validate(row.body)
        case AuthoredEventKind.LEASE_EXPIRED:
            return LeaseExpiredBody.model_validate(row.body)


def row(event: ConversationEvent, *, session_id: UUID, turn_id: UUID, now: datetime) -> SessionEvent | None:
    """The row *event* is stored as, or None for one whose durable home is elsewhere.

    A `TextDelta` has none: it is an increment of prose the completed message carries whole. A
    `TurnCompleted` has `session_turns`, which already holds the exchange's outcome, its cost and
    its frame bracket.

    **Every kind that reaches a row here is frame-derived**, so an event carrying `Authored` is an
    adapter that did not say where it read the fact, and it raises rather than taking the other
    arm. Written instead, the failure would land on the read: `session_views._asked` runs on every
    `SessionStore.get`, and one such row makes a session's whole transcript unreadable.
    """
    if (stored := _stored(event)) is None:
        return None
    kind, body, call_id = stored
    if not isinstance(frames := event.provenance, FrameRange):
        raise ValueError(f"{kind} is projected from frames and names none: {event=}")
    return SessionEvent(
        session_id=session_id,
        turn_id=turn_id,
        kind=kind,
        provenance=EventProvenance.FRAME_RANGE,
        source_first_frame_seq=frames.first_frame_seq,
        source_last_frame_seq=frames.last_frame_seq,
        call_id=call_id,
        body=body.model_dump(mode="json"),
        created_at=now,
    )


def _stored(event: ConversationEvent) -> tuple[ConversationEventKind, BaseModel, str | None] | None:
    match event:
        case TextDelta() | TurnCompleted():
            return None
        case MessageCompleted():
            body = MessageBody(text=event.text, agent_message_id=event.agent_message_id)
            return ConversationEventKind.MESSAGE_COMPLETED, body, None
        case Reasoning():
            return ConversationEventKind.REASONING, ReasoningBody(summary=event.summary), None
        case ToolCallStarted():
            call = ToolCallBody(tool_name=event.tool_name, arguments=dict(event.arguments))
            return ConversationEventKind.TOOL_CALL_STARTED, call, event.call_id
        case ToolCallCompleted():
            result = ToolResultBody(
                content=TextResultBody(text=event.content), structured=event.structured, outcome=event.outcome
            )
            return ConversationEventKind.TOOL_CALL_COMPLETED, result, event.call_id
