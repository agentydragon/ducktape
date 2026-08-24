"""What the console's chat API returns for a session, and how stored rows become it.

The read models the SPA and the conversations inventory are typed against, together with the
projection that assembles one out of the session row, its transcript and its stored events.
Nothing here decides anything about a live session: it is handed rows and produces the shapes the
routes hand back.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from haku.console.chat_models import (
    BridgeFrameKind,
    FrameDirection,
    ItemStatus,
    ItemType,
    ReasoningDisclosure,
    RuntimeKind,
    SessionStatus,
    ToolOutcome,
    TurnOutcome,
)
from haku.console.database_schema import ConversationItem, Session, SessionFrame
from haku.console.x.conversation_records import ChannelAttachment
from haku.console.x.sandbox_claims import SandboxProvisioningView
from haku.console.x.setup_output import SETUP_OUTPUT_KIND


class ConversationItemView(BaseModel):
    """One item of a transcript, as the store hands it to whoever asked.

    **A tool call is one of these, not a field on a message.** What this replaces stapled calls onto
    the message whose frames made them, and joined the answer back by `call_id` at read time — two
    indexes and a bisect over frame spans. A call is a sibling item now, with its ask and its answer
    on its own row, so the join is gone and the transcript is the flat stream the design says it is.

    The per-type fields are the ones `conversation_item`'s constraints tie to `item_type`; a reader
    branches on the type rather than testing them for absence.

    **No frame numbers.** They are one session's and incomparable outside it, so a surface that
    wants to appeal an item to the wire asks for its events rather than being handed a coordinate
    it cannot interpret.
    """

    model_config = ConfigDict(extra="forbid")

    item_id: UUID
    item_type: ItemType
    status: ItemStatus
    text: str = Field(description="The concatenation of this item's segments — its whole prose.")
    call_id: str | None = None
    tool_name: str | None = None
    arguments: dict[str, Any] | None = None
    outcome: ToolOutcome | None = None
    structured: Any | None = None
    disclosure: ReasoningDisclosure | None = None
    created_at: datetime
    updated_at: datetime


class SessionView(BaseModel):
    """One session's own row and transcript, as the store hands it to whoever asked.

    Not a wire shape: the browser reads a conversation, assembled from this.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    status: SessionStatus
    error: str | None
    created_at: datetime
    updated_at: datetime
    items: list[ConversationItemView]


class LiveSession(BaseModel):
    """The session currently holding a conversation, where one holds it.

    At most one, because only one session holds a conversation at a time. Absent means the thread
    is between runners: a prompt to it needs a new session rather than reaching an existing one.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    status: SessionStatus


class ConversationSummary(BaseModel):
    """The operator-facing inventory entry for one conversation.

    Carries **attachments** rather than one surface: a conversation is held by however many
    channels have attached to it, and a browser reading it is not one of them.
    """

    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID
    agent_id: UUID | None = None
    access_profile_id: str | None = None
    runtime_kind: RuntimeKind
    created_at: datetime
    last_activity_at: datetime = Field(
        description="When the most recent session under this conversation last moved. What the list is ordered by."
    )
    attachments: list[ChannelAttachment]
    live_session: LiveSession | None
    last_session_status: SessionStatus | None = Field(
        default=None,
        description="How this conversation's most recent session ended — what separates a thread whose"
        " runner failed from one that closed cleanly, which `live_session: null` alone cannot say."
        " None while a session is live (its state is `live_session`).",
    )
    item_count: int = Field(
        description="How many transcript rows this conversation holds — prompts, answers and calls alike."
    )


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


class SetupNarrationView(BaseModel):
    """One thing the sandbox said while coming up, as the frame log recorded it.

    Positioned by `frame_seq` alone: the runner numbers setup output on the wire but does not retain
    it in the native replay window, so two identical rendered lines are two things that happened.
    """

    model_config = ConfigDict(extra="forbid")

    frame_seq: int
    text: str
    created_at: datetime


class ConversationTurnView(BaseModel):
    """A turn summary, without exposing the raw frame range yet."""

    model_config = ConfigDict(extra="forbid")

    turn_id: UUID
    started_at: datetime
    ended_at: datetime | None
    outcome: TurnOutcome | None


class ConversationSessionView(BaseModel):
    """One session of a conversation, whole: what it said, what it cost to start, how it ended."""

    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    status: SessionStatus
    error: str | None
    created_at: datetime
    updated_at: datetime
    provisioning: SandboxProvisioningView | None = None
    narration: list[SetupNarrationView]
    items: list[ConversationItemView]
    turns: list[ConversationTurnView]


class EarlierSession(BaseModel):
    """A session this conversation ran before the current one, newest first.

    A conversation outlives its sessions, so a thread whose sandbox died has more than one, each
    with its own frame log. This is the handle that keeps them reachable.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    status: SessionStatus
    created_at: datetime


class ConversationView(BaseModel):
    """One conversation as the browser reads it.

    No terminal state and no `ended_at`: a conversation is an id, and what ends is the session
    under it.
    """

    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID
    agent_id: UUID | None = None
    access_profile_id: str | None = None
    runtime_kind: RuntimeKind
    created_at: datetime
    attachments: list[ChannelAttachment]
    session: ConversationSessionView
    earlier_sessions: list[EarlierSession]


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

    Whole rows rather than events to apply: a merge keyed on `item_id` is idempotent, so a
    duplicate costs nothing and re-reading from an older position is always correct. `event_seq` is
    the address — where the follower is — and these rows are what that position resolves to.

    **The transcript arrives incrementally and everything else arrives whole.** Only the items
    and turns grow without bound, so only they are worth addressing by position; the rest of what a
    conversation shows is a handful of rows, and sending them every time is what keeps a follower
    from holding a copy nothing can correct. A field carried only in the snapshot would be a value
    the tab can never be told has changed.

    That extends to the live session's own row, timestamps included, so that a follower merging
    this can hold no field belonging to a session it has just been told was replaced. What a
    conversation never changes is left out: its id, and when it was opened.
    """

    model_config = ConfigDict(extra="forbid")

    message_type: Literal["update"] = "update"
    position: int = Field(description="Where applying this leaves the follower; what a reconnect asks from.")
    session_id: UUID = Field(description="The session now holding the conversation, which a replacement changes.")
    status: SessionStatus
    error: str | None
    created_at: datetime
    updated_at: datetime
    provisioning: SandboxProvisioningView | None = Field(
        default=None,
        description="The cluster's account of the sandbox this session is waiting on, while it is still waiting.",
    )
    narration: list[SetupNarrationView] = Field(
        description="What that session has said while coming up, whole — replace by `frame_seq`."
    )
    attachments: list[ChannelAttachment] = Field(
        description="The channels holding a copy of this conversation now — replaces what is held."
    )
    earlier_sessions: list[EarlierSession] = Field(
        description="The sessions this conversation ran before `session_id`, newest first — replaces what is held."
    )
    items: list[ConversationItemView] = Field(
        description="The items that moved — merge them by `item_id` over the ones already held, never render them as a transcript."
    )
    turns: list[ConversationTurnView] = Field(description="The turns that moved, newest first.")


# The browser's types for these come from here: `haku.console.export_schema` publishes this union
# into the OpenAPI document the frontend generates from, a WebSocket having no route for FastAPI to
# document. A field renamed on either model is a compile error in `frontend/x/conversation_follow.ts`
# rather than a message a tab cannot read.
type ConversationFollowMessage = Annotated[
    ConversationSnapshot | ConversationUpdate, Field(discriminator="message_type")
]


# Rows of one update, in either collection. An item carries its whole prose, and a tool call its
# arguments and structured result, so what bounds a row is payload rather than row count — the
# lesson `/api/tool-calls` learned at `le=500` (`frontend/tool_calls_page.tsx`). One update is
# normally one coalescing window's worth of rows; past this the follower is sent the conversation
# whole instead, which is cheaper than an update carrying most of one twice over.
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
    runtime_kind: RuntimeKind = Field(
        description="The immutable runner implementation pinned by this session's conversation."
    )
    status: SessionStatus = Field(
        description="The session's stored status. `responding` never appears here: it is derived "
        "from an open turn by `session_view` and is not on the row."
    )
    sandbox: SandboxProvisioningView | None = Field(
        description="The cluster's account of this session's sandbox. Null only while the session "
        "is idle and has never asked for one; `claim_absent` means one was requested but Kubernetes "
        "does not have it now."
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
    runtime_kind: RuntimeKind = Field(description="The immutable runner implementation whose wire these frames use.")
    next_before_seq: int | None = Field(
        description="Pass back as `before_seq` for the page of earlier frames, or absent at the start of the log."
    )


def frame_page(
    rows: Sequence[SessionFrame], *, limit: int, conversation_id: UUID, runtime_kind: RuntimeKind
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
        runtime_kind=runtime_kind,
        next_before_seq=frames[0].frame_seq if len(frames) == limit else None,
    )


async def setup_narration(db: AsyncSession, session_id: UUID) -> list[SetupNarrationView]:
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
        SetupNarrationView(frame_seq=frame_seq, text=payload["text"], created_at=created_at)
        for frame_seq, payload, created_at in rows
    ]


def item_view(item: ConversationItem) -> ConversationItemView:
    """One stored item, as a reader sees it. A projection of the row and nothing more."""
    return ConversationItemView(
        item_id=item.item_id,
        item_type=item.item_type,
        status=item.status,
        text=item.item_text,
        call_id=item.call_id,
        tool_name=item.tool_name,
        arguments=item.arguments,
        outcome=item.outcome,
        structured=item.structured,
        disclosure=item.disclosure,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def live_status(record: Session, *, responding: bool) -> SessionStatus:
    """The status a reader is told, with `responding` derived from an open turn.

    `status` is the frontend's contract (`frontend/x/conversations_page.tsx` switches on it); the
    column underneath carries no turn state. The session's own lifecycle — provisioning, closing,
    closed, failed — always wins, because a turn left open by a dead replica says nothing about a
    session the sweep has since failed.
    """
    return SessionStatus.RESPONDING if responding and record.status == SessionStatus.READY else record.status


def session_view(record: Session, items: list[ConversationItem], *, responding: bool) -> SessionView:
    return SessionView(
        session_id=record.session_id,
        status=live_status(record, responding=responding),
        error=record.error,
        created_at=record.created_at,
        updated_at=record.updated_at,
        items=[item_view(item) for item in items],
    )
