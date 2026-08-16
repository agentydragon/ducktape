"""Operator chat sessions backed by Claude Code in Agent Sandbox pods."""

from __future__ import annotations

import asyncio
import collections.abc
import contextlib
import decimal
import hashlib
import json
import logging
import os
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, ClassVar, Protocol, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import CursorResult, Select, delete, func, literal, or_, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.cli_protocol.frame_identity import frame_uid
from haku.console.chat_models import (
    ENDED_SESSION_STATUSES,
    LIVE_SESSION_STATUSES,
    ChatMessageRole,
    ChatMessageStatus,
    ChatSurface,
    FrameDirection,
    PromptFate,
    SessionStatus,
    TurnOutcome,
)
from haku.console.config import ClaudeRuntimeConfig
from haku.console.database_schema import (
    Session,
    SessionFrame,
    SessionMessage,
    SessionOutbox,
    SessionPrompt,
    SessionTurn,
    SessionTurnPrompt,
)
from haku.console.operator_auth import OperatorActorDep
from haku.console.tools.conversations import (
    Conversation,
    ConversationCursor,
    ConversationPage,
    RolloutFrame,
    TurnRecord,
)
from haku.console.x.room_status import TurnStatus, ignore_clear, ignore_status
from haku.console.x.sandbox_claims import ProvisioningStep, SandboxClaims, provisioning_view
from haku.console.x.session_frames import (
    ASSISTANT_FRAME_KIND,
    DELTA_FRAME_KIND,
    PROMPT_FRAME_KIND,
    RESULT_FRAME_KIND,
    SETUP_OUTPUT_KIND,
    agent_message_id,
    assistant_frame,
    content_blocks,
    frame_kind,
    setup_output_frame,
    text_delta,
)
from haku.console.x.session_notifications import SessionEventKind, SessionNotifications, notify
from haku.console.x.session_views import (
    DEFAULT_FRAME_PAGE,
    MAX_FRAME_PAGE,
    NO_CALLS,
    ConversationSessionSummary,
    ConversationSessionView,
    ConversationTurnView,
    SessionFramePage,
    SessionMessageView,
    SessionView,
    frame_page,
    message_view,
    rollout_calls,
    session_view,
    setup_narration,
)
from haku.runtime.x.claude_bridge.cli_client import (
    ClaudeCli,
    ReceivedFrame,
    RecordedFrame,
    SentPrompt,
    cli_over_websocket,
)
from haku.runtime.x.claude_bridge.options import ClaudeSession, HttpMcpServer, build_claude_launch
from haku.runtime.x.claude_bridge.protocol import GOING_AWAY_CODE, NOT_ADMITTED_CODE, TextWebSocket

router = APIRouter(tags=["sessions"])
internal_router = APIRouter(tags=["claude-chat-internal"])
logger = logging.getLogger(__name__)

# How long a live session stays believed-in after its holder last spoke, and how often that
# holder speaks. The gap absorbs a slow database round trip or a paused event loop without
# anyone reclaiming a session that is merely busy; the TTL bounds how long a room waits
# before being told the truth. A turn itself may run far longer than the TTL — the renewal
# is a separate task precisely so a long answer does not read as a dead replica.
LEASE_TTL = timedelta(seconds=90)
LEASE_RENEW_INTERVAL = timedelta(seconds=30)
# The creator's grant, covering the gap before a runner attaches and starts renewing. Longer
# than `LEASE_TTL` because it has to cover an image pull onto a cold node.
PROVISION_LEASE = timedelta(minutes=10)
# What a replica going down cleanly leaves behind: long enough for the runner to notice the
# socket close and redial onto whichever replica is up, short enough that a session nobody comes
# back for is reclaimed promptly. Shorter than `LEASE_TTL` because nothing is holding it — this
# is a window for an adopter to appear, not a heartbeat anyone is keeping.
ADOPTION_GRACE = timedelta(seconds=45)

# This process, as the lease records its holder. Kubernetes sets HOSTNAME to the pod name, which
# is what `kubectl logs` wants as an argument — so a session that died names the thing to go read.
REPLICA = os.environ.get("HOSTNAME", "unknown")

# Appended to a turn's stored answer when the operator stopped it, and sent on its own when the
# room has already heard the turn's prose — so an abort is visible either way.
ABORTED_NOTICE = "[aborted by operator]"


def _first_message(errors: BaseExceptionGroup[Exception]) -> str:
    """The message of the first leaf in *errors*, for the operator-facing `error` column.

    `except*` hands back a group even when one thing failed, and a group's own `str` is a
    count ("1 sub-exception"), which says nothing about what broke.
    """
    leaves = errors.exceptions
    while leaves and isinstance(leaves[0], BaseExceptionGroup):
        leaves = leaves[0].exceptions
    return str(leaves[0]) if leaves else str(errors)


def _frames_of_kinds(query: Select[tuple[SessionFrame]], kinds: Sequence[str] | None) -> Select[tuple[SessionFrame]]:
    """Restrict a frame query to *kinds*, or to everything a reader means by "everything".

    **Deltas are in the log but not in the default view.** A turn streams them in the hundreds and
    each carries a few characters of an answer that arrives whole a moment later, so a reader
    asking for "everything" wants the frames, not the typing. Naming the kind is how a caller
    reading a truncated answer asks for them anyway — for the MCP reader and the console's frame
    inspector alike, which is why the policy lives here rather than in either one.
    """
    return query.where(SessionFrame.kind.in_(kinds) if kinds else SessionFrame.kind != DELTA_FRAME_KIND)


class BridgeAuthentication(StrEnum):
    """What admission has to say to a redialling runner — and there are **three** answers.

    "Not yours" and "not yet" are different, and conflating them is what made a console roll kill
    the session it was supposed to survive: the runner redials about a second after its socket
    drops, so it routinely arrives at a new replica while the dying one's lease is still valid,
    and a refusal it cannot retry costs the sandbox. `handle_runner` gives `HELD` a 5xx handshake
    response for exactly that reason.
    """

    ACCEPTED = "accepted"
    # The session is already over, so the runner should stop rather than retry.
    TERMINAL = "terminal"
    # The credential is wrong. Permanent.
    REJECTED = "rejected"
    # Another replica is still serving this session and saying so. **Transient**: it lasts at most
    # until that lease expires, and the runner that waits it out is the one adopting the session.
    HELD = "held"


class SessionPromptRequest(BaseModel):
    """What the SPA posts to send a prompt. Named for the request, since the prompt itself is now
    a row (`database_schema.SessionPrompt`) rather than a field on the way in."""

    text: str = Field(min_length=1, max_length=100_000)


@dataclass(frozen=True)
class SpaSession:
    """A session created by the browser chat view, which has no room."""

    # What the row records for this variant, carried on the variant rather than derived from it
    # by an `isinstance` chain at the one call site — where the enum and the room had to be
    # mapped separately, so a third surface would be two arms to remember rather than a field.
    surface_column: ClassVar[ChatSurface] = ChatSurface.SPA
    room_id: ClassVar[None] = None


@dataclass(frozen=True)
class MatrixSession:
    """A session created to serve one Matrix room, which it records for good.

    Carried as a variant rather than a `surface` enum beside an optional `room_id`, because
    the two combinations that pair would also admit — a Matrix session with no room, a room on
    an SPA session — are states no caller could act on. The table repeats the rule as a pair of
    check constraints, since the columns outlive this call signature.
    """

    surface_column: ClassVar[ChatSurface] = ChatSurface.MATRIX
    room_id: str


SessionSurface = SpaSession | MatrixSession


@dataclass(frozen=True, slots=True)
class TurnStart:
    """A prompt taken off the queue together with the turn opened to answer it."""

    turn_id: UUID
    message_id: UUID
    prompt: str


@dataclass(frozen=True, slots=True)
class ResumedTurn:
    """A turn a departed holder opened and asked, handed to whoever adopted the session.

    It carries nothing but the turn's name. What the departed holder got through is on the turn's
    own row (`TurnState`), so adoption reads it rather than rebuilding it out of the frame log —
    which is what stopped the live path and the recovery path being two accounts of one exchange.
    """

    turn_id: UUID


@dataclass(frozen=True, slots=True)
class TurnState:
    """How far a turn has got, as `session_turns` records it.

    Every field is written in the same transaction as the effect it describes, so this is the
    turn's state rather than a reading of its side effects — and it says the same thing to the
    process that opened the turn and to the one that inherits it.
    """

    # The assistant message being streamed into, and the text already in it. None between
    # messages: either nothing has been said yet, or the last one completed and closed.
    assistant_message_id: UUID | None
    # The empty prefix of an answer, not an absent one — `assistant_message_id` is what says
    # whether a message is open at all. It is the message row's own content, which the stream
    # writes on every delta, rather than a second copy of it kept on the turn.
    streamed: str
    said_anything: bool
    queued_reply: bool


class TurnClient(Protocol):
    """The part of the CLI client the turn loop needs after frames are handed to it."""

    async def query(self, text: str) -> SentPrompt: ...

    async def interrupt(self) -> None: ...


@dataclass(frozen=True, slots=True)
class SessionOutcome:
    """Where a session got to, and why if it ended badly.

    The two travel together because every caller acting on a dead session wants to say which.
    """

    status: SessionStatus
    error: str | None


class SessionStore:
    """Async Postgres store for agent sessions."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    @staticmethod
    def _fingerprint(token: str) -> bytes:
        return hashlib.sha256(token.encode()).digest()

    async def create(self, operator_id: UUID, surface: SessionSurface) -> tuple[SessionView, str]:
        now = datetime.now(UTC)
        session_id = uuid4()
        bridge_token = secrets.token_urlsafe(32)
        async with self._sessions.begin() as db:
            db.add(
                Session(
                    session_id=session_id,
                    operator_id=operator_id,
                    surface=surface.surface_column,
                    room_id=surface.room_id,
                    status=SessionStatus.PROVISIONING,
                    bridge_token_fingerprint=self._fingerprint(bridge_token),
                    bridge_connected_at=None,
                    error=None,
                    # Granted by the creator, not by an owner: until a runner attaches there is
                    # no replica holding this session, and a sandbox that never comes up would
                    # otherwise sit in `provisioning` — a live status — with no lease to expire
                    # and so nothing to reclaim it. The window is the provisioning budget, and
                    # the owning replica takes over renewing it once the bridge connects.
                    lease_expires_at=now + PROVISION_LEASE,
                    created_at=now,
                    updated_at=now,
                )
            )
        view = await self.get(operator_id, session_id)
        return view, bridge_token

    async def get(self, operator_id: UUID, session_id: UUID) -> SessionView:
        async with self._sessions() as db:
            record = await db.scalar(
                select(Session).where(Session.session_id == session_id, Session.operator_id == operator_id)
            )
            if record is None:
                raise KeyError(session_id)
            messages = list(
                (
                    await db.scalars(
                        select(SessionMessage)
                        .where(SessionMessage.session_id == session_id)
                        .order_by(SessionMessage.created_at, SessionMessage.message_id)
                    )
                ).all()
            )
            responding = await _open_turn(db, session_id) is not None
            return session_view(record, messages, responding=responding, calls=await rollout_calls(db, session_id))

    async def list_operator_conversations(self, operator_id: UUID, *, limit: int) -> list[ConversationSessionSummary]:
        """List this Operator's conversations for the Console inventory.

        The MCP reader intentionally remains unscoped, but a browser-facing inventory is an
        operator-owned surface and must never reveal another Operator's sessions. The aggregate
        comes from the transcript table so the list stays useful without loading every message.
        """
        async with self._sessions() as db:
            rows = (
                await db.execute(
                    select(Session, func.count(SessionMessage.message_id), func.max(SessionMessage.created_at))
                    .outerjoin(SessionMessage, SessionMessage.session_id == Session.session_id)
                    .where(Session.operator_id == operator_id)
                    .group_by(Session.session_id)
                    .order_by(Session.updated_at.desc(), Session.session_id.desc())
                    .limit(limit)
                )
            ).all()
        return [
            ConversationSessionSummary(
                session_id=session.session_id,
                surface=session.surface,
                room_id=session.room_id,
                status=session.status,
                error=session.error,
                created_at=session.created_at,
                updated_at=session.updated_at,
                message_count=message_count,
                last_message_at=last_message_at,
            )
            for session, message_count, last_message_at in rows
        ]

    async def get_operator_conversation(self, operator_id: UUID, session_id: UUID) -> ConversationSessionView:
        """Read one Operator-owned conversation: transcript, turns and bootstrap narration.

        Not the raw frame log — the narration is the one projection of it this surface carries,
        because for a session that died before the CLI produced anything it is the whole account.
        """
        view = await self.get(operator_id, session_id)
        async with self._sessions() as db:
            session = await db.scalar(
                select(Session).where(Session.session_id == session_id, Session.operator_id == operator_id)
            )
            narration = await setup_narration(db, session_id)
        if session is None:
            raise KeyError(session_id)
        turns = await self.list_turns(session_id, limit=100)
        return ConversationSessionView(
            session_id=view.session_id,
            surface=session.surface,
            room_id=session.room_id,
            status=view.status,
            error=view.error,
            created_at=view.created_at,
            updated_at=view.updated_at,
            narration=narration,
            messages=view.messages,
            turns=[
                ConversationTurnView(
                    turn_id=turn.turn_id,
                    started_at=turn.started_at,
                    ended_at=turn.ended_at,
                    outcome=turn.outcome,
                    cost_usd=turn.cost_usd,
                    duration_ms=turn.duration_ms,
                    usage=turn.usage,
                )
                for turn in turns
            ],
        )

    async def authenticate_bridge(self, session_id: UUID, token: str) -> BridgeAuthentication:
        """Admit a runner to its session — the first time, and every time after.

        **Taking the lease is the admission.** A live session admits any runner that can take its
        lease, and the lease is what stops two replicas adopting one CLI: whoever writes it under
        this row lock has it, for as long as it keeps renewing.
        """
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            record = await db.get(Session, session_id, with_for_update=True)
            if record is None or not secrets.compare_digest(record.bridge_token_fingerprint, self._fingerprint(token)):
                return BridgeAuthentication.REJECTED
            if record.status in ENDED_SESSION_STATUSES:
                return BridgeAuthentication.TERMINAL
            if record.status == SessionStatus.PROVISIONING and record.bridge_connected_at is None:
                record.bridge_connected_at = now
                record.status = SessionStatus.READY
            elif record.lease_holder not in (None, REPLICA) and record.lease_expires_at > now:
                # Somebody else is still serving this session and saying so. Turning this runner
                # away is what keeps one CLI answering to one console — but only until that lease
                # lapses, which is why it is `HELD` rather than `REJECTED`.
                return BridgeAuthentication.HELD
            record.lease_holder = REPLICA
            record.lease_expires_at = now + LEASE_TTL
            record.updated_at = now
            return BridgeAuthentication.ACCEPTED

    async def release_lease(self, session_id: UUID) -> None:
        """Hand a live session back for adoption, without declaring it dead.

        "This session is over" and "I am no longer holding it" are different, and only the second
        is true during a roll. Expiring the lease here says the second: the session is unowned as
        of now, and `expire_stale_leases` gives it the same `ADOPTION_GRACE` to be adopted that it
        gives a lease nobody released. This is a courtesy, not the mechanism — a SIGKILL runs no
        finalizer, so the sweep must be correct without it (see `expire_stale_leases`).
        """
        async with self._sessions.begin() as db:
            chat = await db.get(Session, session_id, with_for_update=True)
            if chat is not None and chat.status in LIVE_SESSION_STATUSES:
                chat.lease_holder = None
                chat.lease_expires_at = datetime.now(UTC)
                chat.updated_at = datetime.now(UTC)

    async def release_held_leases(self) -> int:
        """Hand back every live session this replica holds, and report how many.

        The graceful-shutdown counterpart to `release_lease`: that one is called per connection as
        each `handle_runner` unwinds, this one is called once from the lifespan on the way down so a
        rolling replica's sessions become adoptable at once rather than each waiting out the sweep's
        `ADOPTION_GRACE`. One statement keyed on this replica, so it is safe to run concurrently
        with the per-connection releases and idempotent if they already fired — and, unlike them, it
        does not depend on every `handle_runner` task completing its own commit while being
        cancelled, which is the guarantee a SIGKILL-free shutdown actually needs.
        """
        async with self._sessions.begin() as db:
            result = cast(
                "CursorResult[Any]",
                await db.execute(
                    update(Session)
                    .where(Session.status.in_(LIVE_SESSION_STATUSES), Session.lease_holder == REPLICA)
                    .values(lease_holder=None, lease_expires_at=datetime.now(UTC), updated_at=datetime.now(UTC))
                ),
            )
            return result.rowcount

    async def adopt_open_turn(self, session_id: UUID) -> ResumedTurn | None:
        """Say what the previous holder's open turn was, and hand back the one worth finishing.

        The sandbox outlives the replica, so an adopting console inherits the exchange too. "A
        turn was open" is three situations and only one of them is a closure:

        1. **Never asked** — no prompt frame was written. The prompt goes back on the queue.
        2. **Finished, unrecorded** — the `result` is logged but `end_turn` never ran. Closed from
           the record; waiting would be forever, since the replay of a recorded frame is refused.
        3. **Still running** — returned for `_run_turn` to finish.

        Only *which of the three* is asked of the frames. How far the answer had got is no longer
        reconstructed here at all: it is on the turn's row, and `_run_turn` reads it the same way
        whether it opened the turn or inherited it.

        Leaving a turn open is safe only because `uq_session_turns_open` permits exactly one,
        which is what stops `next_prompt` opening a second beside the inherited one.
        """
        async with self._sessions.begin() as db:
            turn = await db.scalar(
                select(SessionTurn)
                .where(SessionTurn.session_id == session_id, SessionTurn.ended_at.is_(None))
                .with_for_update()
            )
            if turn is None:
                return None
            turn_id, first_frame_seq = turn.turn_id, turn.first_frame_seq
            closing: dict[str, Any] | None = None
            if not await _prompt_left(db, session_id, first_frame_seq):
                await _requeue(db, turn_id)
                await notify(db, SessionEventKind.PROMPT, session_id)
            elif (closing := await _recorded_result(db, session_id, first_frame_seq)) is None:
                return ResumedTurn(turn_id=turn_id)
        # Cases 1 and 2. A turn that never asked has no result to close with and no outcome but
        # failure; one that finished carries its own, which `end_turn` also mines for the cost
        # and usage the previous holder never got to write.
        await self.end_turn(
            turn_id,
            TurnOutcome.ANSWERED if closing is not None and not closing.get("is_error") else TurnOutcome.FAILED,
            closing,
        )
        return None

    async def claim_cleanup_candidates(self) -> list[UUID]:
        """Return terminal sessions whose hashed rendezvous credential still marks cleanup pending."""
        async with self._sessions() as db:
            result = await db.scalars(
                select(Session.session_id).where(
                    Session.status.in_(ENDED_SESSION_STATUSES), Session.bridge_token_fingerprint != b""
                )
            )
            return list(result.all())

    async def complete_claim_cleanup(self, session_id: UUID) -> None:
        async with self._sessions.begin() as db:
            chat = await db.get(Session, session_id, with_for_update=True)
            if chat is None:
                return
            chat.bridge_token_fingerprint = b""
            if chat.status == SessionStatus.CLOSING:
                chat.status = SessionStatus.CLOSED
            chat.updated_at = datetime.now(UTC)

    async def enqueue_prompt(self, operator_id: UUID, session_id: UUID, prompt_text: str) -> SessionMessageView:
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            chat = await db.scalar(
                select(Session)
                .where(Session.session_id == session_id, Session.operator_id == operator_id)
                .with_for_update()
            )
            if chat is None:
                raise KeyError(session_id)
            if chat.status != SessionStatus.READY:
                raise RuntimeError(f"session is not ready (status={chat.status})")
            # Admission asks about the turn, not the session's status: gating on `READY` alone
            # would accept a prompt mid-turn, which is the fold-into-turn feature arriving by
            # accident with no fold path wired (R2.2 holds a batch until the turn ends).
            if await _open_turn(db, session_id) is not None:
                raise RuntimeError("a turn is already in flight")
            if await _queued_prompt(db, session_id) is not None or await _legacy_pending(db, session_id) is not None:
                raise RuntimeError("a prompt is already queued")
            # Still minted here, and still `pending`: the transcript row is what the SPA gets back
            # from this call, and a replica on the previous image dequeues by finding that status.
            # Both stop being true in the contract release, where the row is written final and the
            # queue row alone says it is waiting.
            message = SessionMessage(
                message_id=uuid4(),
                session_id=session_id,
                role=ChatMessageRole.USER,
                status=ChatMessageStatus.PENDING,
                content=prompt_text,
                tool_uses=[],
                error=None,
                created_at=now,
                updated_at=now,
            )
            db.add(message)
            db.add(
                SessionPrompt(prompt_id=uuid4(), session_id=session_id, message_id=message.message_id, queued_at=now)
            )
            # No status write: a queued prompt is not a turn in flight. Setting `responding`
            # here is what let `request_abort` accept an abort for a turn that did not exist.
            chat.updated_at = now
            await notify(db, SessionEventKind.PROMPT, session_id)
            await notify(db, SessionEventKind.UPDATE, session_id)
        return message_view(message, NO_CALLS)

    async def prompt_fate(self, message_id: UUID) -> PromptFate:
        """Say whether an accepted prompt is still coming, has been through a turn, or is stranded.

        For a surface that has not yet acknowledged the prompt's source — Matrix ingress holds its
        `/sync` watermark on this (R2.5). What it is asking is the question `enqueue_prompt`
        returning cannot answer: a session can accept a prompt and then end before anything claims
        it, and the supervisor's replacement session is a different `session_id`, so the row is
        left where nothing will ever look.

        The turn is read through `session_turn_prompts`, which is the only durable statement that
        *this* prompt was what a turn ran; the session's status decides the rest, because an open
        turn on a session nobody holds is one nothing will close. A prompt whose transcript row
        has gone (its session was deleted) is stranded by the same reasoning.
        """
        async with self._sessions() as db:
            row = (
                await db.execute(
                    select(SessionTurn.ended_at, Session.status)
                    .select_from(SessionMessage)
                    .join(Session, Session.session_id == SessionMessage.session_id)
                    .outerjoin(SessionTurnPrompt, SessionTurnPrompt.message_id == SessionMessage.message_id)
                    .outerjoin(SessionTurn, SessionTurn.turn_id == SessionTurnPrompt.turn_id)
                    .where(SessionMessage.message_id == message_id)
                    .order_by(SessionTurn.ended_at.desc().nullslast())
                )
            ).first()
        if row is None:
            return PromptFate.LOST
        ended_at, status = row
        if ended_at is not None:
            return PromptFate.COMPLETED
        return PromptFate.IN_FLIGHT if status in LIVE_SESSION_STATUSES else PromptFate.LOST

    async def next_prompt(self, session_id: UUID) -> TurnStart | None:
        """Take the queued prompt and open the turn that will answer it, or None if there is none.

        Dequeue and open are one transaction on purpose: they are the same event — the harness
        handing the agent a prompt — and splitting them would leave a window in which the prompt
        is claimed with no turn to name it, which is exactly what admission and abort now ask
        about.
        """
        async with self._sessions.begin() as db:
            chat = await db.get(Session, session_id, with_for_update=True)
            if chat is None or chat.status in ENDED_SESSION_STATUSES:
                return None
            now = datetime.now(UTC)
            # The queue first, then the transcript scan it replaces. Both, for the length of one
            # roll: a prompt an old replica accepted exists only as a `pending` message row, and
            # dropping the scan now would leave it accepted and never answered.
            if (queued := await _queued_prompt(db, session_id, lock=True)) is not None:
                queued.claimed_at = now
                message = await db.get(SessionMessage, queued.message_id)
                if message is None:
                    # The row the queue points at is gone, so there is no prompt to run and no
                    # text to run it with. Claiming it anyway is what stops the session retrying
                    # a prompt it can never read.
                    logger.error("prompt %s has no message row", queued.prompt_id)
                    return None
            elif (message := await _legacy_pending(db, session_id, lock=True)) is None:
                return None
            message.status = ChatMessageStatus.COMPLETE
            message.updated_at = now
            chat.updated_at = now
            # The bracket's lower bound, taken before the prompt reaches the CLI so every frame
            # the exchange produces falls inside it.
            highest = await db.scalar(
                select(func.max(SessionFrame.frame_seq)).where(SessionFrame.session_id == session_id)
            )
            turn_id = uuid4()
            db.add(
                SessionTurn(turn_id=turn_id, session_id=session_id, first_frame_seq=(highest or 0) + 1, started_at=now)
            )
            db.add(SessionTurnPrompt(turn_id=turn_id, message_id=message.message_id))
            await notify(db, SessionEventKind.UPDATE, session_id)
            return TurnStart(turn_id=turn_id, message_id=message.message_id, prompt=message.content)

    async def turn_state(self, turn_id: UUID) -> TurnState:
        """How far *turn_id* has got, read off its row.

        The one place `_run_turn` learns what has already happened, so a turn this process opened
        a moment ago and one a departed replica left half answered are the same question with the
        same answer — an empty state being what a turn that has done nothing yet honestly has.
        """
        async with self._sessions() as db:
            row = (
                await db.execute(
                    select(SessionTurn, SessionMessage.content)
                    .outerjoin(SessionMessage, SessionMessage.message_id == SessionTurn.assistant_message_id)
                    .where(SessionTurn.turn_id == turn_id)
                )
            ).first()
            if row is None:
                raise KeyError(turn_id)
            turn, streamed = row
            return TurnState(
                assistant_message_id=turn.assistant_message_id,
                streamed=streamed or "",
                said_anything=turn.said_anything,
                queued_reply=turn.queued_reply,
            )

    async def end_turn(self, turn_id: UUID, outcome: TurnOutcome, result: dict[str, Any] | None = None) -> None:
        """Close *turn_id*, taking the bracket's upper bound and what the `result` frame reported.

        Idempotent on an already-closed turn: a second close must not overwrite the first
        outcome, because the first one is the one that happened.
        """
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            turn = await db.get(SessionTurn, turn_id, with_for_update=True)
            if turn is None or turn.ended_at is not None:
                return
            turn.last_frame_seq = await db.scalar(
                select(func.max(SessionFrame.frame_seq)).where(SessionFrame.session_id == turn.session_id)
            )
            turn.ended_at = now
            turn.outcome = outcome
            if result is not None:
                # `total_cost_usd` is a float on the wire; through `Decimal(str(...))` rather than
                # `Decimal(float)`, which would carry the binary representation's noise into a
                # column that is exact on purpose.
                if isinstance(cost := result.get("total_cost_usd"), int | float):
                    turn.cost_usd = decimal.Decimal(str(cost))
                if isinstance(usage := result.get("usage"), dict):
                    turn.usage = usage
                if isinstance(duration := result.get("duration_ms"), int):
                    turn.duration_ms = duration
            chat = await db.get(Session, turn.session_id)
            if chat is not None:
                # `responding` is derived from this turn being open, so closing it is what
                # retires the state — and what the SPA has to be told about. The column is only
                # written back when it still carries the old meaning, which a replica on the
                # previous image is what would have put there.
                if chat.status == SessionStatus.RESPONDING:
                    chat.status = SessionStatus.READY
                chat.updated_at = now
                await notify(db, SessionEventKind.UPDATE, turn.session_id)

    async def list_turns(self, session_id: UUID, *, limit: int) -> list[TurnRecord]:
        """A session's exchanges, newest first, for the `haku_conversations` read tools."""
        async with self._sessions() as db:
            rows = (
                await db.scalars(
                    select(SessionTurn)
                    .where(SessionTurn.session_id == session_id)
                    .order_by(SessionTurn.started_at.desc())
                    .limit(limit)
                )
            ).all()
        return [
            TurnRecord(
                turn_id=row.turn_id,
                first_frame_seq=row.first_frame_seq,
                last_frame_seq=row.last_frame_seq,
                started_at=row.started_at,
                ended_at=row.ended_at,
                outcome=row.outcome,
                cost_usd=float(row.cost_usd) if row.cost_usd is not None else None,
                duration_ms=row.duration_ms,
                usage=row.usage,
            )
            for row in rows
        ]

    async def record_frame(
        self, session_id: UUID, direction: FrameDirection, kind: str, payload: dict[str, Any]
    ) -> RecordedFrame:
        """Append one frame to the session's rollout, unless this session already has it.

        `fresh` says whether the caller should act on the frame; `frame_seq` is the row's sequence
        either way, which is what a projection built from this frame points back at. **False means
        a replay** — the same agent-assigned identity already in this log — and the caller must
        then not act on it again. A frame with no identity is always recorded, since "no identity"
        is not "the same as the last one" (`frame_identity.frame_uid`).

        *kind* is passed rather than read out of the payload: a CLI frame keeps its discriminator
        in `type` and the bridge envelope keeps it in `kind`, and this table holds both.

        Failures are not swallowed — a rollout with quiet holes is the record that looks complete
        while being wrong.
        """
        uid = frame_uid(kind, payload)
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            # `ON CONFLICT DO NOTHING` against the partial unique index rather than a read
            # followed by a write: two replicas can be replaying the same buffer at once during
            # an adoption, and a check-then-insert would let both through.
            insert = (
                pg_insert(SessionFrame)
                .values(
                    session_id=session_id,
                    direction=direction,
                    kind=kind,
                    payload=payload,
                    partial=False,
                    frame_uid=uid,
                    created_at=now,
                    updated_at=now,
                )
                # `index_where` as well as the columns, because the index is partial and Postgres
                # will not infer one without its predicate. A row whose `frame_uid` is NULL does
                # not satisfy that predicate, so it is simply inserted — which is the behaviour
                # "no identity is not the same as the last one" needs.
                .on_conflict_do_nothing(
                    index_elements=["session_id", "frame_uid"], index_where=text("frame_uid IS NOT NULL")
                )
            )
            inserted = await db.execute(insert.returning(SessionFrame.frame_seq))
            if (inserted_seq := inserted.scalar_one_or_none()) is not None:
                return RecordedFrame(fresh=True, frame_seq=int(inserted_seq))
            # Nothing was inserted, so the partial index found this frame's identity already in
            # the log — which is the only way `DO NOTHING` fires, since an identity-less frame
            # does not satisfy the index predicate. Read back the row it collided with, so a
            # replay still names the frame it duplicates.
            existing_seq = await db.scalar(
                select(SessionFrame.frame_seq).where(
                    SessionFrame.session_id == session_id, SessionFrame.frame_uid == uid
                )
            )
            if existing_seq is None:
                raise RuntimeError(f"replayed frame disappeared from the rollout for {uid=}")
        return RecordedFrame(fresh=False, frame_seq=int(existing_seq))

    async def list_conversations(self, *, after: ConversationCursor | None, limit: int) -> ConversationPage:
        """One page of past sessions, newest first, for the `haku_conversations` read tools.

        Keyset paging on `(created_at, session_id)`, for the same reason `read_frames` pages on
        `frame_seq`: an offset counts from the top of the order, and this order grows at the top
        while a reader walks it, so every session created mid-walk would push a row across a page
        boundary — skipping it or repeating it. `session_id` is in the key because `created_at`
        alone is not a total order; a pair created in one instant would straddle the boundary.

        Unscoped by R5.3a: every session, whichever room it served.
        """
        query = select(Session).order_by(Session.created_at.desc(), Session.session_id.desc())
        if after is not None:
            query = query.where(
                tuple_(Session.created_at, Session.session_id)
                < tuple_(literal(after.created_at), literal(after.session_id))
            )
        async with self._sessions() as db:
            # One row past the limit, so a full page that happens to be the last one says so
            # rather than handing back a cursor onto nothing.
            rows = (await db.scalars(query.limit(limit + 1))).all()
        conversations = [
            Conversation(
                session_id=row.session_id,
                surface=row.surface,
                room_id=row.room_id,
                status=row.status,
                created_at=row.created_at,
                error=row.error,
            )
            for row in rows[:limit]
        ]
        return ConversationPage(
            conversations=conversations,
            next_cursor=ConversationCursor.of(conversations[-1]) if len(rows) > limit else None,
        )

    async def read_frames(
        self, session_id: UUID, *, after_seq: int | None, limit: int, kinds: Sequence[str] | None
    ) -> list[RolloutFrame]:
        """One page of a session's rollout, in wire order, from the start of the log onwards.

        Keyset paging on `frame_seq` rather than an offset: the log is append-only, so a cursor
        cannot skip or repeat a row the way an offset would once new frames land between pages.
        """
        query = _frames_of_kinds(select(SessionFrame).where(SessionFrame.session_id == session_id), kinds)
        if after_seq is not None:
            query = query.where(SessionFrame.frame_seq > after_seq)
        async with self._sessions() as db:
            rows = (await db.scalars(query.order_by(SessionFrame.frame_seq).limit(limit))).all()
        return [
            RolloutFrame(
                frame_seq=row.frame_seq,
                direction=row.direction,
                kind=row.kind,
                created_at=row.created_at,
                payload=row.payload,
                partial=row.partial,
            )
            for row in rows
        ]

    async def read_operator_frames(
        self, operator_id: UUID, session_id: UUID, *, before_seq: int | None, limit: int, kinds: Sequence[str] | None
    ) -> SessionFramePage:
        """The tail of an Operator-owned session's rollout, for the console's frame inspector.

        Two things differ from `read_frames`, which serves the MCP reader. It is scoped, because a
        browser surface must never read another Operator's session. And its keyset runs backwards:
        the frames an operator opens this for are a session's *last* ones — an answer that was cut
        off, a turn that died — so paging forward from frame one to reach them is exactly the
        punishment a long session must not carry. The rows still come back in wire order; only
        which page is the first one differs.

        **Where per-message provenance hooks in** (`agent/claude-frame-provenance`, #4105): a
        message's inclusive `frame_seq` range is a bound on this same query, and `before_seq` is
        already its upper half — so linking a message to its frames stays a filter over this view
        rather than a second read path.
        """
        query = _frames_of_kinds(select(SessionFrame).where(SessionFrame.session_id == session_id), kinds)
        if before_seq is not None:
            query = query.where(SessionFrame.frame_seq < before_seq)
        async with self._sessions() as db:
            owned = await db.scalar(
                select(Session.session_id).where(Session.session_id == session_id, Session.operator_id == operator_id)
            )
            if owned is None:
                raise KeyError(session_id)
            rows = (await db.scalars(query.order_by(SessionFrame.frame_seq.desc()).limit(limit))).all()
        return frame_page(list(reversed(rows)), limit=limit)

    async def update_partial_frame(self, session_id: UUID, text: str) -> None:
        """Record the assistant message streaming right now, replacing any earlier state of it.

        Takes its `frame_seq` when the stream opens, so it sits where it belongs in the log
        even though it is rewritten afterwards.

        CLEANUP(added 2026-08-15): Superseded by the deltas themselves, which this row existed to
        stand in for. Stop writing it — with `clear_partial_frame`, the `partial` column and its
        two indexes — once every replica runs an image that records them, one roll after this
        ships. Not in this release: an old replica writing a row a new one never clears would
        leave a stray partial in the rollout for good.
        """
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            partial = await db.scalar(
                select(SessionFrame).where(SessionFrame.session_id == session_id, SessionFrame.partial)
            )
            if partial is None:
                db.add(
                    SessionFrame(
                        session_id=session_id,
                        direction=FrameDirection.FROM_AGENT,
                        kind=ASSISTANT_FRAME_KIND,
                        payload=assistant_frame(text),
                        partial=True,
                        created_at=now,
                        updated_at=now,
                    )
                )
                return
            partial.payload = assistant_frame(text)
            partial.updated_at = now

    async def clear_partial_frame(self, session_id: UUID) -> None:
        """Drop the reconstruction, now that the frame it stood in for has arrived."""
        async with self._sessions.begin() as db:
            await db.execute(delete(SessionFrame).where(SessionFrame.session_id == session_id, SessionFrame.partial))

    async def begin_assistant(
        self, session_id: UUID, turn_id: UUID, *, source_first_frame_seq: int | None = None
    ) -> UUID:
        """Open the message this turn is about to stream into, and point the turn at it.

        One transaction, because the pointer is what makes the message the *turn's*: a replica
        dying with a message row nothing names would leave its replacement to guess which of the
        session's messages it was in the middle of.

        *source_first_frame_seq* is the frame that opened the message, written here rather than on
        every update: this is the one moment that knows where the message began, and a resumed turn
        picks its message up from the turn row without passing through here — so leaving `first` to
        a later write would walk it forward past the frames the earlier process already projected.
        """
        now = datetime.now(UTC)
        message_id = uuid4()
        async with self._sessions.begin() as db:
            db.add(
                SessionMessage(
                    message_id=message_id,
                    session_id=session_id,
                    role=ChatMessageRole.ASSISTANT,
                    status=ChatMessageStatus.STREAMING,
                    content="",
                    tool_uses=[],
                    error=None,
                    source_first_frame_seq=source_first_frame_seq,
                    created_at=now,
                    updated_at=now,
                )
            )
            # Flushed before the turn names it: the pointer is a foreign key, so the message has
            # to exist by the time the update lands.
            await db.flush()
            if (turn := await db.get(SessionTurn, turn_id)) is None:
                raise KeyError(turn_id)
            turn.assistant_message_id = message_id
        return message_id

    async def update_assistant(
        self,
        session_id: UUID,
        message_id: UUID,
        content: str,
        *,
        tool_uses: list[dict[str, Any]] | None = None,
        agent_message_id: str | None = None,
        source_last_frame_seq: int | None = None,
        complete: bool = False,
    ) -> bool:
        """Write what this assistant message says so far. True once the room owes it.

        **A completed message queues the room's copy in this same transaction.** Writing it at
        delivery time instead is what let a turn raising between producing text and speaking it
        lose the answer entirely (<../debug/message_drops.md> E4): the message row committed, the
        room was never told, and nothing afterwards knew there had been anything to say. Here the
        two facts commit together or not at all, and `session_outbox` is what says the room is
        still owed one.

        **And it closes the turn's state in that same transaction** — the turn that this message
        belongs to is the one pointing at it, so no caller has to name it and the three writes
        cannot come apart. Which is the property `spoke` needs: `queued_reply` is set by the
        statement that inserts the outbox row, never by one that merely tried.
        """
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            message = await db.get(SessionMessage, message_id)
            chat = await db.get(Session, session_id)
            if message is None or chat is None:
                return False
            message.content = content
            if tool_uses is not None:
                message.tool_uses = tool_uses
            if agent_message_id is not None:
                message.agent_message_id = agent_message_id
            # Only the far end moves here. `begin_assistant` set the near end once, and frames
            # within a message arrive in order, so this only ever widens the range.
            if source_last_frame_seq is not None:
                message.source_last_frame_seq = source_last_frame_seq
            message.status = ChatMessageStatus.COMPLETE if complete else ChatMessageStatus.STREAMING
            message.updated_at = now
            # No `chat.status = RESPONDING` here. This runs per stream delta, so it was a
            # session-row write per delta to hold a flag true that the open turn already states.
            chat.updated_at = now
            await notify(db, SessionEventKind.UPDATE, session_id)
            if not complete:
                return False
            owed = await _enqueue_reply(
                db,
                chat,
                content,
                message_id=message_id,
                agent_message_id=message.agent_message_id,
                turn_id=None,
                now=now,
            )
            await db.execute(
                update(SessionTurn)
                .where(SessionTurn.assistant_message_id == message_id)
                .values(
                    assistant_message_id=None,
                    said_anything=True,
                    queued_reply=or_(SessionTurn.queued_reply, literal(owed)),
                )
            )
            return owed

    async def set_message_source_frames(self, session_id: UUID, message_id: UUID, frame_seq: int | None) -> None:
        """Point an already-written message at the single frame it went out as.

        For the operator's own prompt, whose row exists before the frame does. A client keeping no
        rollout has no sequence to give, and the row then stays honestly unpointed.
        """
        if frame_seq is None:
            return
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            message = await db.get(SessionMessage, message_id)
            if message is None or message.session_id != session_id:
                return
            message.source_first_frame_seq = frame_seq
            message.source_last_frame_seq = frame_seq
            message.updated_at = now
            await notify(db, SessionEventKind.UPDATE, session_id)

    async def enqueue_turn_reply(self, session_id: UUID, turn_id: UUID, text: str) -> bool:
        """Queue a turn's last word, the one reply no transcript row holds. True if it is owed.

        Two callers, and at most one of them per turn: text that arrived only on the `result`
        frame after the turn's last assistant message said nothing, and the notice an aborted
        turn leaves. `turn_id` is the idempotence key that makes re-derivation by a replacement
        replica a no-op rather than a second copy in the room — see `session_outbox.turn_id`.

        False for an empty body and for a session serving no room; the SPA reads the message rows
        this turn already wrote, so it is owed nothing here.
        """
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            if (chat := await db.get(Session, session_id)) is None:
                return False
            owed = await _enqueue_reply(
                db, chat, text, message_id=None, agent_message_id=None, turn_id=turn_id, now=now
            )
            if owed:
                await db.execute(update(SessionTurn).where(SessionTurn.turn_id == turn_id).values(queued_reply=True))
            return owed

    async def fail(self, session_id: UUID, error: str, message_id: UUID | None = None) -> None:
        # Logged as well as persisted. The column is the operator-facing record, but it is not
        # reachable from `kubectl logs`, and a Matrix session that dies leaves no other trace —
        # the room just stops answering. Diagnosing the asyncpg/psycopg listener mismatch that
        # killed every session meant querying this column out of Postgres by hand, purely
        # because the reason was written where logs are not.
        logger.error("session %s failed: %s", session_id, error)
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            chat = await db.get(Session, session_id)
            if chat is not None and chat.status not in {SessionStatus.CLOSING, SessionStatus.CLOSED}:
                chat.status = SessionStatus.FAILED
                chat.error = error
                chat.updated_at = now
                await notify(db, SessionEventKind.UPDATE, session_id)
            if message_id is not None:
                message = await db.get(SessionMessage, message_id)
                if message is not None:
                    message.status = ChatMessageStatus.FAILED
                    message.error = error
                    message.updated_at = now

    async def request_close(self, operator_id: UUID, session_id: UUID) -> None:
        """Ask this session to end, and wake whoever is running it.

        `CLOSING` is an ended status, so the turn loop stops as soon as it re-reads one — but it
        re-reads only after a wake, and is otherwise parked in a 30-second prompt timeout. So the
        `PROMPT` notify is what makes teardown prompt rather than eventual; without it a closing
        session's runner holds its sandbox for the rest of that wait.
        """
        async with self._sessions.begin() as db:
            chat = await db.scalar(
                select(Session)
                .where(Session.session_id == session_id, Session.operator_id == operator_id)
                .with_for_update()
            )
            if chat is None:
                raise KeyError(session_id)
            chat.status = SessionStatus.CLOSING
            chat.updated_at = datetime.now(UTC)
            await notify(db, SessionEventKind.PROMPT, session_id)
            await notify(db, SessionEventKind.UPDATE, session_id)

    async def room_of(self, session_id: UUID) -> str | None:
        """The room this session was created to serve, or None if it serves none.

        The session's own record of it, not the current binding in `matrix_conversation`: that
        one moves to the next session the moment this one is replaced, so asking it "is this
        session mine?" answers about the room's present, not about the session.
        """
        async with self._sessions() as db:
            return await db.scalar(select(Session.room_id).where(Session.session_id == session_id))

    async def outcome(self, session_id: UUID) -> SessionOutcome | None:
        async with self._sessions() as db:
            chat = await db.get(Session, session_id)
            return None if chat is None else SessionOutcome(status=chat.status, error=chat.error)

    async def status(self, session_id: UUID) -> SessionStatus | None:
        outcome = await self.outcome(session_id)
        return outcome.status if outcome is not None else None

    async def renew_lease(self, session_id: UUID) -> None:
        """Assert that this replica still holds *session_id* and is still working on it.

        Writes the holder as well as the deadline, because the renewal *is* the claim: the row
        goes from the creator's unheld provisioning grant to this pod's heartbeat the first time
        the replica running the turn says so, and nothing else has to sequence that.
        """
        async with self._sessions.begin() as db:
            chat = await db.get(Session, session_id)
            if chat is not None and chat.status in LIVE_SESSION_STATUSES:
                chat.lease_expires_at = datetime.now(UTC) + LEASE_TTL
                chat.lease_holder = REPLICA

    async def expire_stale_leases(self) -> int:
        """Fail every live session nobody came back for, and report how many.

        A live status is only ever corrected by the replica that wrote it, so a replica dying
        without its finalizer — SIGKILL, OOM, node loss — leaves a row claiming a turn is in
        flight that `supervise_once` reads as healthy. This is the only observer that is not that
        process.

        **An expired lease means unowned, not dead**, and the threshold below is the whole of that
        distinction. `authenticate_bridge` already admits any runner once the lease has lapsed —
        it refuses only while somebody else's is still valid — so an expired session is adoptable
        without anything having to hand it back. What it is not is *instantly* adopted: the runner
        redials on a backoff, and failing the row the moment the lease lapses beat that redial
        every time. Measured 2026-08-15: `release_lease` is a finalizer and did not run on any
        roll, so every roll took this path and every roll cost the session.

        So a session is dead only once it has been adoptable for a whole `ADOPTION_GRACE` and
        nobody took it. `release_lease` becomes what it should always have been — the fast path
        that skips the wait, not the thing correctness rests on, which no finalizer can be.

        Set-based and idempotent, like `node_daemons._expire`: any replica may run it, concurrent
        runners converge, and a merely slow owner renews well before the TTL.
        """
        async with self._sessions.begin() as db:
            expired = (
                await db.scalars(
                    select(Session.session_id).where(
                        Session.status.in_(LIVE_SESSION_STATUSES),
                        Session.lease_expires_at <= datetime.now(UTC) - ADOPTION_GRACE,
                    )
                )
            ).all()
            for session_id in expired:
                # Row-at-a-time rather than one UPDATE: `notify` is per session, and a room
                # that is not told its session died is exactly the silence being fixed here.
                chat = await db.get(Session, session_id, with_for_update=True)
                if chat is None or chat.status not in LIVE_SESSION_STATUSES:
                    continue
                # Say what actually happened, not one hardcoded sentence for three different
                # events. `lease_holder` set means a replica died without handing it back; cleared
                # but `bridge_connected_at` set means a runner was here and released/dropped and
                # nobody re-adopted (a roll, or the sandbox reaching its TTL) — the common case,
                # and the one the old "no replica (never attached)" got exactly backwards; neither
                # set means the sandbox never connected. "mid-turn" only if a turn was in fact open.
                mid_turn = " mid-turn" if chat.status == SessionStatus.RESPONDING else ""
                if chat.lease_holder is not None:
                    detail = f"the console replica holding it ({chat.lease_holder}) went away"
                elif chat.bridge_connected_at is not None:
                    detail = "its runner went away and no replica took it back over"
                else:
                    detail = "a runner never attached"
                logger.error("session %s lease expired: %s", session_id, detail)
                chat.status = SessionStatus.FAILED
                chat.error = f"console session ended{mid_turn}: {detail}"
                chat.updated_at = datetime.now(UTC)
                await notify(db, SessionEventKind.UPDATE, session_id)
            return len(expired)

    async def closed(self, session_id: UUID) -> None:
        async with self._sessions.begin() as db:
            chat = await db.get(Session, session_id)
            if chat is not None and chat.status != SessionStatus.FAILED:
                chat.status = SessionStatus.CLOSED
                chat.updated_at = datetime.now(UTC)
                await notify(db, SessionEventKind.UPDATE, session_id)

    async def session_exists(self, operator_id: UUID, session_id: UUID) -> bool:
        async with self._sessions() as db:
            return (
                await db.scalar(
                    select(Session.session_id).where(
                        Session.session_id == session_id, Session.operator_id == operator_id
                    )
                )
                is not None
            )

    async def request_abort(self, session_id: UUID) -> bool:
        """Ask whichever replica is running this session's turn to interrupt it.

        Returns False when no turn is in flight. This goes through NOTIFY rather than an
        in-process registry because the two ends land on different replicas: the abort event
        belongs to the pod holding the runner's bridge websocket, while the operator's HTTP
        request is balanced across all of them.
        """
        async with self._sessions.begin() as db:
            if await _open_turn(db, session_id) is None:
                return False
            await notify(db, SessionEventKind.ABORT, session_id)
            return True


class StarletteTextWebSocket(TextWebSocket):
    def __init__(self, websocket: WebSocket):
        self._websocket = websocket

    async def send_text(self, data: str) -> None:
        await self._websocket.send_text(data)

    async def receive_text(self) -> str:
        return await self._websocket.receive_text()

    async def close(self) -> None:
        await self._websocket.close()


class RolloutRecorder:
    """One session's `FrameSink`: every protocol frame either way, into `session_frames`.

    **No exclusions.** Control frames are kept because an interrupt that did not take is only
    diagnosable from them, and deltas because a log with a hole in it cannot be folded over
    (<../../plans/chat_runtime_projection.md>). `read_frames` is where "do not bury the reader"
    is answered, by leaving deltas out of its default view.
    """

    def __init__(self, store: SessionStore, session_id: UUID):
        self._store = store
        self._session_id = session_id

    async def sent(self, payload: dict[str, Any]) -> int:
        return (await self._record(FrameDirection.TO_AGENT, payload)).frame_seq

    async def received(self, payload: dict[str, Any]) -> RecordedFrame:
        """Record the frame, answering whether the caller should act on it and where it landed.

        A delta has no agent-assigned identity, so it is always recorded and always fresh — safe
        because the runner never replays one (`runner.DELTA_TYPE`).
        """
        return await self._record(FrameDirection.FROM_AGENT, payload)

    async def _record(self, direction: FrameDirection, payload: dict[str, Any]) -> RecordedFrame:
        return await self._store.record_frame(self._session_id, direction, frame_kind(payload), payload)


class RoomSurface(Protocol):
    """The front end for sessions that serve a room, for the parts a turn cannot do itself.

    The SPA needs none of this — its client reads the message rows over SSE, so a finished turn is
    delivered by being written down. A room has to be spoken to.

    **The service picks this by reading the session's `surface`**, rather than offering every
    session to every listener and letting each one re-derive whether it is its own.

    **Replies are not here any more.** They are rows in `session_outbox`, written where they are
    produced and drained into the room by whoever holds the outbox lock, which is what a surface
    reporting success at enqueue could never be (<../debug/message_drops.md>). What is left is
    what genuinely describes a moment and is worthless afterwards.
    """

    async def system_prompt(self, session_id: UUID, room_id: str) -> str: ...

    async def report_silent_turn(self, room_id: str) -> None: ...

    async def report(self, room_id: str, detail: str) -> None: ...

    async def show_status(self, room_id: str, text: str) -> None: ...

    async def clear_status(self, room_id: str) -> None: ...

    async def set_typing(self, room_id: str, active: bool) -> None: ...


class SessionService:
    def __init__(
        self,
        config: ClaudeRuntimeConfig,
        store: SessionStore,
        claims: SandboxClaims,
        notifications: SessionNotifications,
        *,
        mcp_token: SecretStr,
        room_surface: RoomSurface | None = None,
    ):
        self._config = config
        self._store = store
        self._claims = claims
        self._notifications = notifications
        self._mcp_token = mcp_token
        self._room_surface = room_surface

    async def request_abort(self, operator_id: UUID, session_id: UUID) -> bool:
        """Interrupt this session's turn, or answer False when it has none.

        Raises `KeyError` for a session this Operator does not own, so the route asks one
        question instead of reaching through `service._store` for an ownership check and then
        asking the service for the abort.
        """
        if not await self._store.session_exists(operator_id, session_id):
            raise KeyError(session_id)
        return await self._store.request_abort(session_id)

    async def create(self, operator_id: UUID, surface: SessionSurface) -> SessionView:
        view, token = await self._store.create(operator_id, surface)
        try:
            await self._claims.create(
                session_id=view.session_id,
                bridge_token=token,
                expires_at=datetime.now(UTC) + timedelta(seconds=self._config.session_ttl_seconds),
            )
        except Exception as error:
            await self._store.fail(view.session_id, f"sandbox provisioning failed: {error}")
            # If claim creation reached Kubernetes before its response failed, remove the partial
            # resource now. A failed delete leaves the rendezvous hash as a durable retry marker.
            await self._cleanup_terminal_claim(view.session_id)
            raise
        return await self._with_provisioning(view)

    async def get(self, operator_id: UUID, session_id: UUID) -> SessionView:
        view = await self._store.get(operator_id, session_id)
        return await self._with_provisioning(view)

    async def _with_provisioning(self, view: SessionView) -> SessionView:
        if view.status != SessionStatus.PROVISIONING:
            return view
        try:
            provisioning = await self._claims.inspect(session_id=view.session_id)
        except Exception as error:
            provisioning = provisioning_view(
                f"claude-{view.session_id.hex}", step=ProvisioningStep.CLAIM_CREATED, observation_error=str(error)
            )
        return view.model_copy(update={"provisioning": provisioning})

    async def dispose(self, operator_id: UUID, session_id: UUID) -> None:
        await self._store.request_close(operator_id, session_id)
        await self._claims.delete(session_id=session_id)
        await self._store.complete_claim_cleanup(session_id)

    async def reconcile_terminal_claims(self) -> None:
        """Finish idempotent claim cleanup left behind by an interrupted Console process."""

        session_ids = await self._store.claim_cleanup_candidates()
        for session_id in session_ids:
            await self._cleanup_terminal_claim(session_id)

    async def _cleanup_terminal_claim(self, session_id: UUID) -> bool:
        try:
            await self._claims.delete(session_id=session_id)
        except Exception as error:
            # Keep the credential fingerprint as a durable cleanup-pending marker so another
            # replica or a later restart retries. Kubernetes deletion is idempotent.
            logger.warning("Claude claim cleanup failed for session %s: %s", session_id, error)
            return False
        await self._store.complete_claim_cleanup(session_id)
        return True

    async def _room_of(self, session_id: UUID) -> str | None:
        """The room this session serves, or None for one that serves no room.

        Read once per runner connection and carried for the session's life: it is immutable on
        the row, so re-reading it would only add round trips.
        """
        return None if self._room_surface is None else await self._store.room_of(session_id)

    async def _appended_prompt(self, session_id: UUID, room_id: str | None) -> str | None:
        """Who this session is, appended to Claude Code's own system prompt.

        Appended rather than replacing it: the built-ins (Read, Bash, Edit) are live in the
        sandbox and the preset is what tells the model how to drive them. Haku's identity is an
        addition to that, not a substitute for it — which is why the launch sends
        `--append-system-prompt` and never `--system-prompt`.
        """
        if self._room_surface is None or room_id is None:
            return None
        return await self._room_surface.system_prompt(session_id, room_id)

    def _turn_status(self, room_id: str | None) -> TurnStatus:
        """A status driver for one turn, wired to the room if this session serves one.

        A session with no room still gets a driver rather than a `None` to branch on: the SPA
        reads the message rows, so there is simply nothing for its status to do, and the turn
        loop should not have to know which surface it is on.
        """
        surface, room = self._room_surface, room_id
        if surface is None or room is None:
            return TurnStatus(ignore_status, ignore_clear)
        return TurnStatus(
            lambda text: surface.show_status(room, text),
            lambda: surface.clear_status(room),
            lambda active: surface.set_typing(room, active),
        )

    def _progress_reporter(self, session_id: UUID, room_id: str | None) -> Callable[[str], Awaitable[None]]:
        """Record every sandbox progress report, log it, and show it to the room if there is one.

        Recorded first because the rollout is the only durable copy: the pod's log is reaped with
        the sandbox, and a session that died before its first CLI frame has its whole account here.
        """

        async def report(detail: str) -> None:
            logger.info("Claude sandbox %s: %s", session_id, detail)
            await self._store.record_frame(
                session_id, FrameDirection.FROM_AGENT, SETUP_OUTPUT_KIND, setup_output_frame(detail)
            )
            if self._room_surface is not None and room_id is not None:
                await self._room_surface.report(room_id, detail)

        return report

    async def handle_runner(self, websocket: WebSocket, session_id: UUID, bearer: str) -> None:
        authentication = await self._store.authenticate_bridge(session_id, bearer)
        if authentication == BridgeAuthentication.HELD:
            # **A denial response, not a close.** A websocket closed before `accept()` reaches the
            # client as HTTP 403 whatever code is passed to it (uvicorn renders every one that
            # way), so a refusal and a "not yet" were indistinguishable — and the runner gives up
            # on a 4xx, correctly, because a bad credential is not worth redialling. The ASGI
            # `websocket.http.response` extension is what lets this answer 503 instead, which the
            # runner already waits out: `_worth_redialling` retries anything 5xx, since that is
            # also what the Gateway says mid-roll.
            logger.info("session %s is held by another replica; telling the runner to retry", session_id)
            await websocket.send_denial_response(
                Response(status_code=503, content=b"session is held by another replica")
            )
            return
        if authentication == BridgeAuthentication.TERMINAL:
            await self._cleanup_terminal_claim(session_id)
            await websocket.close(code=NOT_ADMITTED_CODE, reason="runner session is already terminal")
            return
        if authentication == BridgeAuthentication.REJECTED:
            await websocket.close(code=NOT_ADMITTED_CODE, reason="invalid or consumed runner credential")
            return
        # Whatever the previous holder was in the middle of *is* ours to finish: the sandbox
        # outlived it, so the rest of that exchange is about to arrive on this socket. What is
        # left to decide is which of three things its open turn was — `adopt_open_turn`.
        resumed = await self._store.adopt_open_turn(session_id)
        if resumed is not None:
            logger.warning("session %s adopted with turn %s still running", session_id, resumed.turn_id)
        # Rendered before the socket is accepted, alongside the other admission failures, so a
        # broken prompt ends the session where the supervisor can see it (and say so in the
        # room) instead of raising past the cleanup below and leaving the claim stranded.
        # Failing is deliberate: a session that silently started without its identity is the
        # generic-assistant bug this prompt exists to fix, and it would be invisible.
        try:
            room_id = await self._room_of(session_id)
            appended = await self._appended_prompt(session_id, room_id)
        except Exception as error:
            logger.exception("Claude system prompt failed to render for session %s", session_id)
            await self._store.fail(session_id, f"system prompt failed to render: {error}")
            await self._cleanup_terminal_claim(session_id)
            await websocket.close(code=1011, reason="system prompt failed to render")
            return
        await websocket.accept()
        session = ClaudeSession(
            append_system_prompt=appended,
            cwd=Path(self._config.cwd),
            environment=self._config.claude_environment(),
            mcp_servers={
                "haku-console": HttpMcpServer(
                    url=self._config.mcp_url, headers={"Authorization": f"Bearer {self._mcp_token.get_secret_value()}"}
                )
            },
        )
        client = cli_over_websocket(
            StarletteTextWebSocket(websocket),
            build_claude_launch(session),
            self._progress_reporter(session_id, room_id),
            RolloutRecorder(self._store, session_id),
        )
        abort_event = asyncio.Event()
        # Whether the sandbox should outlive this connection. False for an ending session — one
        # closed, or failed in a way the CLI cannot be asked to continue past — and true when it
        # is only this replica that is going away.
        keep_sandbox = False
        # Two nested handlers because Python forbids `except` and `except*` on one `try`, and
        # the two are about different things: the inner one unwraps whatever the task group
        # failed with, the outer one is this whole activity being cancelled.
        try:
            try:
                # `TaskGroup` rather than bare `create_task`: both helpers run for exactly this
                # block's lifetime, and it owns awaiting and cancelling them.
                async with asyncio.TaskGroup() as helpers:
                    # The operator's abort lands on whichever replica the Service picks, which
                    # is rarely the one holding this websocket — so the event is driven by
                    # NOTIFY, not by a caller reaching into this process.
                    abort_watch = helpers.create_task(self._watch_aborts(session_id, abort_event))
                    # Says "this replica is still here" for as long as it is. Its absence is
                    # what another replica reclaims the session by; see `expire_stale_leases`.
                    renewal = helpers.create_task(self._renew_lease(session_id))
                    # Turns the runner's socket dropping into a `WebSocketDisconnect` here, even
                    # when this handler is parked in the idle prompt-wait with nothing reading the
                    # socket. Without it a roll leaves an idle session waiting out the whole
                    # graceful-shutdown timeout before it hands back.
                    connection = helpers.create_task(self._watch_connection(client))
                    try:
                        await client.connect()
                        # One stream for the session, not one per turn: a folded prompt is
                        # answered with no second `result`, and an adopted turn was issued by a
                        # process that is gone. So a turn is a bracket over this stream rather
                        # than a request/response pair
                        # (<../../plans/cli_protocol_ownership.md>).
                        frames = client.frames().__aiter__()
                        while True:
                            status = await self._store.status(session_id)
                            if status is None or status in ENDED_SESSION_STATUSES:
                                break
                            # The inherited turn before any new prompt, and once: its remaining
                            # frames are already on their way, so opening a second turn to take
                            # them would deliver one exchange's answer into another's bracket —
                            # which is what routing by session rather than by turn meant.
                            turn: TurnStart | ResumedTurn | None = resumed
                            resumed = None
                            if turn is None:
                                turn = await self._store.next_prompt(session_id)
                            if turn is None:
                                # Wait for a LISTEN/NOTIFY instead of polling.
                                await self._notifications.wait(
                                    SessionEventKind.PROMPT, session_id, timeout_seconds=30.0
                                )
                                continue
                            # Cleared before the turn, not after: an abort notified just as the
                            # previous one ended would otherwise sit set through the idle wait and
                            # kill this turn on arrival.
                            abort_event.clear()
                            try:
                                await self._run_turn(
                                    client, frames, session_id, turn, room_id=room_id, abort_event=abort_event
                                )
                            except Exception as error:
                                logger.exception("turn failed for session %s", session_id)
                                await self._store.fail(session_id, str(error))
                                break
                    finally:
                        # The helpers outlive the loop by construction, so ending it is what
                        # ends them; the group then awaits them before leaving this block.
                        abort_watch.cancel()
                        renewal.cancel()
                        connection.cancel()
            except* WebSocketDisconnect:
                # The runner went away, which is not the session being over: it keeps the CLI
                # alive across a lost socket and redials. Hand the session back and let the lease
                # decide — a runner that never returns leaves the row to the sweep.
                logger.info("session %s lost its runner; leaving it for adoption", session_id)
                keep_sandbox = True
                await self._store.release_lease(session_id)
            except* Exception as errors:
                # `fail` records the message; the traceback is what says which call produced
                # it, and the listener mismatch was three frames below anything it named.
                logger.exception("Claude runtime failed for session %s", session_id)
                await self._store.fail(session_id, f"Claude runtime failed: {_first_message(errors)}")
        except asyncio.CancelledError:
            # `CancelledError` is a `BaseException`, so neither clause above sees it. This is
            # this replica going away — a rolling update, an evicted pod — which says nothing
            # about the session, so it must not be recorded as a failure: a terminal row refuses
            # the runner's reconnect and the supervisor builds a replacement.
            #
            # Hand it back instead. The sandbox outlives this process, the runner redials, and
            # whichever replica answers adopts it. Nothing is swallowed: the sweep fails the
            # session once its adoption window passes with no runner back.
            keep_sandbox = True
            await self._store.release_lease(session_id)
            raise
        finally:
            # Shielded because everything here is an `await` and this task may already be
            # cancelled, in which case the first one would re-raise and the rest would silently
            # not happen — which is how `closed()` came to be skipped. Best effort even so: a
            # SIGKILL runs no finalizer at all, which is why the lease, not this block, is what
            # actually guarantees the session stops looking alive.
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.shield(
                    asyncio.wait_for(self._finalize(session_id, websocket, client, keep_sandbox), timeout=10)
                )

    async def _finalize(self, session_id: UUID, websocket: WebSocket, client: ClaudeCli, keep_sandbox: bool) -> None:
        """Let go of one runner connection, and of the session itself unless it outlives us.

        `keep_sandbox` is the difference between "this conversation is over" and "this replica
        is". Deleting the claim on the second is what made a roll destroy the sandbox it was
        supposed to leave running.
        """
        if keep_sandbox:
            # Said with a code rather than by dropping the socket, so the runner reconnects
            # because it was told to rather than because it guessed.
            with contextlib.suppress(Exception):
                await websocket.close(code=GOING_AWAY_CODE, reason="console replica going away")
            await client.aclose()
            return
        await client.aclose()
        await self._cleanup_terminal_claim(session_id)
        await self._store.closed(session_id)

    async def _renew_lease(self, session_id: UUID) -> None:
        """Hold *session_id*'s lease for as long as this replica runs it, and keep its sandbox with it.

        The same heartbeat slides the SandboxClaim's `shutdownTime` forward, so the sandbox is a
        renewed lease rather than a `session_ttl_seconds` hard timer that killed a conversation in
        full flow (`sandbox_claims.renew`). Console lease and sandbox deadline lapse together the
        moment a replica stops tending the session.
        """
        while True:
            await self._store.renew_lease(session_id)
            await self._claims.renew(
                session_id=session_id,
                expires_at=datetime.now(UTC) + timedelta(seconds=self._config.session_ttl_seconds),
            )
            await asyncio.sleep(LEASE_RENEW_INTERVAL.total_seconds())

    async def _watch_connection(self, client: ClaudeCli) -> None:
        """Raise `WebSocketDisconnect` the moment the runner's stream ends.

        The reader is a detached task, so a dropped socket cannot propagate into the task group on
        its own — it becomes a `None` sentinel that only a *turn* consumer sees. An idle session is
        not consuming, so without this it sits in the prompt-wait until the graceful-shutdown timer
        cancels it. Waking here routes the drop to the `except* WebSocketDisconnect` clause that
        hands the session back, so a roll is handed back at once rather than after that timeout.
        """
        await client.wait_closed()
        raise WebSocketDisconnect(code=GOING_AWAY_CODE)

    async def _watch_aborts(self, session_id: UUID, abort_event: asyncio.Event) -> None:
        """Set *abort_event* every time this session is told to abort, until cancelled.

        The operator's abort lands on whichever replica the Service picks, which is rarely
        the one holding this session's websocket, so it arrives over NOTIFY rather than by a
        caller reaching into this process.
        """
        async with self._notifications.subscribe(SessionEventKind.ABORT, session_id) as notified:
            while True:
                await notified.wait()
                notified.clear()
                abort_event.set()

    async def _run_turn(
        self,
        client: TurnClient,
        frames: AsyncIterator[ReceivedFrame],
        session_id: UUID,
        turn: TurnStart | ResumedTurn,
        *,
        room_id: str | None,
        abort_event: asyncio.Event,
    ) -> None:
        """Ask *turn*'s question if it has not been asked, then consume the stream until its `result`.

        *frames* belongs to the session, not to this call — see `handle_runner`. This call is the
        turn's span, so it closes it on every exit and is the only thing that does. A turn left
        open is therefore not a bookkeeping leak — it means no code got to close it, which is
        what a replica losing its pod mid-exchange looks like from outside, and what the
        `ResumedTurn` variant exists to pick back up.

        **The state below is the row's, held here only between two writes of it.** Every branch
        that changes one of these writes it back before the next frame is taken, so a process
        dying anywhere in this loop leaves `session_turns` saying what had happened — which is
        what makes adoption a read (<../../plans/chat_runtime_projection.md> § stage 3).
        """
        turn_id = turn.turn_id
        if isinstance(turn, TurnStart):
            # A resumed turn's question was asked by a process that is gone; only its answer is
            # still coming.
            prompt = await client.query(turn.prompt)
            # The prompt's row was written when the operator typed it, before any frame existed to
            # point at; this is where the question acquires the frame it went out as.
            await self._store.set_message_source_frames(session_id, turn.message_id, prompt.frame_seq)
        state = await self._store.turn_state(turn_id)
        assistant_id = state.assistant_message_id
        streamed = state.streamed
        # Whether this turn has already queued the room a reply, so the turn's final text is not
        # posted a second time: `result.result` normally repeats the last assistant message. It is
        # the outbox row's existence — recorded on the turn by the transaction that inserts the
        # row — and neither a report from the delivery layer, which used to be a statement about a
        # `deque.append`, nor `sent_at`, which is the drain's business and comes later.
        spoke = state.queued_reply
        # Its own fact rather than `spoke` again: a session with no room queues nothing, so a turn
        # that has completed a message there has `said_anything` and no `queued_reply` — and
        # minting a second message for `result.result` is exactly what conflating them did.
        saw_assistant_message = state.said_anything
        result: dict[str, Any] | None = None
        result_frame_seq: int | None = None
        status = self._turn_status(room_id)
        status.start()
        aborted = asyncio.ensure_future(abort_event.wait())
        # Set once the abort has been seen and the CLI interrupted, from which point this loop
        # stops racing the abort event and drains what is left of the turn to its `result`.
        interrupted = False
        try:
            while True:
                # Exactly one `anext` in flight, and the drain consumes the one it finds rather
                # than starting another: an async generator refuses to be advanced twice at once,
                # and an abort always arrives while this call is parked here.
                next_frame = asyncio.ensure_future(anext(frames))
                if not interrupted:
                    await asyncio.wait([next_frame, aborted], return_when=asyncio.FIRST_COMPLETED)
                    if interrupted := abort_event.is_set():
                        with contextlib.suppress(Exception):
                            await client.interrupt()
                # **The drain is this loop, not a second one beside it.** The CLI finishes the
                # message it is mid-way through, so an `assistant` frame between the interrupt and
                # the `result` is the normal case — and a drain that looked only for the `result`
                # threw it away: no row, no outbox row, no delivery, the text surviving only in
                # `session_frames` where nobody looks (<../debug/message_drops.md> E3). A message
                # the agent finished before it stopped is a message, so it is folded in by the one
                # piece of code that knows what folding one in means.
                #
                # It therefore counts towards `spoke` and `saw_assistant_message` exactly as it
                # would have a moment earlier, which is what keeps the tail below honest: the room
                # is not owed `result.result` as well (it repeats that message), and no second row
                # is minted for it — leaving `ABORTED_NOTICE` to be said on its own, as the one
                # `turn_id`-keyed row this turn writes.
                #
                # The stream stays open for the next turn: it is the session's, so an interrupt
                # ends a turn rather than the conversation.
                received = await next_frame
                frame, frame_seq = received.payload, received.frame_seq
                status.note(frame)
                match frame.get("type"):
                    case "stream_event":
                        if not (delta := text_delta(frame.get("event", {}))):
                            continue
                        if assistant_id is None:
                            assistant_id = await self._store.begin_assistant(
                                session_id, turn_id, source_first_frame_seq=frame_seq
                            )
                        streamed += delta
                        await self._store.update_assistant(
                            session_id, assistant_id, streamed, source_last_frame_seq=frame_seq
                        )
                        # The rollout keeps no deltas, so without this the text an interrupted
                        # turn produced would exist only in the message row and the log would
                        # simply stop mid-answer (R5.5b).
                        await self._store.update_partial_frame(session_id, streamed)
                    case "assistant":
                        saw_assistant_message = True
                        if assistant_id is None:
                            assistant_id = await self._store.begin_assistant(
                                session_id, turn_id, source_first_frame_seq=frame_seq
                            )
                        blocks = content_blocks(frame)
                        text = "".join(
                            str(block.get("text", "")) for block in blocks if block.get("type") == "text"
                        ).strip()
                        tool_uses = [
                            {"tool_use_id": block["id"], "name": block["name"], "input": block["input"]}
                            for block in blocks
                            if block.get("type") == "tool_use"
                        ]
                        said = text or streamed.strip()
                        # Each message is queued for the room as it finishes rather than only the
                        # final answer (R11.1), so a turn that narrates, works and reports back is
                        # three messages in the room and not just its conclusion — and the row is
                        # written here, with the message, rather than after the turn has decided
                        # it is over.
                        spoke = (
                            await self._store.update_assistant(
                                session_id,
                                assistant_id,
                                said,
                                tool_uses=tool_uses,
                                # The wire's own id for this message, which is what lets a reader
                                # find its calls in the frame log rather than match by position.
                                agent_message_id=agent_message_id(frame),
                                source_last_frame_seq=frame_seq,
                                complete=True,
                            )
                            or spoke
                        )
                        # The real frame is already in the log — the recorder wrote it when
                        # the socket delivered it — so the stand-in has nothing to stand for.
                        await self._store.clear_partial_frame(session_id)
                        assistant_id = None
                        streamed = ""
                    case "result":
                        result = frame
                        result_frame_seq = frame_seq
                        break
            if result is None:
                raise RuntimeError("the Claude stream ended without a result for this turn")
            if result.get("is_error") and not abort_event.is_set():
                raise RuntimeError(
                    f"Claude returned {result.get('subtype')}: {result.get('stop_reason') or 'unknown error'}"
                )
            final_text = streamed.strip() or str(result.get("result") or "").strip()
            if abort_event.is_set():
                final_text += f"\n\n{ABORTED_NOTICE}"
            if assistant_id is not None:
                # A stream no completed frame closed. Its `partial` frame stays exactly as the
                # last delta left it: the rollout should show a turn that stopped mid-answer as
                # having stopped mid-answer. `final_text` is not written over it, because the
                # harness adds `[aborted by operator]` to that and the frame records what the
                # agent produced, not what the room was told.
                # No frame range is passed: the deltas that produced this text already recorded
                # theirs, and the `result` frame closing the turn is not where the words came from.
                carried_final = await self._store.update_assistant(
                    session_id, assistant_id, final_text, tool_uses=[], complete=True
                )
                assistant_id = None
            elif not saw_assistant_message:
                # This row's only source is the `result` frame — the turn said nothing else.
                assistant_id = await self._store.begin_assistant(
                    session_id, turn_id, source_first_frame_seq=result_frame_seq
                )
                carried_final = await self._store.update_assistant(
                    session_id,
                    assistant_id,
                    final_text,
                    tool_uses=[],
                    source_last_frame_seq=result_frame_seq,
                    complete=True,
                )
                assistant_id = None
            else:
                # Every completed message queued its own row and one of them closed the answer, so
                # `final_text` — which is `result.result` repeating the last of them — belongs to
                # no row of its own.
                carried_final = False
            spoke = carried_final or spoke
            # Only what the room is not already owed. Each assistant message queued its own row as
            # it finished, and `result.result` normally repeats the last of them — so queueing
            # `final_text` unconditionally would post the answer twice. Two cases still need it: a
            # turn whose text belongs to no completed message at all, and an abort, whose notice
            # rides on `final_text` and therefore on no message row — which is exactly when a
            # message row did *not* just carry `final_text` into the outbox.
            #
            # **Before the turn is closed, not after.** Closing it is what makes it unadoptable, so
            # a replica dying between the two would strand this reply with nothing left to
            # re-derive it. This way the window leaves the turn open, and what the replacement
            # re-derives collides with the row already there (`session_outbox.turn_id`).
            if not spoke:
                await self._speak(session_id, room_id, turn_id, final_text)
            elif abort_event.is_set() and not carried_final:
                await self._speak(session_id, room_id, turn_id, ABORTED_NOTICE)
            # Closed with what the `result` frame reported, which is the only place a turn's
            # cost, usage and duration exist.
            await self._store.end_turn(
                turn_id, TurnOutcome.ABORTED if abort_event.is_set() else TurnOutcome.ANSWERED, result
            )
        except Exception as error:
            await self._store.end_turn(turn_id, TurnOutcome.FAILED)
            if assistant_id is not None:
                await self._store.fail(session_id, str(error), assistant_id)
            raise
        finally:
            # The event outlives the turn (it is the session's), so only this turn's waiter goes.
            aborted.cancel()
            # Every terminal path, failure included: a line still saying "running Bash" after
            # the turn died is the stuck-typing-indicator bug R6.1 calls out, in another form.
            await status.finish()

    async def _speak(self, session_id: UUID, room_id: str | None, turn_id: UUID, text: str) -> None:
        """Queue the turn's last word for the room, or report that it had none (R11.2).

        Only ever the end of a turn: everything a completed assistant message says is queued with
        the message itself, in one transaction. What is left over is text that belongs to no
        message row — an abort notice, or an answer that arrived only on the `result` frame.

        A session with no room needs nothing here; the SPA's client reads the message rows the
        turn already wrote. An empty body is not a silence token (R11.2): the room is told that
        the turn finished without saying anything, as a notice rather than as a reply, because it
        is the console reporting an outcome and not the agent talking.
        """
        if self._room_surface is None or room_id is None:
            return
        if not await self._store.enqueue_turn_reply(session_id, turn_id, text):
            await self._room_surface.report_silent_turn(room_id)

    async def aclose(self) -> None:
        # Called from the lifespan on the way down. Handing every held lease back here is the
        # guarantee the per-connection releases cannot be: a cancelled `handle_runner` may not
        # finish its own commit, but this one statement does, so a graceful roll leaves no session
        # waiting out the sweep. Reachable only because `uvicorn.run` bounds `timeout_graceful_
        # shutdown` (see app.main) — otherwise shutdown never gets here.
        released = await self._store.release_held_leases()
        if released:
            logger.info("Released %d held session lease(s) on shutdown", released)
        await self._claims.aclose()


async def _queued_prompt(db: AsyncSession, session_id: UUID, *, lock: bool = False) -> SessionPrompt | None:
    """The prompt *session_id* is waiting to run, if it has one.

    `SKIP LOCKED` when claiming, so two replicas racing on one session take different rows rather
    than blocking on each other — though a partial unique index means there is at most one to take.
    """
    query = (
        select(SessionPrompt)
        .where(SessionPrompt.session_id == session_id, SessionPrompt.claimed_at.is_(None))
        .order_by(SessionPrompt.queued_at)
    )
    prompt: SessionPrompt | None = await db.scalar(query.with_for_update(skip_locked=True) if lock else query)
    return prompt


async def _legacy_pending(db: AsyncSession, session_id: UUID, *, lock: bool = False) -> SessionMessage | None:
    """A prompt accepted by a replica on the previous image, which wrote no queue row.

    CLEANUP(added 2026-08-13): Remove once every pod runs an image with `session_prompts`
    (0033) — one roll after it ships. Until then this is the only way such a prompt is answered.
    """
    query = (
        select(SessionMessage)
        .where(
            SessionMessage.session_id == session_id,
            SessionMessage.role == ChatMessageRole.USER,
            SessionMessage.status == ChatMessageStatus.PENDING,
        )
        .order_by(SessionMessage.created_at)
    )
    message: SessionMessage | None = await db.scalar(query.with_for_update(skip_locked=True) if lock else query)
    return message


async def _enqueue_reply(
    db: AsyncSession,
    chat: Session,
    body: str,
    *,
    message_id: UUID | None,
    agent_message_id: str | None,
    turn_id: UUID | None,
    now: datetime,
) -> bool:
    """Put *body* in the room's outbox, inside the caller's transaction.

    False means there is nothing for the room to be owed, which covers two ordinary states and no
    failures: a session serving no room — the SPA reads the message rows directly — and a message
    whose text is empty.

    **Every row carries exactly one identity, and the insert is idempotent on it**, because both
    ways a reply can be produced twice are ways a *replacement* replica produces it. A completed
    `assistant` frame is identified by its transcript row and can be replayed out of the runner's
    rollout; a turn's last word is identified by the turn and is re-derived by whoever adopts a
    turn left open. Postgres infers one index per statement, so the conflict target follows
    whichever identity this row has.
    """
    if chat.room_id is None or not (queued := body.strip()):
        return False
    inserted = pg_insert(SessionOutbox).values(
        outbox_id=uuid4(),
        session_id=chat.session_id,
        room_id=chat.room_id,
        body=queued,
        message_id=message_id,
        agent_message_id=agent_message_id,
        turn_id=turn_id,
        created_at=now,
        attempts=0,
        next_attempt_at=now,
    )
    await db.execute(
        inserted.on_conflict_do_nothing(index_elements=["message_id"], index_where=SessionOutbox.message_id.isnot(None))
        if message_id is not None
        else inserted.on_conflict_do_nothing(index_elements=["turn_id"], index_where=SessionOutbox.turn_id.isnot(None))
    )
    return True


async def _open_turn(db: AsyncSession, session_id: UUID) -> UUID | None:
    """The turn *session_id* is in the middle of, if it is in the middle of one.

    The one question behind three: whether a prompt may be accepted, whether there is anything to
    abort, and what the SPA should be told. A partial unique index makes "at most one" a schema
    property, so this is a lookup rather than a scan with a rule attached.
    """
    turn_id: UUID | None = await db.scalar(
        select(SessionTurn.turn_id).where(SessionTurn.session_id == session_id, SessionTurn.ended_at.is_(None))
    )
    return turn_id


async def _prompt_left(db: AsyncSession, session_id: UUID, first_frame_seq: int) -> bool:
    """Whether the turn starting at *first_frame_seq* ever wrote its prompt to the agent.

    **The console's own write is the evidence, not the CLI's acknowledgement.** `sent()` records
    the frame after `channel.write` returns, so its absence means the bytes did not go out; its
    presence means they did, and from then on the CLI's `command_lifecycle` — the only thing that
    would say whether the *CLI* has the prompt — may still be sitting in the runner's replay
    window, unrecorded, because replay does not begin until the socket is accepted and this runs
    before that. Asking a question the record cannot yet answer would re-ask a prompt the agent
    already has, which is the worse of the two failures: a duplicate turn instead of a lost one.

    So the ambiguous middle — written to a socket that then died — is deliberately treated as
    delivered, and what this closes is the window where nothing was written at all.
    """
    written = await db.scalar(
        select(SessionFrame.frame_seq)
        .where(
            SessionFrame.session_id == session_id,
            SessionFrame.frame_seq >= first_frame_seq,
            SessionFrame.direction == FrameDirection.TO_AGENT,
            SessionFrame.kind == PROMPT_FRAME_KIND,
        )
        .limit(1)
    )
    return written is not None


async def _recorded_result(db: AsyncSession, session_id: UUID, first_frame_seq: int) -> dict[str, Any] | None:
    """This turn's `result` frame, if its holder recorded one and then died before closing it.

    Its presence means the exchange is over and nothing more is coming: the runner will replay
    the frame, and `record_frame` will refuse it as one this session already has, so a resumed
    turn would wait for an end that cannot arrive twice.
    """
    payload: dict[str, Any] | None = await db.scalar(
        select(SessionFrame.payload)
        .where(
            SessionFrame.session_id == session_id,
            SessionFrame.frame_seq >= first_frame_seq,
            SessionFrame.direction == FrameDirection.FROM_AGENT,
            SessionFrame.kind == RESULT_FRAME_KIND,
        )
        .limit(1)
    )
    return payload


async def _requeue(db: AsyncSession, turn_id: UUID) -> None:
    """Put the prompts *turn_id* claimed back where `next_prompt` will find them again.

    Three writes because the claim is recorded in three places, and a prompt left in any of them
    is one the queue no longer offers: the queue row's `claimed_at`, the transcript row's status,
    and the link saying this turn answered it — which has to go, or the turn that finally does
    answer cannot record that it did (`(turn_id, message_id)` is the primary key, and the message
    half of it would repeat).
    """
    message_ids = list(
        (await db.scalars(select(SessionTurnPrompt.message_id).where(SessionTurnPrompt.turn_id == turn_id))).all()
    )
    if not message_ids:
        return
    now = datetime.now(UTC)
    for message in await db.scalars(select(SessionMessage).where(SessionMessage.message_id.in_(message_ids))):
        message.status = ChatMessageStatus.PENDING
        message.updated_at = now
    for prompt in await db.scalars(select(SessionPrompt).where(SessionPrompt.message_id.in_(message_ids))):
        prompt.claimed_at = None
    await db.execute(delete(SessionTurnPrompt).where(SessionTurnPrompt.turn_id == turn_id))
    logger.warning("turn %s never asked its prompt; re-queued %d", turn_id, len(message_ids))


def _service(request: Request) -> SessionService:
    service = cast(SessionService | None, request.app.state.session_service)
    if service is None:
        raise HTTPException(status_code=503, detail="the session runtime is not configured")
    return service


def _store(request: Request) -> SessionStore:
    store = cast(SessionStore | None, request.app.state.session_store)
    if store is None:
        raise HTTPException(status_code=503, detail="the session runtime is not configured")
    return store


def _notifications(request: Request) -> SessionNotifications:
    notifications = cast(SessionNotifications | None, request.app.state.session_notifications)
    if notifications is None:
        raise HTTPException(status_code=503, detail="the session runtime is not configured")
    return notifications


SessionNotificationsDep = Annotated[SessionNotifications, Depends(_notifications)]
SessionServiceDep = Annotated[SessionService, Depends(_service)]
SessionStoreDep = Annotated[SessionStore, Depends(_store)]


@router.get("/api/conversations")
async def list_conversations(
    actor: OperatorActorDep, store: SessionStoreDep, limit: Annotated[int, Query(ge=1, le=100)] = 50
) -> list[ConversationSessionSummary]:
    return await store.list_operator_conversations(actor.operator_id, limit=limit)


@router.get("/api/conversations/{session_id}")
async def get_conversation(
    session_id: UUID, actor: OperatorActorDep, store: SessionStoreDep
) -> ConversationSessionView:
    try:
        return await store.get_operator_conversation(actor.operator_id, session_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Conversation not found") from error


@router.get("/api/conversations/{session_id}/frames")
async def read_conversation_frames(
    session_id: UUID,
    actor: OperatorActorDep,
    store: SessionStoreDep,
    before_seq: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_FRAME_PAGE)] = DEFAULT_FRAME_PAGE,
    kind: Annotated[list[str] | None, Query()] = None,
) -> SessionFramePage:
    """The raw protocol frames behind a conversation, newest page first.

    What `session_messages` is a lossy projection *of*: the transcript this console shows is an
    interpretation, and this is the record it was interpreted from. Omitting `before_seq` opens on
    the end of the log; the response's `next_before_seq` walks back from there.

    `kind` is repeatable and open, because the column is: the CLI may send a `type` this release
    has never heard of, and an inspector that could only name a closed list would be the surface
    hiding exactly the frame worth looking at. Omitting it means everything except `stream_event`
    — see `_frames_of_kinds`.
    """
    try:
        return await store.read_operator_frames(
            actor.operator_id, session_id, before_seq=before_seq, limit=limit, kinds=kind
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Conversation not found") from error


# The operator surface moved from `/api/claude/sessions` to `/api/sessions` with the tables it
# reads. Both are served, and only the new path is in the schema — so the generated client (and
# so the SPA) can only call the new one, while a browser tab that loaded the previous bundle keeps
# working until it reloads. That window is what `maxUnavailable: 0` makes unavoidable: old pods,
# and old tabs, outlive the release.
#
# CLEANUP(added 2026-08-15): drop the `include_in_schema=False` registrations one release after
# this ships, when no deployed bundle names them. `/internal/claude/runner` is *not* part of this
# rename — the runner image dials it and that is a two-sided roll of its own.
@router.post("/api/sessions")
@router.post("/api/claude/sessions", include_in_schema=False)
async def create_session(actor: OperatorActorDep, service: SessionServiceDep) -> SessionView:
    try:
        return await service.create(actor.operator_id, SpaSession())
    except Exception as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@router.get("/api/sessions/{session_id}")
@router.get("/api/claude/sessions/{session_id}", include_in_schema=False)
async def get_session(session_id: UUID, actor: OperatorActorDep, service: SessionServiceDep) -> SessionView:
    try:
        return await service.get(actor.operator_id, session_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="session not found") from error


async def _sse_stream(
    store: SessionStore, notifications: SessionNotifications, operator_id: UUID, session_id: UUID
) -> collections.abc.AsyncIterator[str]:
    """Server-Sent Events stream delivering real-time session updates via LISTEN/NOTIFY."""
    yield f"data: {json.dumps({'type': 'connected'})}\n\n"
    try:
        last_view = await store.get(operator_id, session_id)
    except KeyError:
        yield f"data: {json.dumps({'type': 'end'})}\n\n"
        return
    last_status, last_payload = last_view.status, last_view.model_dump_json()
    yield f"data: {last_payload}\n\n"
    while True:
        if last_status in {SessionStatus.CLOSED, SessionStatus.FAILED}:
            yield f"data: {json.dumps({'type': 'end'})}\n\n"
            return
        await notifications.wait(SessionEventKind.UPDATE, session_id, timeout_seconds=30.0)
        try:
            next_view = await store.get(operator_id, session_id)
        except KeyError:
            yield f"data: {json.dumps({'type': 'end'})}\n\n"
            return
        # Serialized once and compared against what was last sent, rather than three times per
        # wake: the view embeds the whole transcript, so each of those was the entire
        # conversation. It suppresses little during a turn — every delta really does change the
        # view — which is the reason not to pay for the comparison twice more.
        if (payload := next_view.model_dump_json()) != last_payload:
            last_status, last_payload = next_view.status, payload
            yield f"data: {payload}\n\n"


@router.get("/api/sessions/{session_id}/stream")
@router.get("/api/claude/sessions/{session_id}/stream", include_in_schema=False)
async def stream_session(
    session_id: UUID, actor: OperatorActorDep, store: SessionStoreDep, notifications: SessionNotificationsDep
) -> StreamingResponse:
    if not await store.session_exists(actor.operator_id, session_id):
        raise HTTPException(status_code=404, detail="session not found")
    return StreamingResponse(
        _sse_stream(store, notifications, actor.operator_id, session_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/sessions/{session_id}/abort", status_code=202)
@router.post("/api/claude/sessions/{session_id}/abort", status_code=202, include_in_schema=False)
async def abort_session(session_id: UUID, actor: OperatorActorDep, service: SessionServiceDep) -> dict[str, str]:
    try:
        aborted = await service.request_abort(actor.operator_id, session_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="session not found") from error
    if not aborted:
        raise HTTPException(status_code=409, detail="no active turn to abort")
    return {"status": "aborted"}


@router.post("/api/sessions/{session_id}/messages")
@router.post("/api/claude/sessions/{session_id}/messages", include_in_schema=False)
async def send_message(
    session_id: UUID, body: SessionPromptRequest, actor: OperatorActorDep, store: SessionStoreDep
) -> SessionMessageView:
    try:
        return await store.enqueue_prompt(actor.operator_id, session_id, body.text)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="session not found") from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.delete("/api/sessions/{session_id}", status_code=204)
@router.delete("/api/claude/sessions/{session_id}", status_code=204, include_in_schema=False)
async def delete_session(session_id: UUID, actor: OperatorActorDep, service: SessionServiceDep) -> None:
    try:
        await service.dispose(actor.operator_id, session_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="session not found") from error


@internal_router.websocket("/internal/claude/runner/{session_id}")
async def runner_websocket(websocket: WebSocket, session_id: UUID) -> None:
    service = cast(SessionService | None, websocket.app.state.session_service)
    authorization = websocket.headers.get("authorization", "")
    scheme, _, bearer = authorization.partition(" ")
    if service is None or scheme.lower() != "bearer" or not bearer:
        await websocket.close(code=NOT_ADMITTED_CODE, reason="runner authentication required")
        return
    await service.handle_runner(websocket, session_id, bearer)
