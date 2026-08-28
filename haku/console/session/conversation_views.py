"""What the console's conversation API returns, and the session shapes beside it.

The SPA's wire shapes — the inventory, the conversation detail, and the follow socket's messages.
Projections, not a read model: the conversation entries they carry are the shared vocabulary of
<conversation_reads.py>, folded once in <item_entries.py>, and what is here is how the browser is
handed it — which container, which session row beside it, which page envelope. Nothing here
decides anything about a live session: it is handed rows and produces the shapes the routes hand
back.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from haku.console.conversation.reads import ChannelAttachment, ConversationEntry, SetupOutputRecord
from haku.console.database_schema import Session, SessionFrame
from haku.console.harnesses.kind import HarnessKind
from haku.console.session.sandbox_claims import SandboxProvisioningView
from haku.console.session.session_frames import BridgeFrameKind, FrameDirection
from haku.console.session.setup_output import SETUP_OUTPUT_KIND
from haku.console.session.status import SessionStatus


class SessionView(BaseModel):
    """One session's own row: the session concept's one REST shape (`SessionRecord` is the MCP one).

    Every surface that shows a session hands back this — the conversation's current and earlier
    sessions, the inventory's live one, the store's own reads. The entries are the conversation's,
    so they are not here; narration and the sandbox observation are per-read projections carried
    beside it, not row facts.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    status: SessionStatus
    error: str | None
    created_at: datetime
    updated_at: datetime


class ConversationSummary(BaseModel):
    """The operator-facing inventory entry for one conversation.

    Carries **attachments** rather than one surface: a conversation is held by however many
    channels have attached to it, and a browser reading it is not one of them.
    """

    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID
    agent_id: UUID | None = None
    access_profile_id: str | None = None
    created_at: datetime
    last_activity_at: datetime = Field(
        description="When the most recent session under this conversation last moved. What the list is ordered by."
    )
    attachments: list[ChannelAttachment]
    live_session: SessionView | None = Field(
        description="The session currently holding this conversation — at most one, because only one "
        "session holds a conversation at a time. Absent means the thread is between runners: a prompt "
        "to it needs a new session rather than reaching an existing one."
    )
    last_session_status: SessionStatus | None = Field(
        default=None,
        description="How this conversation's most recent session ended — what separates a thread whose"
        " runner failed from one that closed cleanly, which `live_session: null` alone cannot say."
        " None while a session is live (its state is `live_session`).",
    )
    item_count: int = Field(
        description="How many transcript rows this conversation holds — prompts, answers and calls alike."
    )
    harness_kind: HarnessKind


class ConversationCursor(BaseModel):
    """A position in the newest-activity-first order the inventory walks.

    Keyset rather than an offset, because this list only ever grows and grows at the top: every
    conversation that moves while a reader pages would push a row across a page boundary.
    `conversation_id` breaks the tie `last_activity_at` alone leaves, so the key is total.
    """

    model_config = ConfigDict(extra="forbid")

    last_activity_at: datetime
    conversation_id: UUID


class ConversationPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversations: list[ConversationSummary]
    next_cursor: ConversationCursor | None = Field(
        description="The first conversation this page did not return. Pass its fields back as "
        "`before_activity`/`before_conversation` to continue; absent when this page is the last."
    )


class ConversationView(BaseModel):
    """One conversation as the browser reads it.

    No terminal state and no `ended_at`: a conversation is an id, and what ends is the session
    under it. `entries` is the same item-row read the MCP surface pages, whole.
    """

    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID
    agent_id: UUID | None = None
    access_profile_id: str | None = None
    created_at: datetime
    attachments: list[ChannelAttachment]
    entries: list[ConversationEntry] = Field(
        description="The conversation's item rows as entries, in opening order — each in its current "
        "state, an item still being written included."
    )
    session: SessionView = Field(
        description="The current session: the one holding the conversation, or the last one to have held it."
    )
    provisioning: SandboxProvisioningView | None = Field(
        default=None,
        description="The cluster's account of the sandbox the current session is waiting on, while it "
        "is still waiting.",
    )
    narration: list[SetupOutputRecord] = Field(
        description="What the current session said while coming up. For a session that died during "
        "setup it is the whole account."
    )
    earlier_sessions: list[SessionView] = Field(
        description="The sessions this conversation ran before the current one, newest first. A "
        "conversation outlives its sessions, so a thread whose sandbox died has more than one, each "
        "with its own frame log; this is the handle that keeps them reachable."
    )
    harness_kind: HarnessKind


class ConversationSnapshot(BaseModel):
    """A conversation whole, at the position the updates after it continue from.

    First of a follow, and again whenever a position can no longer be served from — a client
    replaces what it holds and reads on, without a second way of asking.
    """

    model_config = ConfigDict(extra="forbid")

    message_type: Literal["snapshot"] = "snapshot"
    position: int = Field(
        description="The conversation's `event_seq` this state was read at. Everything after it arrives as updates."
    )
    conversation: ConversationView


class ConversationUpdate(BaseModel):
    """What moved in a conversation since a position, for a follower that already holds the rest.

    **The entries arrive incrementally and everything else arrives whole.** The entries are the
    rows the log's events since the follower's position are about, each re-sent whole in its
    current state — a message being written arrives once per coalescing window with the prose so
    far — and merging them is a replace by `opened_seq`, idempotent, so a message delivered twice or
    re-read from an older position lands on the same conversation. Everything else a conversation
    shows is a handful of rows, and sending them every time is what keeps a follower from holding
    a copy nothing can correct: `narration` replaces what is held, and so do the attachments and
    the session block.

    That extends to the live session's own row, timestamps included, so that a follower merging
    this can hold no field belonging to a session it has just been told was replaced. What a
    conversation never changes is left out: its id, and when it was opened.
    """

    model_config = ConfigDict(extra="forbid")

    message_type: Literal["update"] = "update"
    position: int = Field(description="Where applying this leaves the follower; what a reconnect asks from.")
    session: SessionView = Field(
        description="The session now holding the conversation — which a replacement changes — its own "
        "row whole, replacing what is held."
    )
    provisioning: SandboxProvisioningView | None = Field(
        default=None,
        description="The cluster's account of the sandbox this session is waiting on, while it is still waiting.",
    )
    narration: list[SetupOutputRecord] = Field(
        description="What that session has said while coming up, whole — replaces what is held."
    )
    attachments: list[ChannelAttachment] = Field(
        description="The channels holding a copy of this conversation now — replaces what is held."
    )
    earlier_sessions: list[SessionView] = Field(
        description="The sessions this conversation ran before the current one, newest first — replaces what is held."
    )
    entries: list[ConversationEntry] = Field(
        description="The rows that moved since the follower's position, whole and in their current state — "
        "merge them by `opened_seq` over the ones already held, replacing."
    )


# The browser's types for these come from here: `haku.console.export_schema` publishes this union
# into the OpenAPI document the frontend generates from, a WebSocket having no route for FastAPI to
# document. A field renamed on either model is a compile error in `frontend/x/conversation_follow.ts`
# rather than a message a tab cannot read.
type ConversationFollowMessage = Annotated[
    ConversationSnapshot | ConversationUpdate, Field(discriminator="message_type")
]


# Entries of one update. An entry carries its whole prose, and a tool call its arguments and
# structured result, so what bounds an update is payload rather than row count — the lesson
# `/api/tool-calls` learned at `le=500` (`frontend/tool_calls_page.tsx`). One update is normally
# one coalescing window's worth of entries; past this the follower is sent the conversation whole
# instead, which is cheaper than an update carrying most of one twice over.
UPDATE_ROW_LIMIT = 50


class SessionProvisioningView(BaseModel):
    """What one session says about the sandbox it asked for — in whatever state that session is now.

    **Nothing is reported by being absent.** `sandbox` is null for exactly one reason, a session
    that has never asked for a sandbox; a claim Kubernetes does not have is a view whose step is
    `claim_absent`, and a cluster that could not be read at all is a view carrying
    `observation_error`. An operator asking why a session never came up has to tell the three
    apart.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    status: SessionStatus = Field(
        description="The session's row-derived status. `responding` never appears here: it is "
        "derived from an open turn by `session_view`, which the row cannot spell."
    )
    sandbox: SandboxProvisioningView | None = Field(
        description="The cluster's account of this session's sandbox. Null only while the session "
        "is idle and has never asked for one; `claim_absent` means one was requested but Kubernetes "
        "does not have it now."
    )
    harness_kind: HarnessKind = Field(
        description="The immutable runner implementation pinned by this session's conversation."
    )


# Frames per page of the inspector. One `user` frame carries a whole tool result — a file read, a
# command's output — so the row count alone does not bound a response, and the browser pays again
# to syntax-highlight each one (`frontend/code_block.tsx`). Fifty is roughly two exchanges.
DEFAULT_FRAME_PAGE = 50
MAX_FRAME_PAGE = 200


class SessionFrameView(BaseModel):
    """One row of the rollout, as the console's frame inspector reads the selected harness's wire.

    **This is one backend's wire and must be presented as such**, never as the conversation: it is
    the only shape the console serves that names a backend.

    The payload is the complete native frame, wire whole. This surface exists because
    `conversation_item` is a lossy projection of the frame log, so clipping here would reintroduce
    that one level down; bounding a response is the page's job.
    """

    model_config = ConfigDict(extra="forbid")

    frame_seq: int
    direction: FrameDirection
    kind: BridgeFrameKind
    created_at: datetime
    payload: dict[str, Any]


class SessionFramePage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frames: list[SessionFrameView]
    conversation_id: UUID = Field(
        description="The thread this session ran. The inspector is addressed by session, so this is what its reader "
        "needs to get back to the conversation the session belongs to."
    )
    next_before_seq: int | None = Field(
        description="Pass back as `before_seq` for the page of earlier frames, or absent at the start of the log."
    )
    harness_kind: HarnessKind = Field(description="The immutable runner implementation whose wire these frames use.")


def frame_page(
    rows: Sequence[SessionFrame], *, limit: int, conversation_id: UUID, runtime_kind: HarnessKind
) -> SessionFramePage:
    """One page of rollout rows in wire order, with the cursor for the page before it.

    A short page is the first one, the same rule the MCP reader uses in the other direction:
    cheaper than a second count query, for the only question a caller has.
    """
    frames = [
        SessionFrameView(
            frame_seq=row.frame_seq,
            direction=row.direction,
            kind=row.kind,
            created_at=row.created_at,
            payload=row.payload,
        )
        for row in rows
    ]
    return SessionFramePage(
        frames=frames,
        conversation_id=conversation_id,
        harness_kind=runtime_kind,
        next_before_seq=frames[0].frame_seq if len(frames) == limit else None,
    )


async def setup_narration(db: AsyncSession, session_id: UUID) -> list[SetupOutputRecord]:
    """What the sandbox printed while bootstrapping, in the order it produced it.

    Unbounded, like the transcript beside it in the same response: in the session where narration
    is the longer of the two — one that died during setup — it is the whole account.
    """
    rows = await db.execute(
        select(SessionFrame.frame_seq, SessionFrame.payload, SessionFrame.created_at)
        .where(SessionFrame.session_id == session_id, SessionFrame.kind == SETUP_OUTPUT_KIND)
        .order_by(SessionFrame.frame_seq)
    )
    return [
        SetupOutputRecord(frame_seq=frame_seq, text=payload["text"], created_at=created_at)
        for frame_seq, payload, created_at in rows
    ]


def live_status(record: Session, *, responding: bool) -> SessionStatus:
    """The status a reader is told, with `responding` derived from an open turn.

    `status` is the frontend's contract (`frontend/x/conversations_page.tsx` switches on it); the
    session row's facts carry no turn state. The session's own lifecycle — provisioning, closing,
    closed, failed — always wins, because a turn left open by a dead replica says nothing about a
    session the sweep has since failed.
    """
    return SessionStatus.RESPONDING if responding and record.status == SessionStatus.READY else record.status


def session_view(record: Session, *, responding: bool) -> SessionView:
    return SessionView(
        session_id=record.session_id,
        status=live_status(record, responding=responding),
        error=record.error,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )
