"""The records a conversation read hands back, and the cursors that page them.

The store produces these — a session row, a rollout frame, a turn, a transcript entry — and
<../tools/conversations.py> is the MCP surface that serialises them. They live at the runtime level
because the store is their only producer.

**A record, not a page.** How records are handed out — the `Page` envelope every listing shares,
the byte budget a page spends, and the clipping that budget forces — belongs to the tool. What is
here is what one read produced.

**Pydantic rather than dataclasses, because the boundary needs it.** Every model here is either an
MCP tool's return type, whose JSON schema is generated from the class, or a cursor that arrives
back as a tool argument and is parsed out of the wire. `TranscriptSlice` is the one exception:
nothing serialises it, and it is the store's hand-off to the tool's byte budget.
"""

from __future__ import annotations

import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


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


class RolloutFrame(BaseModel):
    """One frame of a named backend's wire — Claude Code's — not of the neutral conversation.

    The sanctioned exception among the models here, labelled so a reader cannot mistake the two:
    `kind` and `payload` are the CLI's own words, and a transcript entry is what they projected to.
    """

    frame_seq: int
    direction: str = Field(description="`to_agent` for what the console sent, `from_agent` for what came back.")
    kind: str = Field(description="The frame's protocol `type`: assistant, user, result, system, …")
    created_at: datetime.datetime
    payload: dict[str, Any] | None = Field(
        description="The frame exactly as it crossed the wire, or absent when it was clipped for size."
    )
    clipped_bytes: int | None = Field(
        default=None, description="Set instead of `payload` when the frame was too large to return; its size in bytes."
    )


class FrameCursor(BaseModel):
    """Where a read of the frame log starts — inclusively, so this is a frame that exists.

    Inclusive rather than "after this one" so that a transcript entry's `first_frame_seq` is
    already a cursor: appealing a normalization to the frames behind it needs no arithmetic.
    """

    frame_seq: int

    @classmethod
    def of(cls, frame: RolloutFrame) -> FrameCursor:
        return cls(frame_seq=frame.frame_seq)


class TurnRecord(BaseModel):
    """One exchange of a session, as a range over that session's frames."""

    turn_id: UUID
    first_frame_seq: int = Field(description="Pass to `read_rollout` as `cursor` to read this exchange.")
    last_frame_seq: int | None = Field(
        description="Inclusive end of the range. Absent while the exchange is still running, "
        "and on a finished one that recorded no frames at all."
    )
    started_at: datetime.datetime
    ended_at: datetime.datetime | None = Field(description="Absent while the exchange is still running.")
    outcome: str | None = Field(description="`answered`, `aborted` or `failed`; absent while it is still running.")


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

    This is the appeal path. `read_frame(session_id, first_frame_seq)` returns the first one
    whole however large; `read_rollout(session_id, cursor={"frame_seq": first_frame_seq})` walks
    the span.
    """

    kind: Literal["frames"] = "frames"
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
    The shares are measured in <../debug/frame_shape_census.md>.

    Spelled here rather than shared with <conversation_events.py>'s enum of the same members: that
    one is the fold's own vocabulary.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class MessageRef(BaseModel):
    """Which agent message an entry belongs to, within one session's transcript.

    The `frame_seq` the message opened at — ours, deterministic, and a pointer back into the log.
    Deliberately not the agent's own message id, which a great many production rows do not have.
    """

    opened_at_frame_seq: int


class _EntryBase(BaseModel):
    """What every transcript entry carries: where it sits, and where it came from."""

    index: int = Field(
        description="This entry's position in the session's transcript. `read_transcript`'s `cursor` names one."
    )
    provenance: EntryProvenance


class MessageEntry(_EntryBase):
    """One agent message, finished. `text` is absent for a message that was all thinking and tools.

    `agent_message_id` is provenance, not identity: it is what the frames called this message, and
    it is absent whenever the wire did not supply one.
    """

    kind: Literal["message"] = "message"
    message: MessageRef
    text: str | None
    agent_message_id: str | None


class ReasoningEntry(_EntryBase):
    """The agent thought, with a summary where it gave one.

    A state rather than empty prose: real messages are routinely thinking with nothing else in
    them, and a transcript that models only text renders them blank.
    """

    kind: Literal["reasoning"] = "reasoning"
    message: MessageRef
    summary: str | None


class ToolCallEntry(_EntryBase):
    """A tool was called. Its answer is a separate `tool_result` entry, joined by `call_id`."""

    kind: Literal["tool_call"] = "tool_call"
    message: MessageRef
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
        default=None, description="The call's structured output, verbatim; absent when it had none or was clipped."
    )
    clipped_bytes: int | None = Field(
        default=None,
        description="Set instead of `structured` when this entry alone overran a page's budget; its size in bytes. "
        "`provenance` names the frames to read it from.",
    )
    outcome: Outcome


class TurnEndEntry(_EntryBase):
    kind: Literal["turn_end"] = "turn_end"
    outcome: str = Field(description="`answered`, `aborted` or `failed`, as `list_turns` also reports it.")


type TranscriptEntry = Annotated[
    MessageEntry | ReasoningEntry | ToolCallEntry | ToolResultEntry | TurnEndEntry, Field(discriminator="kind")
]


class TranscriptCursor(BaseModel):
    """A position in a session's transcript, by ordinal.

    An ordinal rather than a keyset, and safe here for the one reason an offset is ever safe: this
    order only ever grows at its *end*. The frame log is append-only and the projection is a
    deterministic left-to-right fold of it, so entry *n* is the same entry on every read; the one
    entry that can change is the last, when it belongs to a turn still in flight.

    A keyset on the frame the entry came from would not do: a console-authored entry has no frames
    at all (see `ConsoleAuthored`) and so has no position in that key.
    """

    index: int

    @classmethod
    def of(cls, entry: TranscriptEntry) -> TranscriptCursor:
        return cls(index=entry.index)


class TranscriptSlice(BaseModel):
    """What the store hands back for one `read_transcript` call, before the page's byte budget.

    Up to `limit + 1` entries, like every other read here: the extra row is what tells a full page
    from the last one, and it is the row the returned cursor names.
    """

    entries: list[TranscriptEntry]
    unreadable: dict[str, int] | None
