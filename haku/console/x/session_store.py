"""Postgres store for operator sessions — the rows, and which of them commit together.

The service that drives a turn is next door in `session_runtime.py`; the line between the two is
the transaction. A method whose job is "these writes commit together or not at all" is here, and
several of this file's invariants are exactly that grouping — `update_assistant` writing the
message, the room's outbox row and the turn's `queued_reply` in one transaction is what stops a
turn losing its answer (<../debug/message_drops.md>).

Neutral runtime: no channel and no harness, so a second channel inherits every row in it.

The incidents behind this file's invariants are in <../debug/2026_08_16_runtime_archaeology.md>.
"""

from __future__ import annotations

import decimal
import hashlib
import logging
import os
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, ClassVar, cast
from uuid import UUID, uuid4

from sqlalchemy import CursorResult, Select, delete, func, literal, or_, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.cli_protocol.frame_identity import frame_uid
from haku.console.chat_models import (
    ENDED_SESSION_STATUSES,
    LEASED_SESSION_STATUSES,
    OPEN_SESSION_STATUSES,
    ChatMessageRole,
    ChatMessageStatus,
    ChatSurface,
    FrameDirection,
    LeaseExpiryReason,
    PromptFate,
    RecordedToolCall,
    SessionStatus,
    TurnOutcome,
)
from haku.console.database_schema import (
    Session,
    SessionFrame,
    SessionMessage,
    SessionOutbox,
    SessionPrompt,
    SessionTurn,
    SessionTurnPrompt,
)
from haku.console.x import session_events, transcript_entries
from haku.console.x.claude_code import projection
from haku.console.x.claude_code.frames import DELTA_FRAME_KIND, PROMPT_FRAME_KIND
from haku.console.x.conversation_events import ConversationEvent, MessageCompleted, TextDelta, ToolCallStarted, Usage
from haku.console.x.conversation_records import (
    Conversation,
    ConversationCursor,
    FrameCursor,
    RolloutFrame,
    TranscriptCursor,
    TranscriptSlice,
    TurnCursor,
    TurnRecord,
    TurnUsage,
)
from haku.console.x.session_notifications import SessionEventKind, notify
from haku.console.x.session_views import (
    ConversationSessionSummary,
    ConversationSessionView,
    ConversationTurnView,
    SessionFramePage,
    SessionMessageView,
    SessionView,
    frame_page,
    session_view,
    setup_narration,
    tool_calls,
    user_message_view,
)
from haku.console.x.setup_output import SETUP_OUTPUT_KIND
from haku.runtime.x.bridge.cli_client import ReceivedFrame, RecordedFrame

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
    """What admission has to say to a redialling runner.

    **"Not yours" and "not yet" are different.** The runner redials about a second after its
    socket drops, so it routinely arrives at a new replica while the dying one's lease is still
    valid, and a refusal it cannot retry costs the sandbox — which is why
    `session_runtime.handle_runner` answers `HELD` with a 5xx handshake response.
    """

    ACCEPTED = "accepted"
    # The session is already over, so the runner should stop rather than retry.
    TERMINAL = "terminal"
    # The credential is wrong. Permanent.
    REJECTED = "rejected"
    # Another replica is still serving this session and saying so. **Transient**: it lasts at most
    # until that lease expires, and the runner that waits it out is the one adopting the session.
    HELD = "held"


@dataclass(frozen=True)
class SpaSession:
    """A session created by the browser chat view, which has no room."""

    # What the row records for this variant, carried on the variant rather than derived from it by
    # an `isinstance` chain at the call site: a third surface is a dataclass, not another arm.
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

    What the departed holder got through is on the turn's own row (`TurnState`), so adoption reads
    it rather than rebuilding it out of the frame log: the live path and the recovery path are one
    account of one exchange.

    `replay` is the rest of that account — the frames recorded past the session's projection cursor,
    whose effects therefore did not commit. Feeding them to the turn loop ahead of the live stream
    is what makes adoption the same call as steady state with a cursor that happens to be behind
    (<../../plans/chat_runtime_projection.md> § The shape). Empty for a session with no cursor,
    where adoption falls back to reading the frames itself.
    """

    turn_id: UUID
    replay: tuple[ReceivedFrame, ...]


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
            return session_view(record, messages, responding=responding, calls=await tool_calls(db, session_id))

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
        turns = await self.list_turns(session_id, cursor=None, limit=100)
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

        **A lease changing hands is recorded**, in this transaction, as the session event it is: it
        happens on every roll, it is what three hypotheses in the 2026-08-15 drop investigation
        turned on, and it crosses no wire — so nothing in the frame log can say it happened.
        """
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            record = await db.get(Session, session_id, with_for_update=True)
            if record is None or not secrets.compare_digest(record.bridge_token_fingerprint, self._fingerprint(token)):
                return BridgeAuthentication.REJECTED
            if record.status in ENDED_SESSION_STATUSES:
                return BridgeAuthentication.TERMINAL
            first_attach = record.status == SessionStatus.PROVISIONING and record.bridge_connected_at is None
            if first_attach:
                record.bridge_connected_at = now
                record.status = SessionStatus.READY
            elif record.lease_holder not in (None, REPLICA) and record.lease_expires_at > now:
                # Somebody else is still serving this session and saying so. Turning this runner
                # away is what keeps one CLI answering to one console — but only until that lease
                # lapses, which is why it is `HELD` rather than `REJECTED`.
                return BridgeAuthentication.HELD
            previous_holder = record.lease_holder
            record.lease_holder = REPLICA
            record.lease_expires_at = now + LEASE_TTL
            record.updated_at = now
            # The first attach is the session being served rather than taken over, and a runner
            # redialling the replica that already holds it is neither.
            if not first_attach and previous_holder != REPLICA:
                db.add(
                    session_events.authored(
                        session_events.SessionAdoptedBody(previous_holder=previous_holder, holder=REPLICA),
                        session_id=session_id,
                        now=now,
                    )
                )
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
            if chat is not None and chat.status in LEASED_SESSION_STATUSES:
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
                    .where(Session.status.in_(LEASED_SESSION_STATUSES), Session.lease_holder == REPLICA)
                    .values(lease_holder=None, lease_expires_at=datetime.now(UTC), updated_at=datetime.now(UTC))
                ),
            )
            return result.rowcount

    async def adopt_open_turn(self, session_id: UUID) -> ResumedTurn | None:
        """Say what the previous holder's open turn was, and hand back the one worth finishing.

        The sandbox outlives the replica, so an adopting console inherits the exchange too. Two
        questions remain here, and they are different in kind:

        1. **Was the prompt ever asked?** No prompt frame means the previous holder claimed the
           prompt and died before writing it, so it goes back on the queue. A projection cursor
           cannot answer this — the console's own outbound write is the evidence, and the fold
           projects an outbound prompt to nothing on purpose (`claude_code.projection._user`).
        2. **Everything else is the fold's.** Whether the exchange finished is not asked of the
           frames any more: the turn resumes with the frames past its cursor, and if one of them
           is the turn's ending then projecting it is what closes the turn — the same events, in
           the same loop, as if the frame had just arrived on the socket.

        How far the answer got is not reconstructed here either: it is on the turn's row, and
        `_run_turn` reads it the same way whether it opened the turn or inherited it.

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
            if not await _prompt_left(db, session_id, first_frame_seq):
                await _requeue(db, turn_id)
                await notify(db, SessionEventKind.PROMPT, session_id)
            else:
                cursor = await db.scalar(select(Session.projected_frame_seq).where(Session.session_id == session_id))
                # A cursor inside this turn is a position this turn's own writes put there.
                # One from before the turn opened is stale — a replica that projects without
                # advancing it left it behind — and re-projecting from a stale position would
                # redo effects that did commit, which is a duplicated message and a duplicated
                # room reply rather than a lost one. `next_prompt` anchors it at the frame before
                # the turn, so the normal case satisfies this by construction.
                if cursor is not None and cursor >= first_frame_seq - 1:
                    return ResumedTurn(turn_id=turn_id, replay=await _unprojected_frames(db, session_id, cursor))
        # Two ways to arrive here, one outcome. A turn that never asked its prompt has nothing to
        # finish, and a turn whose cursor sits before it has no position to resume from — so
        # neither has an outcome but failure.
        await self.end_turn(turn_id, TurnOutcome.FAILED)
        return None

    async def claim_cleanup_candidates(self) -> list[UUID]:
        """Terminal sessions whose sandbox claim has not been recorded as deleted."""
        async with self._sessions() as db:
            result = await db.scalars(
                select(Session.session_id).where(
                    Session.status.in_(ENDED_SESSION_STATUSES), Session.claim_cleaned_at.is_(None)
                )
            )
            return list(result.all())

    async def complete_claim_cleanup(self, session_id: UUID) -> None:
        """Record that this session's claim is gone, which is what takes it out of the sweep.

        The rendezvous fingerprint is deliberately left alone. It is a verifier for a bearer that
        was never stored, and a cleaned-up session cannot be admitted anyway — `authenticate_bridge`
        answers `TERMINAL` for any ended status — so blanking it bought nothing and cost the
        redialling runner a truthful answer.
        """
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            chat = await db.get(Session, session_id, with_for_update=True)
            if chat is None:
                return
            chat.claim_cleaned_at = now
            if chat.status == SessionStatus.CLOSING:
                chat.status = SessionStatus.CLOSED
            chat.updated_at = now

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
            if await _queued_prompt(db, session_id) is not None:
                raise RuntimeError("a prompt is already queued")
            # Still minted here, and still `pending`: the transcript row is what the SPA gets back
            # from this call, and `pending` is how it renders a prompt that has not started.
            message = SessionMessage(
                message_id=uuid4(),
                session_id=session_id,
                role=ChatMessageRole.USER,
                status=ChatMessageStatus.PENDING,
                content=prompt_text,
                error=None,
                created_at=now,
                updated_at=now,
            )
            db.add(message)
            db.add(
                SessionPrompt(prompt_id=uuid4(), session_id=session_id, message_id=message.message_id, queued_at=now)
            )
            # In this transaction, so the ordered stream gains the operator's turn exactly when the
            # transcript does. Without it `session_events` holds only the agent's half.
            db.add(
                session_events.prompt_enqueued(
                    session_id=session_id, message_id=message.message_id, text=prompt_text, now=now
                )
            )
            # No status write: a queued prompt is not a turn in flight.
            chat.updated_at = now
            await notify(db, SessionEventKind.PROMPT, session_id)
            await notify(db, SessionEventKind.UPDATE, session_id)
        return user_message_view(message)

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
        return PromptFate.IN_FLIGHT if status in OPEN_SESSION_STATUSES else PromptFate.LOST

    async def next_prompt(self, session_id: UUID) -> TurnStart | None:
        """Take the queued prompt and open the turn that will answer it, or None if there is none.

        Dequeue and open are one transaction on purpose: they are the same event — the harness
        handing the agent a prompt — and splitting them would leave a window in which the prompt
        is claimed with no turn to name it, which is exactly what admission and abort now ask
        about.

        **Opening the turn anchors the projection cursor**, in that same transaction: everything
        recorded so far has been projected, because the previous turn's own frames were and the
        handshake frames between turns project to nothing. So the turn begins with a cursor it
        can be resumed from rather than with one inherited from whatever last wrote it.
        """
        async with self._sessions.begin() as db:
            chat = await db.get(Session, session_id, with_for_update=True)
            if chat is None or chat.status in ENDED_SESSION_STATUSES:
                return None
            now = datetime.now(UTC)
            if (queued := await _queued_prompt(db, session_id, lock=True)) is None:
                return None
            queued.claimed_at = now
            message = await db.get(SessionMessage, queued.message_id)
            if message is None:
                # The row the queue points at is gone, so there is no prompt to run and no text to
                # run it with. Claiming it anyway is what stops the session retrying a prompt it
                # can never read.
                logger.error("prompt %s has no message row", queued.prompt_id)
                return None
            message.status = ChatMessageStatus.COMPLETE
            message.updated_at = now
            chat.updated_at = now
            # The bracket's lower bound, taken before the prompt reaches the CLI so every frame
            # the exchange produces falls inside it.
            highest = await db.scalar(
                select(func.max(SessionFrame.frame_seq)).where(SessionFrame.session_id == session_id)
            )
            chat.projected_frame_seq = highest or 0
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

    async def end_turn(
        self,
        turn_id: UUID,
        outcome: TurnOutcome,
        usage: Usage | None = None,
        *,
        last_frame_seq: int | None = None,
        projected_frame_seq: int | None = None,
    ) -> None:
        """Close *turn_id* at the frame it ended on, with what the exchange cost.

        **The cost is the neutral one**: a backend's adapter has already read its own payload into
        `Usage`, so this method knows no CLI's field names and a second backend fills the same
        columns by producing the same event. The payload those numbers were read from stays in
        `session_frames`, which is the evidence they can be appealed to
        (<../../plans/chat_runtime_projection.md> § Does a turn live over frames or over neutral
        events).

        *last_frame_seq* is the turn's own last frame, and only the caller knows which that is: the
        CLI emits a `command_lifecycle` frame just after the `result` one, so a bound re-derived
        here from the head of the log lands on a frame the turn did not produce. A turn that ended
        on no frame at all — a failure, or an abort whose result never arrived — passes none, and
        the bound is then the last frame recorded since the turn opened: an overshoot of the same
        kind, but the most this transaction can honestly say, and one `uq_session_turns_open` keeps
        inside this turn because no second turn can have opened. NULL stays what it always meant, a
        turn that recorded nothing.

        *projected_frame_seq* is the frame that ended the turn, and this is the transaction that
        takes the cursor past it — the turn's last word is written before the close, so advancing
        in `apply_frame` when that frame was projected would move the cursor ahead of writes still
        to come. A turn ending any other way passes none and leaves the cursor where it is; the
        next `next_prompt` re-anchors it.

        Idempotent on an already-closed turn: a second close must not overwrite the first
        outcome, because the first one is the one that happened.
        """
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            turn = await db.get(SessionTurn, turn_id, with_for_update=True)
            if turn is None or turn.ended_at is not None:
                return
            turn.last_frame_seq = (
                last_frame_seq
                if last_frame_seq is not None
                else await db.scalar(
                    select(func.max(SessionFrame.frame_seq)).where(
                        SessionFrame.session_id == turn.session_id, SessionFrame.frame_seq >= turn.first_frame_seq
                    )
                )
            )
            turn.ended_at = now
            turn.outcome = outcome
            if usage is not None:
                turn.input_tokens = usage.input_tokens
                turn.output_tokens = usage.output_tokens
                turn.cached_input_tokens = usage.cached_input_tokens
                # A float in the neutral shape, because that is what every backend puts on the
                # wire; through `Decimal(str(...))` rather than `Decimal(float)`, which would
                # carry the binary representation's noise into a column that is exact on purpose.
                turn.cost_usd = None if usage.cost_usd is None else decimal.Decimal(str(usage.cost_usd))
                turn.duration_ms = usage.duration_ms
            chat = await db.get(Session, turn.session_id)
            if chat is not None:
                # `responding` is derived from this turn being open, so closing it is what
                # retires the state — and what the SPA has to be told about. The column is only
                # written back when it still carries the old meaning, which a replica on the
                # previous image is what would have put there.
                if chat.status == SessionStatus.RESPONDING:
                    chat.status = SessionStatus.READY
                _advance_cursor(chat, projected_frame_seq)
                chat.updated_at = now
                await notify(db, SessionEventKind.UPDATE, turn.session_id)

    async def list_turns(self, session_id: UUID, *, cursor: TurnCursor | None, limit: int) -> list[TurnRecord]:
        """A session's exchanges from *cursor*, newest first, for the `haku_conversations` tools.

        Keyset on `(started_at, turn_id)`, and the tiebreak is what makes it one: two turns of one
        session can share a start instant, and a cursor naming only the timestamp would hand a
        tied pair out twice or step over one. Inclusive of the row the cursor names, which is the
        first row the previous page did not return.
        """
        query = select(SessionTurn).where(SessionTurn.session_id == session_id)
        if cursor is not None:
            query = query.where(
                tuple_(SessionTurn.started_at, SessionTurn.turn_id)
                <= tuple_(literal(cursor.started_at), literal(cursor.turn_id))
            )
        async with self._sessions() as db:
            rows = (
                await db.scalars(query.order_by(SessionTurn.started_at.desc(), SessionTurn.turn_id.desc()).limit(limit))
            ).all()
        return [
            TurnRecord(
                turn_id=row.turn_id,
                first_frame_seq=row.first_frame_seq,
                last_frame_seq=row.last_frame_seq,
                started_at=row.started_at,
                ended_at=row.ended_at,
                outcome=row.outcome,
                usage=_turn_usage(row),
            )
            for row in rows
        ]

    async def record_frame(
        self,
        session_id: UUID,
        direction: FrameDirection,
        kind: str,
        payload: dict[str, Any],
        *,
        runner_seq: int | None = None,
    ) -> RecordedFrame:
        """Append one frame to the session's rollout, unless this session already has it.

        `fresh` says whether the caller should act on the frame; `frame_seq` is the row's sequence
        either way, which is what a projection built from this frame points back at. **False means
        a replay** — the same agent-assigned identity already in this log — and the caller must
        then not act on it again. A frame with no identity is always recorded, since "no identity"
        is not "the same as the last one" (`frame_identity.frame_uid`).

        *kind* is passed rather than read out of the payload: a CLI frame keeps its discriminator
        in `type` and the bridge envelope keeps it in `kind`, and this table holds both.

        *runner_seq* is the runner's own number for the frame, where the frame came from a runner
        that numbers. It is written down and nothing here orders by it; what reads it is
        `highest_runner_seq`. Default None because most writers have no such number to give — this
        console's writes to the CLI, and the rows it authors itself.

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
                    runner_seq=runner_seq,
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

    async def highest_runner_seq(self, session_id: UUID) -> int | None:
        """The resume cursor for one session: the highest number a runner gave a frame in it.

        **Per session, not per connection**, which is the property the whole scheme rests on: two
        consoles can be adopting one runner's window during a roll, and both compute this from the
        same rows, so they agree on what has been recorded. None is a session whose log holds
        nothing a runner numbered — a fresh session, or one served entirely by a runner image
        predating the field — and the runner reading it replays its whole window as before.

        It is a **floor**, and the runner treats it as one (`OutboundLog.seed`). A `setup_output`
        row cannot carry its number here (the runner numbers the frame, the console records the
        lines it decoded into, and one is not the other), so the cursor can sit below what the
        console truly holds. That costs a re-sent frame the log already has, which the `frame_uid`
        dedup refuses — never a frame skipped, which is the direction that would lose one.
        """
        async with self._sessions() as db:
            return await db.scalar(
                select(func.max(SessionFrame.runner_seq)).where(SessionFrame.session_id == session_id)
            )

    async def list_conversations(self, *, cursor: ConversationCursor | None, limit: int) -> list[Conversation]:
        """Past sessions from *cursor*, newest first, for the `haku_conversations` read tools.

        Keyset paging on `(created_at, session_id)`, for the same reason `read_frames` pages on
        `frame_seq`: an offset counts from the top of the order, and this order grows at the top
        while a reader walks it, so every session created mid-walk would push a row across a page
        boundary — skipping it or repeating it. `session_id` is in the key because `created_at`
        alone is not a total order; a pair created in one instant would straddle the boundary.

        Unscoped by R5.3a: every session, whichever room it served. Inclusive of the row the
        cursor names, which is the first row the previous page did not return — so a caller
        resumes with the cursor it was handed and no arithmetic.
        """
        query = select(Session).order_by(Session.created_at.desc(), Session.session_id.desc())
        if cursor is not None:
            query = query.where(
                tuple_(Session.created_at, Session.session_id)
                <= tuple_(literal(cursor.created_at), literal(cursor.session_id))
            )
        async with self._sessions() as db:
            rows = (await db.scalars(query.limit(limit))).all()
        return [
            Conversation(
                session_id=row.session_id,
                surface=row.surface,
                room_id=row.room_id,
                status=row.status,
                created_at=row.created_at,
                error=row.error,
            )
            for row in rows
        ]

    async def read_frames(
        self, session_id: UUID, *, cursor: FrameCursor | None, limit: int, kinds: Sequence[str] | None
    ) -> list[RolloutFrame]:
        """One page of a session's rollout, in wire order, from the start of the log onwards.

        Keyset paging on `frame_seq` rather than an offset: the log is append-only, so a cursor
        cannot skip or repeat a row the way an offset would once new frames land between pages.
        The cursor names the first frame to return rather than the last one already returned, so
        a transcript entry's `first_frame_seq` is a cursor as it stands.
        """
        query = _frames_of_kinds(select(SessionFrame).where(SessionFrame.session_id == session_id), kinds)
        if cursor is not None:
            query = query.where(SessionFrame.frame_seq >= cursor.frame_seq)
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

    async def read_transcript(
        self, session_id: UUID, *, cursor: TranscriptCursor | None, limit: int
    ) -> TranscriptSlice:
        """A window of the session's projected transcript, for the `haku_conversations` reader.

        **The fold always runs from the session's first frame**, whatever the cursor says — which
        is what `project_log` is: the reducer seeded empty and told the stream ends here. A window
        would need the state the frames before it left behind, and this reader has nowhere to keep
        one; seeding empty from a suffix instead would let a page boundary close a message the
        whole session does not end there, and the same entry would read differently depending on
        which page it landed on. Determinism is the property the projection exists for
        (<claude_code/projection.py>), and it is not a property of a suffix read cold. A stored
        cursor is what turns this into a window, and it is deliberately not part of this change.

        That costs one read of the session's projectable frames per page — the last O(session)
        read on any path, now that the SPA's detail view reads stored events instead of re-parsing
        the log (`session_views.tool_calls`).

        Two kinds are excluded in SQL rather than by the fold. `setup_output` is the console's own
        envelope and carries no protocol `type` at all, so the fold would refuse it; deltas are
        hundreds per turn of prose that arrives again whole. Everything else is passed through, so
        a frame class the CLI adds still lands in `Projection.unprojected` and is reported.
        """
        async with self._sessions() as db:
            rows = (
                await db.scalars(
                    select(SessionFrame)
                    .where(
                        SessionFrame.session_id == session_id,
                        SessionFrame.kind.not_in([SETUP_OUTPUT_KIND, DELTA_FRAME_KIND]),
                    )
                    .order_by(SessionFrame.frame_seq)
                )
            ).all()
        projected = projection.project_log(
            projection.RecordedFrame(frame_seq=row.frame_seq, payload=row.payload) for row in rows
        )
        entries = transcript_entries.entries(projected)
        start = cursor.index if cursor is not None else 0
        return TranscriptSlice(
            entries=entries[start : start + limit], unreadable=transcript_entries.unreadable(projected)
        )

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

    async def apply_frame(
        self, session_id: UUID, turn_id: UUID, frame_seq: int, events: Sequence[ConversationEvent]
    ) -> TurnState:
        """Write what one frame's events imply, and move the cursor past that frame, together.

        **One transaction, and the cursor is inside it.** The message row, the neutral event rows,
        the room's outbox row, the turn's state and `sessions.projected_frame_seq` commit or do not
        commit as one, which
        is the whole of what makes those effects exactly-once: a process that dies anywhere leaves
        the cursor naming the last frame whose effects are durable, so whoever adopts the session
        re-projects from there and redoes exactly the frames whose effects did not commit
        (<../../plans/chat_runtime_projection.md> § The shape).

        **A frame that ends the turn does not come here.** Closing the turn is `end_turn`'s
        transaction and the turn's last word is written before it, so advancing the cursor here for
        that frame would put it ahead of writes still to come; `end_turn` takes it instead. Under
        per-frame projection the ending frame produces exactly that one event and nothing else.

        The returned state is the turn's after these writes, so the caller holds no message
        identity or accumulated prose of its own between frames.
        """
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            turn = await db.get(SessionTurn, turn_id, with_for_update=True)
            if turn is None:
                raise KeyError(turn_id)
            chat = await db.get(Session, session_id)
            if chat is None:
                raise KeyError(session_id)
            message = (
                None if turn.assistant_message_id is None else await db.get(SessionMessage, turn.assistant_message_id)
            )
            # The calls of the message being assembled. Per frame rather than per turn, because a
            # message never spans two frames here (`frame_projection.projected`) and they are
            # written with the message that made them.
            tool_calls: list[RecordedToolCall] = []
            for event in events:
                match event:
                    case TextDelta():
                        message = message or await _open_assistant(db, session_id, turn, frame_seq, now)
                        message.content += event.text
                        message.source_last_frame_seq = frame_seq
                        message.status = ChatMessageStatus.STREAMING
                        message.updated_at = now
                    case ToolCallStarted():
                        tool_calls.append(
                            RecordedToolCall(
                                call_id=event.call_id, tool_name=event.tool_name, arguments=dict(event.arguments)
                            )
                        )
                    case MessageCompleted():
                        message = message or await _open_assistant(db, session_id, turn, frame_seq, now)
                        message.content = (event.text or "").strip() or message.content.strip()
                        message.tool_calls = tool_calls
                        # Provenance, not identity: it is what the frames called this message, and
                        # it is what lets a reader find its calls in the log rather than match by
                        # position. Absent on thousands of production rows, which is why nothing
                        # keys on it.
                        if event.agent_message_id is not None:
                            message.agent_message_id = event.agent_message_id
                        message.source_last_frame_seq = frame_seq
                        message.status = ChatMessageStatus.COMPLETE
                        message.updated_at = now
                        # Each message is queued for the room as it finishes rather than only the
                        # final answer (R11.1), so a turn that narrates, works and reports back is
                        # three messages in the room and not just its conclusion.
                        owed = await _enqueue_reply(
                            db,
                            chat,
                            message.content,
                            message_id=message.message_id,
                            agent_message_id=message.agent_message_id,
                            turn_id=None,
                            now=now,
                        )
                        turn.assistant_message_id = None
                        turn.said_anything = True
                        turn.queued_reply = turn.queued_reply or owed
                        message, tool_calls = None, []
                    case _:
                        # Every event is stored below, whatever the transcript row does with it.
                        # This arm is what the *message* row makes of one — nothing, for reasoning,
                        # a tool's answer and the harness's narration.
                        pass
            # In this transaction, so a row exists here exactly when the cursor says its frame was
            # projected: a process that dies leaves no half-written stream to reconcile, and the
            # frames whose effects did not commit are re-projected into rows that were never
            # written.
            db.add_all(
                stored
                for event in events
                if (stored := session_events.row(event, session_id=session_id, turn_id=turn_id, now=now)) is not None
            )
            _advance_cursor(chat, frame_seq)
            chat.updated_at = now
            await notify(db, SessionEventKind.UPDATE, session_id)
            return TurnState(
                assistant_message_id=turn.assistant_message_id,
                streamed="" if message is None else message.content,
                said_anything=turn.said_anything,
                queued_reply=turn.queued_reply,
            )

    async def begin_assistant(self, session_id: UUID, turn_id: UUID, *, source_first_frame_seq: int) -> UUID:
        """Open the message this turn is about to stream into, and point the turn at it.

        One transaction, because the pointer is what makes the message the *turn's*: a replica
        dying with a message row nothing names would leave its replacement to guess which of the
        session's messages it was in the middle of.

        *source_first_frame_seq* is the frame that opened the message, written here rather than on
        every update: this is the one moment that knows where the message began, and a resumed turn
        picks its message up from the turn row without passing through here — so leaving `first` to
        a later write would walk it forward past the frames the earlier process already projected.
        Required, because `ck_session_messages_assistant_pointed` refuses a row without it.
        """
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            if (turn := await db.get(SessionTurn, turn_id)) is None:
                raise KeyError(turn_id)
            message = await _open_assistant(db, session_id, turn, source_first_frame_seq, now)
            return message.message_id

    async def update_assistant(
        self,
        session_id: UUID,
        message_id: UUID,
        content: str,
        *,
        tool_calls: list[RecordedToolCall] | None = None,
        agent_message_id: str | None = None,
        source_last_frame_seq: int | None = None,
        complete: bool = False,
    ) -> bool:
        """Write what this assistant message says so far. True once the room owes it.

        **A completed message queues the room's copy in this same transaction** — the two facts
        commit together or not at all, and `session_outbox` is what says the room is still owed
        one. Writing it at delivery time instead loses the answer whenever the turn raises in
        between (<../debug/message_drops.md> E4).

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
            if tool_calls is not None:
                message.tool_calls = tool_calls
            if agent_message_id is not None:
                message.agent_message_id = agent_message_id
            # Only the far end moves here. `begin_assistant` set the near end once, and frames
            # within a message arrive in order, so this only ever widens the range.
            if source_last_frame_seq is not None:
                message.source_last_frame_seq = source_last_frame_seq
            message.status = ChatMessageStatus.COMPLETE if complete else ChatMessageStatus.STREAMING
            message.updated_at = now
            # No `chat.status = RESPONDING` here: this runs per stream delta, and the open turn
            # already states it.
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

    async def set_message_source_frames(self, session_id: UUID, message_id: UUID, frame_seq: int) -> None:
        """Point an already-written message at the single frame it went out as.

        For the operator's own prompt, whose row exists before the frame does.
        """
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

        Two callers, and at most one of them per turn: `result.result` on a turn whose completed
        assistant messages were all empty — a turn that completed none at all has a row minted for
        it instead — and the notice an aborted turn leaves. `turn_id` is the idempotence key that
        makes re-derivation by a replacement replica a no-op rather than a second copy in the
        room — see `session_outbox.turn_id`.

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
        # the room just stops answering.
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
            if chat is not None and chat.status in LEASED_SESSION_STATUSES:
                chat.lease_expires_at = datetime.now(UTC) + LEASE_TTL
                chat.lease_holder = REPLICA

    async def expire_stale_leases(self) -> int:
        """Fail every leased session nobody came back for, and report how many.

        A held status is only ever corrected by the replica that wrote it, so a replica dying
        without its finalizer — SIGKILL, OOM, node loss — leaves a row claiming a turn is in
        flight that `supervise_once` reads as healthy. This is the only observer that is not that
        process.

        **An expired lease means unowned, not dead**, and the threshold below is the whole of that
        distinction. `authenticate_bridge` already admits any runner once the lease has lapsed —
        it refuses only while somebody else's is still valid — so an expired session is adoptable
        without anything having to hand it back. What it is not is *instantly* adopted: the runner
        redials on a backoff, and failing the row the moment the lease lapses beats that redial
        every time.

        So a session is dead only once it has been adoptable for a whole `ADOPTION_GRACE` and
        nobody took it. `release_lease` is the fast path that skips that wait, not the thing
        correctness rests on, which no finalizer can be.

        Set-based and idempotent, like `node_daemons._expire`: any replica may run it, concurrent
        runners converge, and a merely slow owner renews well before the TTL.
        """
        async with self._sessions.begin() as db:
            expired = (
                await db.scalars(
                    select(Session.session_id).where(
                        Session.status.in_(LEASED_SESSION_STATUSES),
                        Session.lease_expires_at <= datetime.now(UTC) - ADOPTION_GRACE,
                    )
                )
            ).all()
            for session_id in expired:
                # Row-at-a-time rather than one UPDATE: `notify` is per session, and a room that
                # is not told its session died simply goes quiet.
                chat = await db.get(Session, session_id, with_for_update=True)
                if chat is None or chat.status not in LEASED_SESSION_STATUSES:
                    continue
                # Which of the three ended it is read off two columns that the failure itself then
                # leaves behind, so it is recorded as an event rather than only rendered into the
                # error prose the operator sees. "mid-turn" only if a turn was in fact open.
                mid_turn = " mid-turn" if chat.status == SessionStatus.RESPONDING else ""
                if chat.lease_holder is not None:
                    reason = LeaseExpiryReason.HOLDER_GONE
                elif chat.bridge_connected_at is not None:
                    reason = LeaseExpiryReason.UNADOPTED
                else:
                    reason = LeaseExpiryReason.NEVER_ATTACHED
                detail = _expiry_detail(reason, chat.lease_holder)
                logger.error("session %s lease expired: %s", session_id, detail)
                now = datetime.now(UTC)
                db.add(
                    session_events.authored(
                        session_events.LeaseExpiredBody(reason=reason, last_holder=chat.lease_holder),
                        session_id=session_id,
                        now=now,
                    )
                )
                chat.status = SessionStatus.FAILED
                chat.error = f"console session ended{mid_turn}: {detail}"
                chat.updated_at = now
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


def _expiry_detail(reason: LeaseExpiryReason, holder: str | None) -> str:
    """What the operator is told, derived from the reason the sweep recorded."""
    match reason:
        case LeaseExpiryReason.HOLDER_GONE:
            return f"the console replica holding it ({holder}) went away"
        case LeaseExpiryReason.UNADOPTED:
            return "its runner went away and no replica took it back over"
        case LeaseExpiryReason.NEVER_ATTACHED:
            return "a runner never attached"


def _advance_cursor(chat: Session, frame_seq: int | None) -> None:
    """Move the session's projection cursor to *frame_seq*, never backwards.

    Monotone because two writers can reach it out of order: `end_turn` carries the frame that
    ended the turn while the turn's last word — written a moment earlier and through
    `update_assistant` — carries none, and a retried adoption re-projects frames the cursor has
    already passed.
    """
    if frame_seq is not None and (chat.projected_frame_seq is None or chat.projected_frame_seq < frame_seq):
        chat.projected_frame_seq = frame_seq


async def _unprojected_frames(db: AsyncSession, session_id: UUID, cursor: int) -> tuple[ReceivedFrame, ...]:
    """The recorded frames past *cursor* — the ones whose effects did not commit.

    Deltas are in it, because their effects are message content and the cursor is what says
    whether that content landed. What is left out is what the console authored rather than
    received: `setup_output`, which carries no protocol `type` for the fold to read, and a
    `partial` row — this console's own reconstruction of an answer in flight, which projecting
    would turn into a message the agent never sent. Nothing writes `partial` any more; the filter
    goes with the column (`SessionFrame.partial`'s CLEANUP note).
    """
    rows = await db.scalars(
        select(SessionFrame)
        .where(
            SessionFrame.session_id == session_id,
            SessionFrame.frame_seq > cursor,
            SessionFrame.partial.is_(False),
            SessionFrame.kind != SETUP_OUTPUT_KIND,
        )
        .order_by(SessionFrame.frame_seq)
    )
    return tuple(ReceivedFrame(payload=row.payload, frame_seq=row.frame_seq) for row in rows)


async def _open_assistant(
    db: AsyncSession, session_id: UUID, turn: SessionTurn, source_first_frame_seq: int, now: datetime
) -> SessionMessage:
    """Open an assistant message and point *turn* at it, inside the caller's transaction.

    The pointer is what makes the message the turn's: a replica dying with a message row nothing
    names would leave its replacement to guess which of the session's messages it was in the
    middle of.
    """
    message = SessionMessage(
        message_id=uuid4(),
        session_id=session_id,
        role=ChatMessageRole.ASSISTANT,
        status=ChatMessageStatus.STREAMING,
        content="",
        error=None,
        source_first_frame_seq=source_first_frame_seq,
        created_at=now,
        updated_at=now,
    )
    db.add(message)
    # Flushed before the turn names it: the pointer is a foreign key, so the message has to exist
    # by the time the update lands.
    await db.flush()
    turn.assistant_message_id = message.message_id
    return message


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


def _turn_usage(turn: SessionTurn) -> TurnUsage | None:
    """What a closed turn cost, or None where its backend reported nothing at all.

    A counter that is NULL beside a cost or a duration reads as 0 rather than as "no usage": that
    is already what `Usage` says an unreported counter means, and it is the state a turn closed by
    a replica on the image before these columns existed leaves behind for the length of a roll.
    """
    if turn.input_tokens is None and turn.cost_usd is None and turn.duration_ms is None:
        return None
    return TurnUsage(
        input_tokens=turn.input_tokens or 0,
        output_tokens=turn.output_tokens or 0,
        cached_input_tokens=turn.cached_input_tokens or 0,
        cost_usd=None if turn.cost_usd is None else float(turn.cost_usd),
        duration_ms=turn.duration_ms,
    )


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

    **The console's own record is the evidence, not the CLI's acknowledgement.** `sent()` records
    the frame before `channel.write` (`cli_client._write`), so a row here means this end committed
    to sending the prompt, and the CLI's `command_lifecycle` — the only thing that would say
    whether the *CLI* has it — may still be sitting in the runner's replay window, unrecorded,
    because replay does not begin until the socket is accepted and this runs before that. Asking a
    question the record cannot yet answer would re-ask a prompt the agent already has, which is the
    worse of the two failures: a duplicate turn instead of a lost one.

    So the ambiguous middle — recorded, and then the write or the replica died — is deliberately
    treated as delivered, and what this closes is the window where nothing was recorded at all.
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
