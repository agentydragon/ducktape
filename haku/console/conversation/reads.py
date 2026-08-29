"""What the conversation reads hand back — a session, a frame, a turn — and the cursors that page
them.

The store produces these, and two surfaces serialise them: <../tools/conversations.py> over MCP,
and the SPA's wire shapes in <../session/conversation_views.py>. They live at the runtime level
because the store is their only producer. The item read — one `conversation_item` row on the
wire — is the fold's business, beside the store that produces it in <item_reads.py>.

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

from haku.console.conversation.conversation_event import TurnAborted, TurnAnswered, TurnFailed
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
    verbatim and whole, never the neutral conversation; a conversation item is what this frame
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


class WorkerStatus(StrEnum):
    """Where a dispatched worker's one-shot run has got to, as `get_worker_result` reports it.

    A three-way coarsening of the session lifecycle for the orchestrator that fanned the work out
    (#5193): the finer session/turn vocabulary is the console's own, not what a caller polling for
    an answer needs.
    """

    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class WorkerResult(BaseModel):
    """A dispatched worker's progress and, once it has answered, its output (#5193).

    The v0 loop-closer, deliberately minimal: a status off the worker session's own lifecycle plus
    the worker's final assistant message when it answered, or its failure surface when it died. A
    structured outcome — typed artifacts, PR links, files touched — is explicitly out of scope for
    v0; it needs a worker-output contract the harness does not emit yet.
    """

    status: WorkerStatus = Field(
        description="`running` while the worker is still working, `done` once its turn has answered, "
        "`failed` if the session or its turn died."
    )
    result: str | None = Field(
        default=None,
        description="The worker's final assistant message when `done`, the failure surface when "
        "`failed`; absent while `running` (and when a finished worker produced no message at all).",
    )
