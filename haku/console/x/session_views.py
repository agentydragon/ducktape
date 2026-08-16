"""What the console's chat API returns for a session, and how stored rows become it.

The read models the SPA and the conversations inventory are typed against, together with the
projection that assembles one out of the session row, its transcript and its rollout. Nothing
here decides anything about a live session: it is handed rows and produces the shapes the
routes hand back.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from haku.console.chat_models import (
    ChatMessageRole,
    ChatMessageStatus,
    ChatSurface,
    FrameDirection,
    RecordedToolCall,
    SessionStatus,
    TurnOutcome,
)
from haku.console.database_schema import Session, SessionFrame, SessionMessage
from haku.console.x.sandbox_claims import ClaudeSandboxProvisioningView
from haku.console.x.session_frames import ASSISTANT_FRAME_KIND, PROMPT_FRAME_KIND, SETUP_OUTPUT_KIND


class SessionToolResultView(BaseModel):
    """What a tool answered, as the wire carried it.

    `content` is passed through rather than normalized: the CLI sends a bare string for most
    tools and a list of content blocks for those that return structured or mixed output, and
    collapsing the two here would be this layer deciding what a tool's output means.
    """

    model_config = ConfigDict(extra="forbid")

    content: Any
    is_error: bool = False


class SessionToolCallView(RecordedToolCall):
    """A recorded call, plus the answer that is not recorded beside it.

    Inheriting the stored model is the statement of that split. `call_id`, `tool_name` and
    `arguments` are the row; `result` is joined onto it at read time out of the frame log, which
    is the only place a result exists at all — the turn loop keeps the blocks that asked and
    drops the frames that answered.

    Absent while the call is still running, and on a turn that died before it answered, which is
    a state worth seeing rather than one to hide.
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
    """The operator-facing inventory entry for one conversation.

    This deliberately names the resource generically. Claude/Matrix are the only current
    producer values, but the console's read surface should not make either one part of its
    navigation or response shape.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    surface: ChatSurface | None
    room_id: str | None
    status: SessionStatus
    error: str | None
    created_at: datetime
    updated_at: datetime
    message_count: int
    last_message_at: datetime | None


class SetupNarrationView(BaseModel):
    """One thing the sandbox said while coming up, as the frame log recorded it.

    Positioned by `frame_seq` and by nothing else. These rows are recorded with **no frame
    identity** — the runner sends them `replayable=False`, because narration is a frame a console
    could not tell a replay of from a repeat — so the sequence is the only order there is, and two
    identical lines are two things that happened rather than one thing seen twice.
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
    cost_usd: float | None
    duration_ms: int | None
    usage: dict[str, Any] | None


class ConversationSessionView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    surface: ChatSurface | None
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
    projection of the frame log, so clipping the frame for size here would reintroduce the same
    problem one level down. Bounding a response is the page's job, not the frame's.
    """

    model_config = ConfigDict(extra="forbid")

    frame_seq: int
    direction: FrameDirection
    kind: str
    partial: bool
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
            partial=row.partial,
            created_at=row.created_at,
            payload=row.payload,
        )
        for row in rows
    ]
    return SessionFramePage(frames=frames, next_before_seq=frames[0].frame_seq if len(frames) == limit else None)


@dataclass(frozen=True, slots=True)
class RolloutCalls:
    """What one session's frame log says about tool calls.

    Two indexes over the same frames, because the transcript joins to them by different keys: an
    assistant message finds its own calls by the agent's message id, and a call finds its answer by
    its own id — unique within a session, so that half needs no per-message association at all.
    """

    by_message: Mapping[str, list[RecordedToolCall]]
    results: Mapping[str, SessionToolResultView]


async def rollout_calls(db: AsyncSession, session_id: UUID) -> RolloutCalls:
    """Read the calls and their results out of the session's rollout.

    Both live only here: `assistant` frames carry the `tool_use` blocks, `user` frames carry the
    `tool_result` blocks the turn loop drops, and `session_messages.tool_calls` is a copy of the
    first half with the second half missing.

    This is a Claude frame parser and says so — one of the four interpreters the projection work
    is counting down. What it produces is neutral: the same `RecordedToolCall` the row stores.
    """
    frames = await db.execute(
        select(SessionFrame.kind, SessionFrame.payload)
        .where(SessionFrame.session_id == session_id, SessionFrame.kind.in_([ASSISTANT_FRAME_KIND, PROMPT_FRAME_KIND]))
        .order_by(SessionFrame.frame_seq)
    )
    by_message: dict[str, list[RecordedToolCall]] = {}
    results: dict[str, SessionToolResultView] = {}
    for kind, payload in frames:
        message = payload.get("message")
        if not isinstance(message, dict):
            continue
        agent_id = message.get("id")
        for block in message.get("content", []):
            if not isinstance(block, dict):
                continue
            match block.get("type"):
                case "tool_use" if kind == ASSISTANT_FRAME_KIND and agent_id:
                    by_message.setdefault(str(agent_id), []).append(
                        RecordedToolCall(call_id=block["id"], tool_name=block["name"], arguments=block["input"])
                    )
                case "tool_result" if call_id := block.get("tool_use_id"):
                    results[str(call_id)] = SessionToolResultView(
                        content=block.get("content"), is_error=bool(block.get("is_error"))
                    )
    return RolloutCalls(by_message=by_message, results=results)


async def setup_narration(db: AsyncSession, session_id: UUID) -> list[SetupNarrationView]:
    """What the sandbox printed while bootstrapping, in the order it produced it.

    Unbounded, like the transcript beside it in the same response. Narration is the shorter of
    the two in any session that got as far as answering — and in the session where it is not,
    one that died during setup, it is the entire account of what happened, which is exactly
    what a cap would cut the end off.
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


def message_view(message: SessionMessage, calls: RolloutCalls) -> SessionMessageView:
    # The rollout where the row points into it, the column otherwise. That column is the lossy copy
    # — the calls without their answers — and is kept only for rows with nothing to point at: ones
    # that predate the pointer, and ones this console synthesized rather than observed.
    recorded = calls.by_message.get(message.agent_message_id or "")
    return _view(
        message,
        tool_calls=[
            SessionToolCallView(
                call_id=call.call_id,
                tool_name=call.tool_name,
                arguments=call.arguments,
                result=calls.results.get(call.call_id),
            )
            for call in (recorded if recorded is not None else message.tool_calls)
        ],
    )


def user_message_view(message: SessionMessage) -> SessionMessageView:
    """The prompt row `enqueue_prompt` has just written, as its caller reads it back.

    Its own constructor rather than `message_view` with an empty `RolloutCalls`, because the
    empty value was a caller asserting a fact about its message and reads equally as "I did not
    look": a prompt that has only just been accepted is a user turn nothing has answered, so
    there are no tool calls to join and no rollout to join them out of.
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
    record: Session, messages: list[SessionMessage], *, responding: bool, calls: RolloutCalls
) -> SessionView:
    """The session as the SPA reads it, with `responding` derived from an open turn.

    `status` is the frontend's contract (`frontend/x/claude_chat_page.tsx` switches on it), so
    the column underneath can stop carrying turn state without a frontend release. A live
    session with a turn in flight reports `responding`; the session's own lifecycle —
    provisioning, closing, closed, failed — always wins, because a turn left open by a replica
    that died says nothing about a session the sweep has since failed.

    The `record.status == RESPONDING` arm is the roll's other half: a replica on the previous
    image still writes that column, and its sessions have no turn rows to derive from.
    """
    live = record.status in {SessionStatus.READY, SessionStatus.RESPONDING}
    status = (
        SessionStatus.RESPONDING
        if live and (responding or record.status == SessionStatus.RESPONDING)
        else record.status
    )
    return SessionView(
        session_id=record.session_id,
        status=status,
        error=record.error,
        created_at=record.created_at,
        updated_at=record.updated_at,
        provisioning=None,
        messages=[message_view(message, calls) for message in messages],
    )
