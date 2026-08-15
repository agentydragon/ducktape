"""What the console's chat API returns for a session, and how stored rows become it.

The read models the SPA and the conversations inventory are typed against, together with the
projection that assembles one out of the session row, its transcript and its rollout. Nothing
here decides anything about a live session: it is handed rows and produces the shapes the
routes hand back.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from haku.console.chat_models import ChatMessageRole, ChatMessageStatus, ChatSurface, SessionStatus, TurnOutcome
from haku.console.database_schema import Session, SessionFrame, SessionMessage
from haku.console.x.sandbox_claims import ClaudeSandboxProvisioningView
from haku.console.x.session_frames import ASSISTANT_FRAME_KIND, PROMPT_FRAME_KIND


class SessionToolResultView(BaseModel):
    """What a tool answered, as the wire carried it.

    `content` is passed through rather than normalized: the CLI sends a bare string for most
    tools and a list of content blocks for those that return structured or mixed output, and
    collapsing the two here would be this layer deciding what a tool's output means.
    """

    model_config = ConfigDict(extra="forbid")

    content: Any
    is_error: bool = False


class SessionToolUseView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_use_id: str
    name: str
    input: dict[str, Any]
    # Absent while the call is still running, and on a turn that died before it answered — which
    # is a state worth seeing rather than one to hide. It comes from the rollout, because
    # `session_messages.tool_uses` never held it: the turn loop keeps the `tool_use` blocks
    # that asked and drops the `user` frames that answered.
    result: SessionToolResultView | None = None


class SessionMessageView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: UUID
    role: ChatMessageRole
    status: ChatMessageStatus
    content: str
    tool_uses: list[SessionToolUseView]
    error: str | None
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
    """A readable conversation: metadata, transcript, and exchange summaries."""

    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    surface: ChatSurface | None
    room_id: str | None
    status: SessionStatus
    error: str | None
    created_at: datetime
    updated_at: datetime
    messages: list[SessionMessageView]
    turns: list[ConversationTurnView]


@dataclass(frozen=True, slots=True)
class RolloutCalls:
    """What one session's frame log says about tool calls.

    Two indexes over the same frames, because the transcript joins to them by different keys: an
    assistant message finds its own calls by the agent's message id, and a call finds its answer by
    its own id — unique within a session, so that half needs no per-message association at all.
    """

    by_message: Mapping[str, list[dict[str, Any]]]
    results: Mapping[str, SessionToolResultView]


async def rollout_calls(db: AsyncSession, session_id: UUID) -> RolloutCalls:
    """Read the calls and their results out of the session's rollout.

    Both live only here: `assistant` frames carry the `tool_use` blocks, `user` frames carry the
    `tool_result` blocks the turn loop drops, and `session_messages.tool_uses` is a copy of the
    first half with the second half missing.
    """
    frames = await db.execute(
        select(SessionFrame.kind, SessionFrame.payload)
        .where(SessionFrame.session_id == session_id, SessionFrame.kind.in_([ASSISTANT_FRAME_KIND, PROMPT_FRAME_KIND]))
        .order_by(SessionFrame.frame_seq)
    )
    by_message: dict[str, list[dict[str, Any]]] = {}
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
                        {"tool_use_id": block["id"], "name": block["name"], "input": block["input"]}
                    )
                case "tool_result" if call_id := block.get("tool_use_id"):
                    results[str(call_id)] = SessionToolResultView(
                        content=block.get("content"), is_error=bool(block.get("is_error"))
                    )
    return RolloutCalls(by_message=by_message, results=results)


# Passed explicitly rather than defaulted, so a caller says it has no rollout to join against
# instead of inheriting that silently: exactly one does, and it is the row it just inserted.
NO_CALLS = RolloutCalls(by_message=MappingProxyType({}), results=MappingProxyType({}))


def message_view(message: SessionMessage, calls: RolloutCalls) -> SessionMessageView:
    # The rollout where the row points into it, the column otherwise. That column is the lossy copy
    # — the calls without their answers — and is kept only for rows with nothing to point at: ones
    # that predate the pointer, and ones this console synthesized rather than observed.
    recorded = calls.by_message.get(message.agent_message_id or "")
    return SessionMessageView(
        message_id=message.message_id,
        role=message.role,
        status=message.status,
        content=message.content,
        tool_uses=[
            SessionToolUseView.model_validate(tool_use | {"result": calls.results.get(tool_use["tool_use_id"])})
            for tool_use in (recorded if recorded is not None else message.tool_uses)
        ],
        error=message.error,
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
