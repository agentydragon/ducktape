"""The item read models a conversation read hands back, and the fold that produces them.

<reads.py> holds the other conversation reads — a session, a frame, a turn — and the cursors that
page them; this module holds the item read: what one `conversation_item` row looks like on the
wire, and the single fold every reader consumes to get there. An item is the row with its wire
face on: the row's `item_type` as the item's kind, the row's lifecycle as its `status`, the prose
as it stands, and the frame span its content was read off as `provenance`. The rows are folds of
the log the writer keeps — asserted so by <reprojection.py> — which is what lets a page be served
by a keyset read instead of refolding the conversation from its first row.

**An item sits at the row's opening position for its whole life.** `opened_seq` is allocated when
the item opens and never moves, so paging is stable while an item is still being written: a later
read serves the same position with the row's newer state — more prose, an answer, a settled
`status` — never a second item somewhere else. What a page holds is therefore the rows as of the
read, and re-reading any position is always correct.

**Pydantic rather than dataclasses, because the boundary needs it.** Every model here is either an
MCP tool's return type, whose JSON schema is generated from the class, or is carried in one. Two
surfaces serialise these: <../tools/conversations.py> over MCP, and the SPA's wire shapes in
<../session/conversation_views.py>. They live beside the store that produces them (the sole
producer), private to the conversation read surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, TypeAdapter

from haku.console.chat_models import ItemStatus, ItemType, ToolOutcome
from haku.console.conversation.conversation_event import (
    ReasoningDisclosure,
    TurnAborted,
    TurnAnswered,
    TurnEnd,
    TurnFailed,
    TurnOutcome,
)
from haku.console.conversation.prompt_origin import PromptOrigin, PromptOriginKind
from haku.console.database_schema import ConversationItem, ConversationTurn


class FromFrames(BaseModel):
    """The frames this item was read off, inclusive at both ends.

    Inclusive of everything between the ends, which is not the same as "these frames and no
    others": a message whose frames are interrupted by a tool result spans the interruption too,
    and that is the honest reading of a range rather than a defect in it.

    This is the appeal path. `read_session_frames(session_id, cursor=first_frame_seq)` walks the span,
    and with `limit=1` returns exactly the first frame. Frames are session-level while a
    conversation spans replaced sessions, so the range names its session.
    """

    kind: Literal["frames"] = "frames"
    session_id: UUID = Field(
        description="Whose wire log the range indexes — pass to `read_session_frames` unchanged. A conversation's "
        "items span replaced sessions, and a frame number means nothing without its session."
    )
    first_frame_seq: int
    last_frame_seq: int


class ConsoleAuthored(BaseModel):
    """The console said this itself, so there is no frame to appeal to and there never will be.

    Distinct in kind from a frame-derived item whose range happens to be unknown: re-reading the
    frames can only preserve one of these, never re-derive it.
    """

    kind: Literal["authored"] = "authored"


type ItemProvenance = Annotated[FromFrames | ConsoleAuthored, Field(discriminator="kind")]


class _ItemBase(BaseModel):
    """What every conversation item carries: one `conversation_item` row, minimally wrapped.

    `opened_seq` is the row's opening position in the conversation's event stream — its position
    for its whole life, however its state settles — so paging is stable while an item is still
    being written. `status` is the row's lifecycle exactly as stored: `open` is still being
    written and its prose fields are as of this read; `failed` is an item its session died
    around, kept with whatever had been said.
    """

    opened_seq: int = Field(
        description="The stream position the item was opened at — the row's position for its whole life, "
        "and what `read_conversation_items`'s `cursor` names. Items are sparse in the stream, since most "
        "stream rows build an item rather than opening one."
    )
    closed_seq: int | None = Field(
        description="The stream position the item settled at; absent exactly while it is still open."
    )
    status: ItemStatus = Field(
        description="The row's lifecycle as stored: `open` is still being written (prose as of this read), "
        "`failed` was cut off by its session dying, `complete` is settled."
    )
    provenance: ItemProvenance


class PromptItem(_ItemBase):
    """What the session was asked, and in whose voice.

    On the record because a conversation without its questions is half of one. Console authored,
    always, and complete from admission: a prompt's whole text is known when it is accepted.
    """

    kind: Literal["prompt"] = "prompt"
    text: str
    origin: PromptOriginKind = Field(
        description="Who asked: `spa` or `matrix` for the operator, through the console or a room; `harness` "
        "for the agent resuming its own session, which nobody typed."
    )


class MessageItem(_ItemBase):
    """One agent message: the concatenation of the prose that has arrived for it.

    `backend_item_id` is provenance, not identity: it is what the frames called this message, and it
    is absent whenever the wire did not supply one — including while the message is still open.
    """

    kind: Literal["message"] = "message"
    text: str
    backend_item_id: str | None


class ReasoningItem(_ItemBase):
    """The agent thought, as the row records it.

    Its own item rather than part of a message: only Claude nests thinking inside an assistant
    message, and a shape that nested it would be one backend's promoted upward.

    `disclosure` is what the backend let the record keep: `withheld` means `text` is not the
    thought (Claude's `redacted_thinking` discloses nothing); absent while the row is still open.
    """

    kind: Literal["reasoning"] = "reasoning"
    text: str
    disclosure: ReasoningDisclosure | None


class ToolCallItem(_ItemBase):
    """One tool call, whole: the ask, and the answer where one has arrived.

    The ask and the answer are one row and so one item — `status` says whether the answer has
    arrived, rather than a separate result item a reader would have to join back.
    """

    kind: Literal["tool_call"] = "tool_call"
    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    content: str = Field(
        description="The answer as text — what the provider sent where it sent prose, and its JSON otherwise. "
        "Empty until the call is answered; `provenance` names the frames to read the original blocks from."
    )
    structured: Any = Field(
        default=None, description="The call's structured output, verbatim; absent when it had none."
    )
    outcome: ToolOutcome | None = Field(
        description="How the call went, once answered: `unknown` where the provider reported neither way. "
        "None exactly while no answer has arrived — a call still running, or one its session died around."
    )


type Item = Annotated[PromptItem | MessageItem | ReasoningItem | ToolCallItem, Field(discriminator="kind")]


# The item read's cursor is the plain stream position (`_ItemBase.opened_seq`): `event_seq` is
# dense per conversation and append-only, so an `int` is already a durable keyset position —
# inclusive, naming the first item a page did not return — and a page is served from it by
# indexed reads of the materialised rows, never by refolding the thread. A single integral key
# wears no wrapper; the composite keysets (`SessionCursor`, `TurnCursor`) keep their types.


_PROMPT_ORIGIN = TypeAdapter[PromptOrigin](PromptOrigin)


@dataclass(frozen=True, slots=True)
class ConversationPageRow:
    """One item row of a page, with the frame span its events were read off.

    The span is aggregated from the row's `conversation_event` rows — absent for a row whose every
    event was console-authored, a prompt being the case that exists.
    """

    item: ConversationItem
    first_frame_seq: int | None
    last_frame_seq: int | None


def turn_end_of(turn: ConversationTurn) -> TurnEnd | None:
    """How *turn* ended, or None while it is still running.

    `ck_conversation_turn_failure` is what makes the failed arm's reason always present.
    """
    match turn.outcome:
        case None:
            return None
        case TurnOutcome.ANSWERED:
            return TurnAnswered()
        case TurnOutcome.ABORTED:
            return TurnAborted()
        case TurnOutcome.FAILED:
            assert turn.failure is not None, "ck_conversation_turn_failure"
            return TurnFailed(failure=turn.failure)


def _provenance_of(row: ConversationPageRow) -> ItemProvenance:
    """The frame span the row's content was read off, or the console's own authorship.

    A frame-derived row names its session (`ck_conversation_event_provenance_frames` holds it on
    every event in the span), and the item carries it: a conversation's items span replaced
    sessions, so a frame number alone would not say whose wire log to appeal to.
    """
    match row.first_frame_seq, row.last_frame_seq:
        case None, None:
            return ConsoleAuthored()
        case int(first), int(last):
            if row.item.session_id is None:
                raise ValueError(f"a frame-derived row names no session: {row.item.item_id=}")
            return FromFrames(session_id=row.item.session_id, first_frame_seq=first, last_frame_seq=last)
        case _:
            raise ValueError(f"a row's frame span has one end: {row.item.item_id=}")


def item_of(row: ConversationPageRow) -> Item:
    """The wire item one row folds to."""
    item = row.item
    provenance = _provenance_of(row)
    match item.item_type:
        case ItemType.PROMPT:
            if item.origin is None:
                raise ValueError(f"a prompt row names no origin: {item.item_id=}")
            return PromptItem(
                opened_seq=item.opened_seq,
                closed_seq=item.closed_seq,
                status=item.status,
                provenance=provenance,
                text=item.item_text,
                origin=_PROMPT_ORIGIN.validate_python(item.origin).kind,
            )
        case ItemType.MESSAGE:
            return MessageItem(
                opened_seq=item.opened_seq,
                closed_seq=item.closed_seq,
                status=item.status,
                provenance=provenance,
                text=item.item_text,
                backend_item_id=item.backend_item_id,
            )
        case ItemType.REASONING:
            return ReasoningItem(
                opened_seq=item.opened_seq,
                closed_seq=item.closed_seq,
                status=item.status,
                provenance=provenance,
                text=item.item_text,
                disclosure=item.disclosure,
            )
        case ItemType.TOOL_CALL:
            assert item.call_id is not None, "ck_conversation_item_tool_call_fields"
            assert item.tool_name is not None, "ck_conversation_item_tool_call_fields"
            return ToolCallItem(
                opened_seq=item.opened_seq,
                closed_seq=item.closed_seq,
                status=item.status,
                provenance=provenance,
                call_id=item.call_id,
                tool_name=item.tool_name,
                arguments=dict(item.arguments) if item.arguments is not None else {},
                content=item.item_text,
                structured=item.structured,
                outcome=item.outcome,
            )
