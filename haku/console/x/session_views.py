"""What the console's chat API returns for a session, and how stored rows become it.

The read models the SPA and the conversations inventory are typed against, together with the
projection that assembles one out of the session row, its transcript and its stored events.
Nothing here decides anything about a live session: it is handed rows and produces the shapes the
routes hand back.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from haku.console.chat_models import (
    TOOL_CALL_EVENT_KINDS,
    ChatMessageRole,
    ChatMessageStatus,
    ChatSurface,
    ConversationEventKind,
    FrameDirection,
    RecordedToolCall,
    SessionStatus,
    TurnOutcome,
)
from haku.console.database_schema import Session, SessionEvent, SessionFrame, SessionMessage
from haku.console.x import session_events
from haku.console.x.conversation_events import Outcome
from haku.console.x.conversation_records import TurnUsage
from haku.console.x.sandbox_claims import ClaudeSandboxProvisioningView
from haku.console.x.setup_output import SETUP_OUTPUT_KIND


class SessionToolResultView(BaseModel):
    """What a tool answered — the renderable half, out of the event that recorded it.

    `content` is a string for most tools, the tool names for a result that named tools and carried
    nothing else, and the payload verbatim for a shape this release has no prose reading for.
    """

    model_config = ConfigDict(extra="forbid")

    content: Any
    # `Outcome.UNKNOWN` reads here as "not an error", which is what the frame parser this replaced
    # also did: a provider routinely reports nothing, and the SPA has one boolean with no third
    # state to render. Carrying the outcome through is a frontend change rather than this one.
    is_error: bool = False


class SessionToolCallView(RecordedToolCall):
    """A recorded call, plus the answer stored beside it.

    Inheriting the stored model is the statement of what the two are: `call_id`, `tool_name` and
    `arguments` are what the transcript row records, and `result` is the `tool_call_completed`
    event that answered the same `call_id`.

    Absent while the call is still running, and on a turn that died before answering: a state worth
    seeing rather than hiding.
    """

    result: SessionToolResultView | None = None


class SessionMessageView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: UUID
    role: ChatMessageRole
    status: ChatMessageStatus
    content: str
    tool_calls: list[SessionToolCallView]
    error: str | None
    source_first_frame_seq: int | None
    source_last_frame_seq: int | None
    created_at: datetime
    updated_at: datetime


class SessionView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    status: SessionStatus
    error: str | None
    created_at: datetime
    updated_at: datetime
    provisioning: ClaudeSandboxProvisioningView | None = None
    messages: list[SessionMessageView]


class ConversationSessionSummary(BaseModel):
    """The operator-facing inventory entry for one conversation."""

    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    surface: ChatSurface
    room_id: str | None
    status: SessionStatus
    error: str | None
    created_at: datetime
    updated_at: datetime
    message_count: int
    last_message_at: datetime | None


class SetupNarrationView(BaseModel):
    """One thing the sandbox said while coming up, as the frame log recorded it.

    Positioned by `frame_seq` and nothing else: these rows carry **no frame identity** (the runner
    sends them `replayable=False`, since a console cannot tell a replayed narration line from a
    repeated one), so two identical lines are two things that happened, not one seen twice.
    """

    model_config = ConfigDict(extra="forbid")

    frame_seq: int
    text: str
    created_at: datetime


class ConversationTurnView(BaseModel):
    """A turn summary, without exposing the raw frame range yet.

    `usage` is the neutral shape every backend's adapter produces, not the CLI's own accounting
    object: the SPA renders what an exchange cost without knowing which harness ran it.
    """

    model_config = ConfigDict(extra="forbid")

    turn_id: UUID
    started_at: datetime
    ended_at: datetime | None
    outcome: TurnOutcome | None
    usage: TurnUsage | None


class ConversationSessionView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    surface: ChatSurface
    room_id: str | None
    status: SessionStatus
    error: str | None
    created_at: datetime
    updated_at: datetime
    narration: list[SetupNarrationView]
    messages: list[SessionMessageView]
    turns: list[ConversationTurnView]


# Frames per page of the inspector. A frame is usually small, but one `user` frame carries a whole
# tool result — a file read, a command's output — so the row count alone does not bound a response,
# and the browser pays again to syntax-highlight each one (`frontend/code_block.tsx`). Fifty is
# roughly two exchanges: enough that the answer to "what happened at the end" is on the first page,
# few enough that reaching it costs one request.
DEFAULT_FRAME_PAGE = 50
MAX_FRAME_PAGE = 200


class SessionFrameView(BaseModel):
    """One row of the rollout, as the console's frame inspector reads it.

    The payload is the wire, whole: this surface exists because `session_messages` is a lossy
    projection of the frame log, so clipping here would reintroduce that one level down. Bounding a
    response is the page's job, not the frame's.
    """

    model_config = ConfigDict(extra="forbid")

    frame_seq: int
    direction: FrameDirection
    kind: str
    created_at: datetime
    payload: dict[str, Any]


class SessionFramePage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frames: list[SessionFrameView]
    next_before_seq: int | None = Field(
        description="Pass back as `before_seq` for the page of earlier frames, or absent at the start of the log."
    )


def frame_page(rows: Sequence[SessionFrame], *, limit: int) -> SessionFramePage:
    """One page of rollout rows in wire order, with the cursor for the page before it.

    A short page is the first one, the same rule the MCP reader uses in the other direction:
    cheaper than a second count query, for the only question a caller has — whether to ask again.
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
    return SessionFramePage(frames=frames, next_before_seq=frames[0].frame_seq if len(frames) == limit else None)


@dataclass(frozen=True, slots=True)
class SessionToolCalls:
    """What one session's stored events say about its tool calls.

    Two indexes over the same rows, because the transcript joins to them by different keys: a
    message finds the calls it made through the frames it was built from, and a call finds its
    answer by its own id — unique within a session, so that half needs no per-message association
    at all.
    """

    asked: Sequence[tuple[int, RecordedToolCall]]
    results: Mapping[str, SessionToolResultView]

    def within(self, first: int | None, last: int | None) -> list[RecordedToolCall]:
        """The calls a message's own frames made, in the order it made them.

        `asked` is ordered by frame, so this is a slice: a message's span and the next one's do not
        overlap, which is what makes a range lookup exact where matching by position was a guess.
        """
        if first is None:
            return []
        lo = bisect_left(self.asked, first, key=_frame_of)
        hi = bisect_right(self.asked, first if last is None else last, key=_frame_of)
        return [call for _, call in self.asked[lo:hi]]


def _frame_of(asked: tuple[int, RecordedToolCall]) -> int:
    return asked[0]


async def tool_calls(db: AsyncSession, session_id: UUID) -> SessionToolCalls:
    """Read the calls and their answers out of the session's stored events.

    Until `session_events` the reply existed in no row at all: it was re-parsed out of the frame
    log on every request by a Claude frame parser — the last of the four interpreters
    (<../../plans/chat_runtime_projection.md> § The four interpreters, counted).
    """
    rows = (
        await db.scalars(
            select(SessionEvent)
            .where(SessionEvent.session_id == session_id, SessionEvent.kind.in_(TOOL_CALL_EVENT_KINDS))
            .order_by(SessionEvent.source_first_frame_seq, SessionEvent.event_seq)
        )
    ).all()
    return SessionToolCalls(
        asked=[_asked(row) for row in rows if row.kind is ConversationEventKind.TOOL_CALL_STARTED],
        results=dict(_answered(row) for row in rows if row.kind is ConversationEventKind.TOOL_CALL_COMPLETED),
    )


def _asked(row: SessionEvent) -> tuple[int, RecordedToolCall]:
    """One call, at the frame that made it.

    Both columns are guaranteed by the table — a `call_id` on exactly the tool kinds, and a frame
    range on the only arm anything writes — so a row missing either is a bug in the writer rather
    than a state to render around.
    """
    if row.call_id is None or row.source_first_frame_seq is None:
        raise ValueError(f"a tool call carries no call id or no frame: {row.event_seq=}")
    body = session_events.ToolCallBody.model_validate(row.body)
    return row.source_first_frame_seq, RecordedToolCall(
        call_id=row.call_id, tool_name=body.tool_name, arguments=dict(body.arguments)
    )


def _answered(row: SessionEvent) -> tuple[str, SessionToolResultView]:
    if row.call_id is None:
        raise ValueError(f"a tool result carries no call id: {row.event_seq=}")
    body = session_events.ToolResultBody.model_validate(row.body)
    return row.call_id, SessionToolResultView(content=_rendered(body.content), is_error=body.outcome is Outcome.FAILED)


def _rendered(content: session_events.ResultContentBody) -> Any:
    """The half of a result a transcript prints, out of the variant the event stored it as."""
    match content:
        case session_events.TextResultBody():
            return content.text
        case session_events.ToolReferencesResultBody():
            return content.tool_names
        case session_events.OpaqueResultBody():
            return content.payload


async def setup_narration(db: AsyncSession, session_id: UUID) -> list[SetupNarrationView]:
    """What the sandbox printed while bootstrapping, in the order it produced it.

    Unbounded, like the transcript beside it in the same response: narration is the shorter of the
    two in any session that got as far as answering, and in the one where it is not — a session
    that died during setup — it is the whole account, which is what a cap would truncate.
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


def message_view(message: SessionMessage, calls: SessionToolCalls) -> SessionMessageView:
    return _view(
        message,
        tool_calls=[
            SessionToolCallView(
                call_id=call.call_id,
                tool_name=call.tool_name,
                arguments=call.arguments,
                result=calls.results.get(call.call_id),
            )
            for call in calls.within(message.source_first_frame_seq, message.source_last_frame_seq)
        ],
    )


def user_message_view(message: SessionMessage) -> SessionMessageView:
    """The prompt row `enqueue_prompt` has just written, as its caller reads it back.

    Its own constructor rather than `message_view` with an empty `SessionToolCalls`, because the
    empty value was a caller asserting a fact about its message and reads equally as "I did not
    look": a prompt that has only just been accepted is a user turn nothing has answered, so
    there are no tool calls to join and no events to join them out of.
    """
    return _view(message, tool_calls=[])


def _view(message: SessionMessage, *, tool_calls: list[SessionToolCallView]) -> SessionMessageView:
    return SessionMessageView(
        message_id=message.message_id,
        role=message.role,
        status=message.status,
        content=message.content,
        tool_calls=tool_calls,
        error=message.error,
        source_first_frame_seq=message.source_first_frame_seq,
        source_last_frame_seq=message.source_last_frame_seq,
        created_at=message.created_at,
        updated_at=message.updated_at,
    )


def session_view(
    record: Session, messages: list[SessionMessage], *, responding: bool, calls: SessionToolCalls
) -> SessionView:
    """The session as the SPA reads it, with `responding` derived from an open turn.

    `status` is the frontend's contract (`frontend/x/claude_chat_page.tsx` switches on it); the
    column underneath does not carry turn state. A live session with a turn in flight reports
    `responding`; the session's own lifecycle — provisioning, closing, closed, failed — always
    wins, because a turn left open by a dead replica says nothing about a session the sweep has
    since failed.
    """
    status = SessionStatus.RESPONDING if responding and record.status == SessionStatus.READY else record.status
    return SessionView(
        session_id=record.session_id,
        status=status,
        error=record.error,
        created_at=record.created_at,
        updated_at=record.updated_at,
        provisioning=None,
        messages=[message_view(message, calls) for message in messages],
    )
