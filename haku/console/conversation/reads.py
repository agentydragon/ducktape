"""What the conversation reads hand back, and the cursors that page them.

The store produces these — a session, a frame, a turn, a conversation entry — and two surfaces
serialise them: <../tools/conversations.py> over MCP, and the SPA's wire shapes in
<conversation_views.py>. They live at the runtime level because the store is their only producer.

**One vocabulary, faithfully what the rows hold.** A `ConversationEntry` is one
`conversation_item` row, minimally wrapped: its kind is the row's `item_type`, its `status` the
row's lifecycle, and an item still open or cut off appears exactly as the row does — nothing is a
member of one surface and not the other, and the read does not editorialise the record. Turns are
`list_turns`' business, not entries.

**What one read produced, not a page.** How reads are handed out — the `Page` envelope every
listing shares — belongs to the tool.

**Pydantic rather than dataclasses, because the boundary needs it.** Every model here is either an
MCP tool's return type, whose JSON schema is generated from the class, or a cursor that arrives
back as a tool argument and is parsed out of the wire.
"""

from __future__ import annotations

import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from haku.console.chat_models import ItemStatus
from haku.console.conversation.conversation_event import ReasoningDisclosure, TurnAborted, TurnAnswered, TurnFailed
from haku.console.conversation.prompt_origin import PromptOriginKind
from haku.console.harnesses.kind import HarnessKind
from haku.console.session.session_frames import FrameDirection


class ChannelAttachment(BaseModel):
    """A channel holding a copy of a conversation, at the address it holds it under.

    Only live attachments are reported: a detached one is history the channel no longer serves.
    A browser tab has none — it keeps no copy, so there is nothing to address.
    """

    surface: str = Field(description="Which kind of channel holds the copy; `matrix` today.")
    address: str = Field(
        description="What that channel calls this conversation — a Matrix room id for `matrix`. Opaque "
        "to everything but the channel itself."
    )
    attached_at: datetime.datetime


class SessionRecord(BaseModel):
    """One runner's life, and the thread it ran.

    A session, not a conversation: it ends, and the conversation it belonged to does not. Sessions
    sharing a `conversation_id` are one thread, which is how a session replaced when its sandbox
    died is recognisable as the continuation of the one before it.
    """

    session_id: UUID
    conversation_id: UUID = Field(
        description="The thread this session ran; successive sessions of one thread share it."
    )
    agent_id: UUID | None = None
    access_profile_id: str | None = None
    attachments: list[ChannelAttachment] = Field(description="The channels currently holding a copy of that thread.")
    status: str
    created_at: datetime.datetime
    error: str | None = None
    harness_kind: HarnessKind = Field(description="The immutable runner implementation pinned by that conversation.")


class SessionCursor(BaseModel):
    """A position in the `(created_at, session_id)` order `list_sessions` walks.

    **Both columns, because one does not order the corpus.** A Matrix room and the SPA can open a
    session in the same instant, and a cursor naming only `created_at` would then either hand back
    a session the previous page already carried or step over one it never did. `session_id` breaks
    the tie, and the key is spelled out rather than hidden behind an opaque string so a reader can
    see what the page boundary is.
    """

    created_at: datetime.datetime
    session_id: UUID

    @classmethod
    def of(cls, session: SessionRecord) -> SessionCursor:
        return cls(created_at=session.created_at, session_id=session.session_id)


class HarnessFrameRecord(BaseModel):
    """One frame of the session's wire log that a named harness sent or was sent.

    A **named** harness's own wire — Claude Code's today, since that is the adapter there is —
    verbatim and whole, never the neutral conversation; a conversation entry is what this frame
    projected to. No generic reader derives a discriminator from ``payload``: a harness is free to
    use any JSON shape at all.
    """

    kind: Literal["harness_frame"] = "harness_frame"
    frame_seq: int
    direction: FrameDirection = Field(
        description="`to_agent` for what the console sent, `from_agent` for what came back."
    )
    created_at: datetime.datetime
    payload: dict[str, Any] = Field(description="The frame exactly as it crossed the wire, whole.")


class SetupOutputRecord(BaseModel):
    """One line the sandbox printed while coming up, recorded in the same per-session log.

    Console-authored: no harness wire stands behind it, which is why it is its own variant with
    the line as typed text rather than a payload a reader must know not to read as protocol.
    """

    kind: Literal["setup_output"] = "setup_output"
    frame_seq: int
    created_at: datetime.datetime
    text: str


type FrameRecord = Annotated[HarnessFrameRecord | SetupOutputRecord, Field(discriminator="kind")]


class TurnRecord(BaseModel):
    """One exchange of a session, as a range over that session's frames."""

    turn_id: UUID
    first_frame_seq: int = Field(description="Pass to `read_session_frames` as `cursor` to read this exchange.")
    last_frame_seq: int | None = Field(
        description="Inclusive end of the range. Absent while the exchange is still running, "
        "and on a finished one that recorded no frames at all."
    )
    started_at: datetime.datetime
    ended_at: datetime.datetime | None = Field(description="Absent while the exchange is still running.")
    end: TurnAnswered | TurnAborted | TurnFailed | None = Field(
        discriminator="outcome",
        description="How the exchange ended, and on a failure why. Absent while it is still running.",
    )


class TurnCursor(BaseModel):
    """A position in the newest-first exchange order, tiebroken like `SessionCursor`."""

    started_at: datetime.datetime
    turn_id: UUID

    @classmethod
    def of(cls, turn: TurnRecord) -> TurnCursor:
        return cls(started_at=turn.started_at, turn_id=turn.turn_id)


class FromFrames(BaseModel):
    """The frames this entry was read off, inclusive at both ends.

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
        "entries span replaced sessions, and a frame number means nothing without its session."
    )
    first_frame_seq: int
    last_frame_seq: int


class ConsoleAuthored(BaseModel):
    """The console said this itself, so there is no frame to appeal to and there never will be.

    Distinct in kind from a frame-derived entry whose range happens to be unknown: re-reading the
    frames can only preserve one of these, never re-derive it.
    """

    kind: Literal["authored"] = "authored"


type EntryProvenance = Annotated[FromFrames | ConsoleAuthored, Field(discriminator="kind")]


class Outcome(StrEnum):
    """How a step ended, where "cannot tell" is a first-class answer rather than a default.

    `UNKNOWN` is the common case, not the corner: the field a provider would report failure in is
    routinely absent, and collapsing that into `SUCCEEDED` reports every unanswerable case as fine.

    Spelled here rather than shared with <conversation_events.py>'s enum of the same members: that
    one is the fold's own vocabulary.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class _EntryBase(BaseModel):
    """What every conversation entry carries: one `conversation_item` row, minimally wrapped.

    `opened_seq` is the row's opening position in the conversation's event stream — its position
    for its whole life, however its state settles — so paging is stable while an item is still
    being written. `status` is the row's lifecycle exactly as stored: `open` is still being
    written and its prose fields are as of this read; `failed` is an item its session died
    around, kept with whatever had been said.
    """

    opened_seq: int = Field(
        description="The stream position the item was opened at — the row's position for its whole life, "
        "and what `read_conversation_items`'s `cursor` names. Entries are sparse in the stream, since most "
        "stream rows build an entry rather than opening one."
    )
    closed_seq: int | None = Field(
        description="The stream position the item settled at; absent exactly while it is still open."
    )
    status: ItemStatus = Field(
        description="The row's lifecycle as stored: `open` is still being written (prose as of this read), "
        "`failed` was cut off by its session dying, `complete` is settled."
    )
    provenance: EntryProvenance


class PromptEntry(_EntryBase):
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


class MessageEntry(_EntryBase):
    """One agent message: the concatenation of the prose that has arrived for it.

    `backend_item_id` is provenance, not identity: it is what the frames called this message, and it
    is absent whenever the wire did not supply one — including while the message is still open.
    """

    kind: Literal["message"] = "message"
    text: str
    backend_item_id: str | None


class ReasoningEntry(_EntryBase):
    """The agent thought, as the row records it.

    Its own entry rather than part of a message: only Claude nests thinking inside an assistant
    message, and a shape that nested it would be one backend's promoted upward.

    `disclosure` is what the backend let the record keep: `withheld` means `text` is not the
    thought (Claude's `redacted_thinking` discloses nothing); absent while the row is still open.
    """

    kind: Literal["reasoning"] = "reasoning"
    text: str
    disclosure: ReasoningDisclosure | None


class ToolCallEntry(_EntryBase):
    """One tool call, whole: the ask, and the answer where one has arrived.

    The ask and the answer are one row and so one entry — `status` says whether the answer has
    arrived, rather than a separate result entry a reader would have to join back.
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
    outcome: Outcome | None = Field(
        description="How the call went, once answered: `unknown` where the provider reported neither way. "
        "None exactly while no answer has arrived — a call still running, or one its session died around."
    )


type ConversationEntry = Annotated[
    PromptEntry | MessageEntry | ReasoningEntry | ToolCallEntry, Field(discriminator="kind")
]


# The item read's cursor is the plain stream position (`ConversationEntry.seq`): `event_seq` is
# dense per conversation and append-only, so an `int` is already a durable keyset position —
# inclusive, naming the first entry a page did not return — and a page is served from it by
# indexed reads of the materialised rows, never by refolding the thread. A single integral key
# wears no wrapper; the composite keysets (`SessionCursor`, `TurnCursor`) keep their types.
