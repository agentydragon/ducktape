"""What the conversation reads hand back, and the cursors that page them.

The store produces these — a session, a frame, a turn, a conversation entry — and
<../tools/conversations.py> is the MCP surface that serialises them. They live at the runtime level
because the store is their only producer.

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

from haku.console.chat_models import FrameDirection, PromptOriginKind, RuntimeKind


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
    runtime_kind: RuntimeKind = Field(description="The immutable runner implementation pinned by that conversation.")
    attachments: list[ChannelAttachment] = Field(description="The channels currently holding a copy of that thread.")
    status: str
    created_at: datetime.datetime
    error: str | None = None


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
    first_frame_seq: int = Field(description="Pass to `read_frames` as `cursor` to read this exchange.")
    last_frame_seq: int | None = Field(
        description="Inclusive end of the range. Absent while the exchange is still running, "
        "and on a finished one that recorded no frames at all."
    )
    started_at: datetime.datetime
    ended_at: datetime.datetime | None = Field(description="Absent while the exchange is still running.")
    end: TurnAnsweredEnd | TurnAbortedEnd | TurnFailedEnd | None = Field(
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

    This is the appeal path. `read_frames(session_id, cursor=first_frame_seq)` walks the span,
    and with `limit=1` returns exactly the first frame. Frames are session-level while a
    conversation spans replaced sessions, so the range names its session.
    """

    kind: Literal["frames"] = "frames"
    session_id: UUID = Field(
        description="Whose wire log the range indexes — pass to `read_frames` unchanged. A conversation's "
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
    """What every conversation entry carries: where it sits, and where it came from."""

    seq: int = Field(
        description="The position in the conversation's event stream of the row that defines this entry. "
        "`read_conversation_items`'s `cursor` names one; entries are sparse in it, since most stream rows build an "
        "entry rather than being one."
    )
    provenance: EntryProvenance


class PromptEntry(_EntryBase):
    """What the session was asked, and in whose voice.

    On the transcript because a conversation without its questions is half a record. Console
    authored, always: a prompt is admitted before it crosses any wire, so re-reading the frames
    could only preserve one of these and never re-derive it.
    """

    kind: Literal["prompt"] = "prompt"
    text: str
    origin: PromptOriginKind = Field(
        description="Who asked: `spa` or `matrix` for the operator, through the console or a room; `harness` "
        "for the agent resuming its own session, which nobody typed."
    )


class MessageEntry(_EntryBase):
    """One agent message, finished: the concatenation of the prose that arrived for it.

    `backend_item_id` is provenance, not identity: it is what the frames called this message, and it
    is absent whenever the wire did not supply one.
    """

    kind: Literal["message"] = "message"
    text: str
    backend_item_id: str | None


class ReasoningEntry(_EntryBase):
    """The agent thought, with a summary where the backend disclosed one.

    Its own entry rather than part of a message: only Claude nests thinking inside an assistant
    message, and a shape that nested it would be one backend's promoted upward.

    `summary` is absent where the backend disclosed nothing at all — Claude's `redacted_thinking`,
    `ReasoningDisclosure.WITHHELD`. One field rather than a summary beside a disclosure enum, so
    the two cannot come to disagree.
    """

    kind: Literal["reasoning"] = "reasoning"
    summary: str | None


class ToolCallEntry(_EntryBase):
    """A tool was called. Its answer is a separate `tool_result` entry, joined by `call_id`."""

    kind: Literal["tool_call"] = "tool_call"
    call_id: str
    tool_name: str
    arguments: dict[str, Any]


class ToolResultEntry(_EntryBase):
    """What a call answered: the part a transcript can print, and the part it cannot.

    **`content` is the result rendered, not the result.** `structured` is the exit code, the patch,
    the MCP `structuredContent` — an open set of per-tool shapes no string carries. Both are
    carried because neither is derivable from the other, and `structured` is absent when the
    provider carried none.
    """

    kind: Literal["tool_result"] = "tool_result"
    call_id: str
    content: str = Field(
        description="The result as text: what the provider sent where it sent prose, and its JSON otherwise. "
        "`provenance` names the frames to read the original blocks from."
    )
    structured: Any = Field(
        default=None, description="The call's structured output, verbatim; absent when it had none."
    )
    outcome: Outcome


class TurnAnsweredEnd(BaseModel):
    outcome: Literal["answered"] = "answered"


class TurnAbortedEnd(BaseModel):
    outcome: Literal["aborted"] = "aborted"


class TurnFailedEnd(BaseModel):
    outcome: Literal["failed"] = "failed"
    failure: str = Field(
        description="Why the runtime could not finish, in the words it used. Present on every failed "
        "turn and on no other, so a reader never has to ask why a failure states no reason."
    )


class TurnEndEntry(_EntryBase):
    kind: Literal["turn_end"] = "turn_end"
    end: TurnAnsweredEnd | TurnAbortedEnd | TurnFailedEnd = Field(
        discriminator="outcome", description="How the exchange ended. `outcome` is the same value `list_turns` reports."
    )


type TurnEnd = TurnAnsweredEnd | TurnAbortedEnd | TurnFailedEnd

type ConversationEntry = Annotated[
    PromptEntry | MessageEntry | ReasoningEntry | ToolCallEntry | ToolResultEntry | TurnEndEntry,
    Field(discriminator="kind"),
]


# The item read's cursor is the plain stream position (`ConversationEntry.seq`): `event_seq` is
# dense per conversation and append-only, so an `int` is already a durable keyset position —
# inclusive, naming the first entry a page did not return — and a page is served from it by
# indexed reads of the materialised rows, never by refolding the thread. A single integral key
# wears no wrapper; the composite keysets (`SessionCursor`, `TurnCursor`) keep their types.
