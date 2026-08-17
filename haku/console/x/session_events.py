"""The stream's two categories as `session_events` rows.

The one place the vocabularies meet the table in <../database_schema.py>: a `ConversationEvent`
from <conversation_events.py> in and a row out, one of the console's own facts about a session in
and a row out, or an accepted prompt in and a row out. Nothing else reads or writes
`session_events.body`, so the stored spelling of an event is settled here and is a boundary shape
rather than a second vocabulary — the events themselves stay dataclasses.

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
    """Which spelling of a result's content a stored row carries."""

    TEXT = "text"
    # CLEANUP(added 2026-08-17): drop both members and their bodies once no `session_events` row
    #   carries either shape. A tool result's content is a string now, so nothing writes them — but
    #   they are history the SPA still renders, so what gets there is a migration rewriting each
    #   row to `{shape: text}`, not a delete that would blank an old session's results. Until then
    #   both stay readable: `ToolResultBody` is parsed on every SPA read of a stored result, so an
    #   arm removed while its rows survive makes reading one raise rather than degrade.
    TOOL_REFERENCES = "tool_references"
    OPAQUE = "opaque"


class TextResultBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shape: Literal[ResultShape.TEXT] = ResultShape.TEXT
    text: str


class ToolReferencesResultBody(BaseModel):
    """Rows written while a result that named tools was its own shape; read, never written."""

    model_config = ConfigDict(extra="forbid")

    shape: Literal[ResultShape.TOOL_REFERENCES] = ResultShape.TOOL_REFERENCES
    tool_names: list[str]


class OpaqueResultBody(BaseModel):
    """Rows written while content with no prose reading was its own shape; read, never written."""

    model_config = ConfigDict(extra="forbid")

    shape: Literal[ResultShape.OPAQUE] = ResultShape.OPAQUE
    payload: Json


type ResultContentBody = TextResultBody | ToolReferencesResultBody | OpaqueResultBody


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

    content: ResultContentBody = Field(discriminator="shape", description="The part a transcript prints.")
    structured: Json = Field(description="The exit code, the patch, the MCP structuredContent — an open set.")
    outcome: Outcome


class PromptBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: UUID = Field(description="The `session_messages` row this prompt is — its only join.")
    text: str


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


type AuthoredBody = PromptRejectedBody | UnreadableInputBody | SessionAdoptedBody | LeaseExpiredBody


def authored(body: AuthoredBody, *, session_id: UUID, now: datetime) -> SessionEvent:
    """The row one of the console's own facts about *session_id* is stored as.

    No frame range because it crossed no wire, and no turn because none of these belongs to an
    exchange — a refused prompt least of all, since what refused it is the turn it is not part of.
    Written in the transaction that makes the fact true, like every other row in this table.
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


def prompt_enqueued(*, session_id: UUID, message_id: UUID, text: str, now: datetime) -> SessionEvent:
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
        body=PromptBody(message_id=message_id, text=text).model_dump(mode="json"),
        created_at=now,
    )


def turn_aborted(*, session_id: UUID, turn_id: UUID, now: datetime) -> SessionEvent:
    """The row the operator's stop is stored as, written in `end_turn`'s own transaction.

    Authored because no frame carries it: the console interrupts the CLI, and the `result` frame
    that comes back says a turn ended without saying who ended it.

    **Written where the turn closes rather than where the abort is asked for**, so it lands after
    the turn's own events and a channel folding the stream in order renders the notice after the
    prose it interrupted.

    The kind and the turn are the whole fact, so the body is empty: who asked is not carried
    (`request_abort` reaches the running replica as a NOTIFY), and when it took effect is
    `created_at`.
    """
    return SessionEvent(
        session_id=session_id,
        turn_id=turn_id,
        kind=AuthoredEventKind.TURN_ABORTED,
        provenance=EventProvenance.AUTHORED,
        source_first_frame_seq=None,
        source_last_frame_seq=None,
        call_id=None,
        body={},
        created_at=now,
    )


def row(event: ConversationEvent, *, session_id: UUID, turn_id: UUID, now: datetime) -> SessionEvent | None:
    """The row *event* is stored as, or None for one whose durable home is elsewhere.

    A `TextDelta` has none: it is an increment of prose the completed message carries whole. A
    `TurnCompleted` has `session_turns`, which already holds the exchange's outcome, its cost and
    its frame bracket.
    """
    if (stored := _stored(event)) is None:
        return None
    kind, body, call_id = stored
    frames = event.provenance if isinstance(event.provenance, FrameRange) else None
    return SessionEvent(
        session_id=session_id,
        turn_id=turn_id,
        kind=kind,
        provenance=EventProvenance.AUTHORED if frames is None else EventProvenance.FRAME_RANGE,
        source_first_frame_seq=None if frames is None else frames.first_frame_seq,
        source_last_frame_seq=None if frames is None else frames.last_frame_seq,
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
