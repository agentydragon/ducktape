"""Postgres store for operator sessions — the rows, and which of them commit together.

The service that drives a turn is next door in `session_runtime.py`; the line between the two is
the transaction. A method whose job is "these writes commit together or not at all" is here —
`apply_frame` writing a frame's events, the items they materialise and the session's cursor in one
transaction is what makes adoption a read without replaying committed effects.

Neutral runtime: no channel and no harness, so a second channel inherits every row in it.

"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy import CursorResult, Select, Subquery, func, literal, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.conversation import conversation_event, log, prompt_inbox
from haku.console.conversation.conversation_event import FrameRange, PromptRejection
from haku.console.conversation.item_reads import ConversationPageRow, item_of, turn_end_of
from haku.console.conversation.item_vocabulary import ItemStatus, ItemType
from haku.console.conversation.prompt_origin import HARNESS_ORIGIN, PromptOrigin
from haku.console.conversation.reads import (
    ChannelAttachment,
    FrameRecord,
    HarnessFrameRecord,
    SessionCursor,
    SessionRecord,
    SetupOutputRecord,
    TurnCursor,
    TurnRecord,
)
from haku.console.conversation_read_access import (
    ConversationAccessDeniedError,
    ConversationReadScope,
    ProfileScopedReads,
)
from haku.console.database_schema import (
    ChannelAttachmentRow,
    Conversation,
    ConversationEventRow,
    ConversationItem,
    ConversationPrompt,
    ConversationTurn,
    HttpGrantRow,
    KubernetesGrantRow,
    Session,
    SessionFrame,
    SubmittedPrompt,
)
from haku.console.grants.principal import GrantPrincipalKind
from haku.console.harnesses.kind import HarnessKind
from haku.console.notifications.conversation_wakes import ConversationWakeKind, notify_conversation, notify_update
from haku.console.notifications.session_wakes import SessionEventKind, notify
from haku.console.session.conversation_views import (
    ConversationCursor,
    ConversationPage,
    ConversationSummary,
    ConversationUpdate,
    ConversationView,
    SessionFramePage,
    SessionView,
    frame_page,
    session_view,
    setup_narration,
)
from haku.console.session.launch_identity import LaunchAgentRejectedError, LaunchAuthorizer
from haku.console.session.session_frames import BridgeFrameKind, FrameDirection
from haku.console.session.setup_output import SETUP_OUTPUT_KIND, setup_output_frame
from haku.console.session.status import (
    ENDED_SESSION_STATUSES,
    LEASED_SESSION_STATUSES,
    OPEN_SESSION_STATUSES,
    LeaseExpiryReason,
    SessionStatus,
)
from haku.console.session.subscription import stream_head
from haku.console.x.conversation_events import ConversationEvent, ItemSegment, MessageCompleted, MessageStarted, OpenRef
from haku.runner.client import RecordedFrame

logger = logging.getLogger(__name__)

# How long a live session stays believed-in after its holder last spoke, and how often that holder
# speaks. The gap absorbs a slow database round trip or a paused event loop without anyone
# reclaiming a session that is merely busy. A turn may run far longer than the TTL — the renewal is
# a separate task precisely so a long answer does not read as a dead replica.
LEASE_TTL = timedelta(seconds=90)
LEASE_RENEW_INTERVAL = timedelta(seconds=30)
# The creator's grant, covering the gap before a runner attaches and starts renewing. Longer
# than `LEASE_TTL` because it has to cover an image pull onto a cold node.
PROVISION_LEASE = timedelta(minutes=10)
# What a replica going down cleanly leaves behind: long enough for the runner to notice the socket
# close and redial onto whichever replica is up. Shorter than `LEASE_TTL` because nothing is
# holding it — this is a window for an adopter to appear, not a heartbeat anyone is keeping.
ADOPTION_GRACE = timedelta(seconds=45)

# This process, as the lease records its holder. Kubernetes sets HOSTNAME to the pod name, which
# is what `kubectl logs` wants as an argument — so a session that died names the thing to go read.
REPLICA = os.environ.get("HOSTNAME", "unknown")


def _bridge_frames(
    query: Select[tuple[SessionFrame]], kinds: Sequence[BridgeFrameKind] | None = None
) -> Select[tuple[SessionFrame]]:
    """Restrict a frame query using only Haku's outer bridge discriminator.

    The default rollout is the selected harness's native wire. Console-authored setup narration is
    available when explicitly requested, and through its dedicated narration view, but is not a
    native harness frame merely because it shares the append-only table.
    """
    selected = [BridgeFrameKind.HARNESS_FRAME] if kinds is None else list(kinds)
    return query.where(SessionFrame.kind.in_(selected))


class PromptRecords(Protocol):
    """What a caller needs written in the prompt's own transaction, given the inbox id it minted.

    A channel's record of which of its own inbound events a prompt carries. It has to commit with
    the prompt or not at all: written afterwards it is lost by a crash in between, and the channel
    then cannot tell a message the record already holds from one it has never been offered.

    The store adds it and never reads it — what these rows mean is the caller's business. The id it
    hands over is the `submitted_prompt` id (#4667), the durable command the prompt is before it is
    any transcript item.
    """

    async def __call__(self, db: AsyncSession, prompt_id: UUID) -> None: ...


class PromptRefusedError(RuntimeError):
    """Admission would not take this prompt, and says which state refused it.

    Typed because the refusal is an answer a surface renders and records rather than a string it
    logs: the room tells the operator what to wait for, and `AuthoredEventKind.PROMPT_REJECTED`
    stores the same member.
    """

    def __init__(self, reason: PromptRejection):
        super().__init__(f"the session cannot take a prompt right now ({reason})")
        self.reason = reason


async def _live_attachments(db: AsyncSession, conversations: set[UUID]) -> dict[UUID, list[ChannelAttachment]]:
    """The channels currently holding a copy of each of *conversations*, keyed by conversation.

    Total over the argument: a conversation nothing is attached to gets an empty list rather than a
    missing key, so a caller never has to decide what an absent entry meant.
    """
    rows = (
        await db.scalars(
            select(ChannelAttachmentRow)
            .where(ChannelAttachmentRow.conversation_id.in_(conversations), ChannelAttachmentRow.detached_at.is_(None))
            .order_by(ChannelAttachmentRow.attached_at, ChannelAttachmentRow.attachment_id)
        )
    ).all()
    attachments: dict[UUID, list[ChannelAttachment]] = {conversation: [] for conversation in conversations}
    for row in rows:
        attachments[row.conversation_id].append(
            ChannelAttachment(surface=row.surface, address=row.address, attached_at=row.attached_at)
        )
    return attachments


async def _require_readable_conversation(db: AsyncSession, conversation_id: UUID, scope: ConversationReadScope) -> None:
    """Refuse a point read of a conversation outside *scope*'s readable profiles.

    A conversation that does not exist passes: the read it guards then returns its natural empty
    page, exactly as before scoping, so absence and emptiness stay one shape. Only a row that
    exists and pins a profile outside the scope — or predates pinned identity, for a non-Operator
    scope — is refused, loudly, so a future grant misconfiguration reads as a denial rather than
    as an inexplicably empty transcript.
    """
    row = (
        await db.execute(select(Conversation.access_profile_id).where(Conversation.conversation_id == conversation_id))
    ).one_or_none()
    if row is not None and not scope.allows(row.access_profile_id):
        raise ConversationAccessDeniedError(f"{conversation_id=}")


async def _require_readable_session(db: AsyncSession, session_id: UUID, scope: ConversationReadScope) -> None:
    """`_require_readable_conversation`, resolved through the session that names it."""
    row = (
        await db.execute(
            select(Conversation.access_profile_id)
            .join(Session, Session.conversation_id == Conversation.conversation_id)
            .where(Session.session_id == session_id)
        )
    ).one_or_none()
    if row is not None and not scope.allows(row.access_profile_id):
        raise ConversationAccessDeniedError(f"{session_id=}")


async def _item_counts(db: AsyncSession, conversations: set[UUID]) -> dict[UUID, int]:
    """How many transcript rows each of *conversations* holds.

    Asked of the items directly rather than joined through `sessions`: items are the conversation's,
    so a thread that has run several sessions needs no join, and one holding a prompt no session has
    claimed is counted rather than missed.
    """
    rows = await db.execute(
        select(ConversationItem.conversation_id, func.count(ConversationItem.item_id))
        .where(ConversationItem.conversation_id.in_(conversations))
        .group_by(ConversationItem.conversation_id)
    )
    counted = {conversation: count for conversation, count in rows.all() if conversation is not None}
    return {conversation: counted.get(conversation, 0) for conversation in conversations}


async def _live_sessions(db: AsyncSession, conversations: set[UUID]) -> dict[UUID, SessionView]:
    """The session holding each of *conversations*, for those a session is holding.

    At most one per conversation — the rule neutral runtime supervision keeps — so a duplicate is a bug. No
    index enforces it and a listing is the wrong place to raise over it, so the newest wins.

    **`responding` is derived, not read** (`session_view`): the column carries no turn state, so a
    session with an open turn reports `responding` however its row reads.
    """
    rows = (
        await db.scalars(
            select(Session)
            .where(Session.conversation_id.in_(conversations), Session.status.in_(OPEN_SESSION_STATUSES))
            .order_by(Session.created_at)
        )
    ).all()
    responding = set(
        (
            await db.scalars(
                select(ConversationTurn.session_id).where(
                    ConversationTurn.session_id.in_([row.session_id for row in rows]),
                    ConversationTurn.ended_at.is_(None),
                )
            )
        ).all()
    )
    return {row.conversation_id: session_view(row, responding=row.session_id in responding) for row in rows}


async def _last_ended_sessions(db: AsyncSession, conversations: set[UUID]) -> dict[UUID, SessionStatus]:
    """How each of *conversations*' newest session ended.

    Asked only about conversations no session is holding, so every answer is a terminal status —
    what lets the inventory tell a thread whose runner failed from one that closed cleanly.
    """
    rows = (
        await db.execute(
            select(Session.conversation_id, Session.status)
            .where(Session.conversation_id.in_(conversations))
            .distinct(Session.conversation_id)
            .order_by(Session.conversation_id, Session.created_at.desc())
        )
    ).tuples()
    return dict(rows.all())


class BridgeAuthentication(StrEnum):
    """What admission has to say to a redialling runner.

    **"Not yours" and "not yet" are different.** The runner redials about a second after its socket
    drops, so it routinely arrives at a new replica while the dying one's lease is still valid, and
    a refusal it cannot retry costs the sandbox — hence `session_runtime.handle_journal_runner`
    answering `HELD` with a 5xx handshake response.
    """

    ACCEPTED = "accepted"
    # The session is already over, so the runner should stop rather than retry.
    TERMINAL = "terminal"
    # The credential is wrong. Permanent.
    REJECTED = "rejected"
    # Another replica is still serving this session and saying so. **Transient**: it lasts at most
    # until that lease expires, and the runner that waits it out is the one adopting the session.
    HELD = "held"


@dataclass(frozen=True, slots=True)
class TurnStart:
    """A prompt taken off the queue together with the turn opened to answer it."""

    turn_id: UUID
    item_id: UUID
    prompt: str


@dataclass(frozen=True, slots=True)
class WakeTurn:
    """An exchange the harness began itself, opened when its first frame arrived.

    Nothing to send: the harness is already mid-exchange, so the turn loop only drains it.
    """

    turn_id: UUID


@dataclass(frozen=True, slots=True)
class OpenItem:
    """A prose item a departed holder was streaming into, as the adopting fold needs it back.

    Its prose rather than its id: the store finds the row again through the turn, so what the fold
    cannot re-derive is how much of it has already been said. The frame numbers are the span its rows
    were read from, so the completion this fold eventually writes reports where the message began
    rather than where the adoption did.
    """

    text: str
    first_frame_seq: int
    last_frame_seq: int


@dataclass(frozen=True, slots=True)
class TurnState:
    """How far a turn has got, read off the items it opened.

    Derived rather than recorded: what a turn is streaming into is its one open message item, and
    whether it said anything is whether it has a completed one. The columns this replaces were the
    turn loop's own bookkeeping kept on the turn row, which is a second place the same facts could
    be wrong.

    Delivery is deliberately absent. Whether a room has been told is the channel's, and it reads
    the log forward from its own cursor rather than being handed a flag by the turn that produced
    the words.
    """

    # The prose of the message still being streamed into, or None when none is open. Empty string
    # is a real state — a message opened and not yet spoken into.
    streaming: str | None
    said_anything: bool


@dataclass(frozen=True, slots=True)
class SessionOutcome:
    """Where a session got to, and why if it ended badly.

    The two travel together because every caller acting on a dead session wants to say which.
    """

    status: SessionStatus
    error: str | None


@dataclass(frozen=True, slots=True)
class OperatorSessionIdentity:
    """The conversation identity needed by a session-addressed operator inspection."""

    status: SessionStatus
    agent_id: UUID | None
    access_profile_id: str | None
    harness_kind: HarnessKind


@dataclass(frozen=True, slots=True)
class SessionAllocation:
    """The sandbox session credential minted by the transaction that starts provisioning."""

    session_id: UUID
    bridge_token: str


@dataclass(frozen=True, slots=True)
class SandboxDemand:
    """An idle Operator-owned session whose conversation has an unclaimed prompt."""

    operator_id: UUID
    session_id: UUID


@dataclass(frozen=True, slots=True)
class ConversationDemand:
    """An Operator-owned conversation with queued work and no open session."""

    operator_id: UUID
    conversation_id: UUID


@dataclass(frozen=True, slots=True)
class DispatchablePrompt:
    """One pending inbox prompt the runner is owed, as `protocol.PromptDispatch` carries it."""

    prompt_id: UUID
    text: str


class PositionUnusableError(Exception):
    """An update cannot be served from a follower's position; it must be sent the conversation whole.

    Two causes and one recovery, which is why they are one exception rather than two: the position
    names no row this conversation's log still has, or so much has moved since it that the update
    would carry most of a snapshot anyway. Never reaches a client — `ConversationFollow` answers
    with a snapshot, which is what makes snapshot-or-resume the server's decision.
    """


class Store:
    """Async Postgres store for agent sessions."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], *, adoption_grace: timedelta = ADOPTION_GRACE):
        self._sessions = sessions
        # Injectable so a full-stack test can exercise the real adoption path without spending the
        # production window in wall clock; `console_replica` is the only caller that shortens it.
        self._adoption_grace = adoption_grace

    @property
    def sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        """The store's session factory, for a collaborator committing in its own transaction —
        the journal consumer (#4667), whose commits are the store's transactions by another name."""
        return self._sessions

    @staticmethod
    def _fingerprint(token: str) -> bytes:
        return hashlib.sha256(token.encode()).digest()

    @staticmethod
    async def _end_session(db: AsyncSession, chat: Session, *, error: str | None, now: datetime) -> None:
        """Persist the facts that end a session and its durable account in the same transaction.

        *error* is the whole decision: an end with one is `failed`, without one `closed` — the pair
        of a terminal member and a disagreeing error is not expressible. The event body carries the
        member the facts derive, so the stream and the row cannot say different things.
        """
        chat.ended_at = now
        chat.error = error
        chat.updated_at = now
        writer = await log.writer_for(db, chat.conversation_id, session_id=chat.session_id, turn_id=None, now=now)
        writer.authored(conversation_event.SessionEnded(status=chat.status, error=error))
        # Exact-session authority never transfers to a replacement session. End it in the same
        # transaction as the session's terminal event so authorization and the durable account
        # cannot disagree. Grant status is derived, so already-expired leases need no write: one
        # end fact ends every lease this session could still exercise.
        for row in (KubernetesGrantRow, HttpGrantRow):
            await db.execute(
                update(row)
                .where(
                    row.principal_kind == GrantPrincipalKind.SESSION,
                    row.principal_session_id == chat.session_id,
                    row.ended_at.is_(None),
                    row.expires_at > now,
                )
                .values(ended_at=now, end_reason="principal_ended")
            )

    async def create(
        self,
        operator_id: UUID,
        *,
        conversation_id: UUID | None = None,
        agent_id: UUID | None = None,
        access_profile_id: str | None = None,
        harness_kind: HarnessKind | None = None,
        launch_authorizer: LaunchAuthorizer | None = None,
    ) -> tuple[SessionView, str]:
        """Open the idle session used by every production caller."""
        return await self.create_idle(
            operator_id,
            conversation_id=conversation_id,
            agent_id=agent_id,
            access_profile_id=access_profile_id,
            harness_kind=harness_kind,
            launch_authorizer=launch_authorizer,
        )

    async def create_idle(
        self,
        operator_id: UUID,
        *,
        conversation_id: UUID | None = None,
        agent_id: UUID | None = None,
        access_profile_id: str | None = None,
        harness_kind: HarnessKind | None = None,
        launch_authorizer: LaunchAuthorizer | None = None,
    ) -> tuple[SessionView, str]:
        """Open an authorized idle session without allocating its sandbox.

        The first accepted prompt is durable demand. `allocate` later locks this row, mints the
        sandbox session credential and moves the session to provisioning before anybody writes to
        Kubernetes. An empty conversation therefore owns no lease and no SandboxClaim.

        Conversation identity is selected once, in the same transaction that inserts the thread.
        A replacement locks that row and re-authorizes the pinned identity without consulting the
        Agent's current profile as a new selector.
        """
        now = datetime.now(UTC)
        session_id = uuid4()
        agent_binding_id: UUID | None = None
        async with self._sessions.begin() as db:
            if conversation_id is None:
                if launch_authorizer is not None:
                    if agent_id is None:
                        raise LaunchAgentRejectedError
                    if harness_kind is None:
                        raise LaunchAgentRejectedError("chat launch requires a selected harness")
                    identity = await launch_authorizer(db, operator_id, agent_id, harness_kind)
                    agent_id = identity.agent_id
                    agent_binding_id = identity.binding_id
                    access_profile_id = identity.access_profile_id
                    harness_kind = identity.harness_kind
                elif harness_kind is None:
                    raise ValueError("harness_kind is required for a new conversation")
                conversation_id = uuid4()
                db.add(
                    Conversation(
                        conversation_id=conversation_id,
                        operator_id=operator_id,
                        agent_id=agent_id,
                        access_profile_id=access_profile_id,
                        harness_kind=harness_kind,
                        created_at=now,
                    )
                )
                # Flushed before the session that points at it: a `ForeignKey` carrying no
                # `relationship()` does not order the unit of work.
                await db.flush()
            else:
                conversation = await db.scalar(
                    select(Conversation)
                    .where(Conversation.conversation_id == conversation_id, Conversation.operator_id == operator_id)
                    .with_for_update()
                )
                if conversation is None:
                    raise KeyError(conversation_id)
                if launch_authorizer is not None:
                    if conversation.agent_id is None or conversation.access_profile_id is None:
                        raise LaunchAgentRejectedError
                    identity = await launch_authorizer(
                        db,
                        operator_id,
                        conversation.agent_id,
                        conversation.harness_kind,
                        expected_profile_id=conversation.access_profile_id,
                    )
                    if (
                        identity.agent_id != conversation.agent_id
                        or identity.access_profile_id != conversation.access_profile_id
                        or identity.harness_kind != conversation.harness_kind
                    ):
                        raise LaunchAgentRejectedError
                    agent_binding_id = identity.binding_id
                elif any(
                    value is not None and value != expected
                    for value, expected in (
                        (agent_id, conversation.agent_id),
                        (access_profile_id, conversation.access_profile_id),
                        (harness_kind, conversation.harness_kind),
                    )
                ):
                    raise ValueError("replacement session does not match pinned conversation identity")
                agent_id = conversation.agent_id
                access_profile_id = conversation.access_profile_id
                harness_kind = conversation.harness_kind
                # Rolling coexistence: an older Matrix supervisor and the neutral reconciler use
                # different advisory locks. The durable conversation lock is therefore the shared
                # mutex, and an old writer reaching this method after the new one reuses its winner
                # rather than opening a second live session.
                existing = await db.scalar(
                    select(Session.session_id)
                    .where(Session.conversation_id == conversation_id, Session.status.in_(OPEN_SESSION_STATUSES))
                    .order_by(Session.created_at.desc(), Session.session_id.desc())
                    .limit(1)
                )
                if existing is not None:
                    session_id = existing
            if await db.get(Session, session_id) is None:
                db.add(
                    Session(
                        session_id=session_id,
                        operator_id=operator_id,
                        conversation_id=conversation_id,
                        agent_binding_id=agent_binding_id,
                        bridge_token_fingerprint=None,
                        bridge_connected_at=None,
                        error=None,
                        lease_expires_at=None,
                        created_at=now,
                        updated_at=now,
                    )
                )
        # Keep the historical tuple shape while making the absence explicit: this is not a bearer
        # and cannot authenticate. The real credential exists only in `SessionAllocation`.
        return await self.get(operator_id, session_id), ""

    async def conversations_awaiting_session(self) -> tuple[ConversationDemand, ...]:
        """Return conversation-owned prompt demand that no open session can serve.

        This is an unlocked work hint for the neutral runtime reconciler. Creation repeats both
        predicates while holding the conversation row lock, which is the exactly-once mutex across
        replicas and across channels offering prompts concurrently.
        """
        pending = _pending_prompts()
        open_session = (
            select(Session.session_id)
            .where(Session.conversation_id == pending.c.conversation_id, Session.status.in_(OPEN_SESSION_STATUSES))
            .exists()
        )
        async with self._sessions() as db:
            rows = await db.execute(
                select(
                    Conversation.operator_id,
                    pending.c.conversation_id,
                    func.min(pending.c.pending_since).label("oldest_prompt"),
                )
                .join(Conversation, Conversation.conversation_id == pending.c.conversation_id)
                .where(~open_session)
                .group_by(Conversation.operator_id, pending.c.conversation_id)
                .order_by("oldest_prompt", pending.c.conversation_id)
            )
            return tuple(
                ConversationDemand(operator_id=row.operator_id, conversation_id=row.conversation_id) for row in rows
            )

    async def ensure_session_for_demand(
        self, operator_id: UUID, conversation_id: UUID, *, launch_authorizer: LaunchAuthorizer | None = None
    ) -> SandboxDemand | None:
        """Create the one authorized idle session queued conversation work needs.

        The conversation row lock serializes every prompt writer and every competing reconciler.
        A replacement re-authorizes the conversation's pinned identity in this same transaction;
        then the queued prompt is attached to the new session so readers do not briefly lose the
        operator's text between admission and the runner claiming it.
        """
        now = datetime.now(UTC)
        session_id = uuid4()
        agent_binding_id: UUID | None = None
        async with self._sessions.begin() as db:
            conversation = await db.scalar(
                select(Conversation)
                .where(Conversation.conversation_id == conversation_id, Conversation.operator_id == operator_id)
                .with_for_update()
            )
            if conversation is None:
                return None
            if not await _has_pending_prompt(db, conversation_id):
                return None
            if (
                await db.scalar(
                    select(Session.session_id)
                    .where(Session.conversation_id == conversation_id, Session.status.in_(OPEN_SESSION_STATUSES))
                    .limit(1)
                )
                is not None
            ):
                return None
            if launch_authorizer is not None:
                if conversation.agent_id is None or conversation.access_profile_id is None:
                    raise LaunchAgentRejectedError
                identity = await launch_authorizer(
                    db,
                    operator_id,
                    conversation.agent_id,
                    conversation.harness_kind,
                    expected_profile_id=conversation.access_profile_id,
                )
                if (
                    identity.agent_id != conversation.agent_id
                    or identity.access_profile_id != conversation.access_profile_id
                    or identity.harness_kind != conversation.harness_kind
                ):
                    raise LaunchAgentRejectedError
                agent_binding_id = identity.binding_id
            db.add(
                session := Session(
                    session_id=session_id,
                    operator_id=operator_id,
                    conversation_id=conversation_id,
                    agent_binding_id=agent_binding_id,
                    bridge_token_fingerprint=None,
                    bridge_connected_at=None,
                    error=None,
                    lease_expires_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            await db.flush([session])
            # An inbox prompt needs no attach here — it is conversation-keyed and the runner
            # dispatches it (#4667). An unclaimed v3 queue prompt's item is still re-pointed at the
            # replacement, so readers do not briefly lose the operator's text between admission and
            # the claim; the queue retires with the native turn loop (stage 5).
            await db.execute(
                update(ConversationItem)
                .where(
                    ConversationItem.item_id.in_(
                        select(ConversationPrompt.item_id).where(
                            ConversationPrompt.conversation_id == conversation_id,
                            ConversationPrompt.claimed_at.is_(None),
                        )
                    )
                )
                .values(session_id=session_id, updated_at=now)
            )
            await notify(db, SessionEventKind.PROMPT, session_id)
            await notify_update(db, session_id=session_id, conversation_id=conversation_id)
        return SandboxDemand(operator_id=operator_id, session_id=session_id)

    async def _create_provisioning_for_test(
        self,
        operator_id: UUID,
        *,
        conversation_id: UUID | None = None,
        agent_id: UUID | None = None,
        access_profile_id: str | None = None,
        harness_kind: HarnessKind | None = None,
    ) -> tuple[SessionView, str]:
        """Seed the pre-lazy allocated state for focused tests of an already-running session."""
        now = datetime.now(UTC)
        session_id = uuid4()
        bridge_token = secrets.token_urlsafe(32)
        async with self._sessions.begin() as db:
            if conversation_id is None:
                conversation_id = uuid4()
                db.add(
                    Conversation(
                        conversation_id=conversation_id,
                        operator_id=operator_id,
                        agent_id=agent_id,
                        access_profile_id=access_profile_id,
                        harness_kind=harness_kind,
                        created_at=now,
                    )
                )
                await db.flush()
            else:
                conversation = await db.scalar(
                    select(Conversation)
                    .where(Conversation.conversation_id == conversation_id, Conversation.operator_id == operator_id)
                    .with_for_update()
                )
                if conversation is None:
                    raise KeyError(conversation_id)
                if any(
                    value is not None and value != expected
                    for value, expected in (
                        (agent_id, conversation.agent_id),
                        (access_profile_id, conversation.access_profile_id),
                        (harness_kind, conversation.harness_kind),
                    )
                ):
                    raise ValueError("test session does not match pinned conversation identity")
            db.add(
                session := Session(
                    session_id=session_id,
                    operator_id=operator_id,
                    conversation_id=conversation_id,
                    bridge_token_fingerprint=self._fingerprint(bridge_token),
                    bridge_connected_at=None,
                    error=None,
                    lease_expires_at=now + PROVISION_LEASE,
                    created_at=now,
                    updated_at=now,
                )
            )
            await db.flush([session])
            writer = await log.writer_for(db, conversation_id, session_id=session_id, turn_id=None, now=now)
            writer.authored(conversation_event.SessionProvisioning())
        return await self.get(operator_id, session_id), bridge_token

    async def allocate(self, operator_id: UUID, session_id: UUID) -> SessionAllocation | None:
        """Start provisioning an idle session once its conversation has work queued.

        The row lock is the allocation mutex. A prompt request and the runtime reconciler may both
        observe the same accepted prompt, but exactly one moves ``idle`` to ``provisioning`` — the
        credential fingerprint *is* that transition — and receives a credential with which to
        create the SandboxClaim. The prompt, bridge fingerprint, provisioning lease and lifecycle
        event are therefore durable before the external Kubernetes write begins.

        Returns ``None`` when the session is already allocated or no prompt has created demand yet.
        """
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            chat = await db.scalar(
                select(Session)
                .where(Session.session_id == session_id, Session.operator_id == operator_id)
                .with_for_update()
            )
            if chat is None:
                raise KeyError(session_id)
            if chat.status != SessionStatus.IDLE or not await _has_pending_prompt(db, chat.conversation_id):
                return None
            bridge_token = secrets.token_urlsafe(32)
            chat.bridge_token_fingerprint = self._fingerprint(bridge_token)
            chat.lease_expires_at = now + PROVISION_LEASE
            chat.updated_at = now
            writer = await log.writer_for(db, chat.conversation_id, session_id=session_id, turn_id=None, now=now)
            writer.authored(conversation_event.SessionProvisioning())
            await notify_update(
                db,
                session_id=session_id,
                conversation_id=chat.conversation_id,
                position=writer.conversation.next_event_seq - 1,
            )
            return SessionAllocation(session_id=session_id, bridge_token=bridge_token)

    async def sessions_awaiting_sandbox(self) -> tuple[SandboxDemand, ...]:
        """Return every idle session with durable demand, longest-waiting first.

        The prompt belongs to the conversation, so a replacement idle session sees work its
        predecessor never claimed. The result deliberately names no surface: SPA and Matrix
        prompts leave the same inbox row and follow the same allocation rule.

        This read does not lock. It is a work hint for the reconciler; ``allocate`` locks the
        session and repeats both predicates before minting a credential.
        """
        pending = _pending_prompts()
        async with self._sessions() as db:
            rows = await db.execute(
                select(Session.operator_id, Session.session_id)
                .join(pending, pending.c.conversation_id == Session.conversation_id)
                .where(Session.status == SessionStatus.IDLE)
                .order_by(pending.c.pending_since, Session.created_at, Session.session_id)
            )
            return tuple(SandboxDemand(operator_id=row.operator_id, session_id=row.session_id) for row in rows)

    async def get(self, operator_id: UUID, session_id: UUID) -> SessionView:
        async with self._sessions() as db:
            record = await db.scalar(
                select(Session).where(Session.session_id == session_id, Session.operator_id == operator_id)
            )
            if record is None:
                raise KeyError(session_id)
            responding = await _open_turn(db, record.conversation_id) is not None
            return session_view(record, responding=responding)

    async def conversation_of(self, session_id: UUID) -> UUID:
        """The thread this session runs, for a caller that has just created it."""
        async with self._sessions() as db:
            record = await db.get(Session, session_id)
            if record is None:
                raise KeyError(session_id)
            return record.conversation_id

    async def conversation_position(self, conversation_id: UUID) -> int:
        """Where this conversation's event stream has got to — what a follower reading it now holds.

        Zero for a conversation nothing has written an event for yet, which reads correctly as
        "everything after 0": `event_seq` is a global `Identity` and so is never 0 itself.
        """
        async with self._sessions() as db:
            return (await stream_head(db, conversation_id)).event_seq

    async def list_operator_conversations(
        self, operator_id: UUID, *, cursor: ConversationCursor | None, limit: int
    ) -> ConversationPage:
        """One page of this Operator's conversations, newest activity first.

        Scoped, unlike the MCP reader: a browser-facing inventory must never reveal another
        Operator's conversations.

        **Keyset**, because a conversation never ends: an offset counts from the top of an order
        that only grows there, so anything that moves mid-walk pushes a row across a page boundary.
        `limit + 1` rows are read so the last page is told from a full one without a second count
        query, and the extra row is what `next_cursor` names.

        Four reads rather than one join: the aggregates fan out per session and the attachments per
        conversation, so one query would multiply the two and count each message once per
        attachment.
        """
        activity = func.max(Session.updated_at).label("last_activity_at")
        page = (
            select(
                Conversation.conversation_id,
                Conversation.agent_id,
                Conversation.access_profile_id,
                Conversation.harness_kind,
                Conversation.created_at,
                activity,
            )
            .join(Session, Session.conversation_id == Conversation.conversation_id)
            .where(Conversation.operator_id == operator_id)
            .group_by(
                Conversation.conversation_id,
                Conversation.agent_id,
                Conversation.access_profile_id,
                Conversation.harness_kind,
                Conversation.created_at,
            )
            .order_by(activity.desc(), Conversation.conversation_id.desc())
            .limit(limit + 1)
        )
        if cursor is not None:
            page = page.having(
                tuple_(activity, Conversation.conversation_id)
                <= tuple_(literal(cursor.last_activity_at), literal(cursor.conversation_id))
            )
        async with self._sessions() as db:
            rows = (await db.execute(page)).all()
            threads = {row.conversation_id for row in rows[:limit]}
            attachments = await _live_attachments(db, threads)
            counts = await _item_counts(db, threads)
            live = await _live_sessions(db, threads)
            ended = await _last_ended_sessions(db, threads - live.keys())
        return ConversationPage(
            conversations=[
                ConversationSummary(
                    conversation_id=row.conversation_id,
                    agent_id=row.agent_id,
                    access_profile_id=row.access_profile_id,
                    harness_kind=row.harness_kind,
                    created_at=row.created_at,
                    last_activity_at=row.last_activity_at,
                    attachments=attachments[row.conversation_id],
                    live_session=live.get(row.conversation_id),
                    last_session_status=ended.get(row.conversation_id),
                    item_count=counts[row.conversation_id],
                )
                for row in rows[:limit]
            ],
            next_cursor=(
                ConversationCursor(
                    last_activity_at=rows[limit].last_activity_at, conversation_id=rows[limit].conversation_id
                )
                if len(rows) > limit
                else None
            ),
        )

    async def get_operator_conversation(self, operator_id: UUID, conversation_id: UUID) -> ConversationView:
        """Read one Operator-owned conversation: its channels, its items, and its sessions.

        The items are the conversation's, across replaced sessions — the same stream the MCP
        read pages, plus the lifecycle members only this surface carries. The session block is
        the current session's — the one holding the conversation, or the last one to have held it;
        earlier sessions stay reachable rather than disappearing with the sandbox they ran in.

        Not the raw frame log — the narration is the one projection of it this surface carries,
        because for a session that died before the CLI produced anything it is the whole account.
        """
        async with self._sessions() as db:
            conversation = await db.scalar(
                select(Conversation).where(
                    Conversation.conversation_id == conversation_id, Conversation.operator_id == operator_id
                )
            )
            if conversation is None:
                raise KeyError(conversation_id)
            sessions = (
                await db.scalars(
                    select(Session)
                    .where(Session.conversation_id == conversation_id)
                    .order_by(Session.created_at.desc(), Session.session_id.desc())
                )
            ).all()
            attachments = (await _live_attachments(db, {conversation_id}))[conversation_id]
            if not sessions:
                # A conversation is only ever created alongside its first session, so this is a writer
                # that committed one without the other rather than a thread waiting to start.
                raise ValueError(f"a conversation has no sessions: {conversation_id=}")
            current, *earlier = sessions
            responding = await _open_turn(db, conversation_id) is not None
            narration = await setup_narration(db, current.session_id)
            rows = await _item_page_rows(db, conversation_id, after_seq=None, limit=None)
        return ConversationView(
            conversation_id=conversation_id,
            agent_id=conversation.agent_id,
            access_profile_id=conversation.access_profile_id,
            harness_kind=conversation.harness_kind,
            created_at=conversation.created_at,
            attachments=attachments,
            items=[item_of(row) for row in rows],
            session=session_view(current, responding=responding),
            narration=narration,
            earlier_sessions=[session_view(row, responding=False) for row in earlier],
        )

    async def read_operator_conversation_changes(
        self, operator_id: UUID, conversation_id: UUID, *, after: int, limit: int
    ) -> ConversationUpdate:
        """What this Operator-owned conversation has changed to since *after*.

        Addressed by the thread rather than by the session running it, so a position survives a
        session being replaced: reading only the live session's rows would skip whatever its
        predecessor wrote after the follower's position, and a session lives only as long as the
        sandbox it holds.

        **`event_seq` is the address; whole rows are the payload.** The rows an update carries are
        exactly the items the log's events after *after* are about — every change to an item is an
        event naming it, an open item's growing prose included — each re-sent whole in its current
        state, so the follower's merge replaces by position and delivery needs neither order nor
        exactly-once.

        **The position is read before the rows**, so a row written between the two reads is carried
        by the follower's next update rather than by neither.
        """
        async with self._sessions() as db:
            conversation = await db.scalar(
                select(Conversation).where(
                    Conversation.conversation_id == conversation_id, Conversation.operator_id == operator_id
                )
            )
            if conversation is None:
                raise KeyError(conversation_id)
            sessions = (
                await db.scalars(
                    select(Session)
                    .where(Session.conversation_id == conversation_id)
                    .order_by(Session.created_at.desc(), Session.session_id.desc())
                )
            ).all()
            if not sessions:
                # A conversation is only ever created alongside its first session, so this is a
                # writer that committed one without the other rather than a thread waiting to start.
                raise ValueError(f"a conversation has no sessions: {conversation_id=}")
            current, *earlier = sessions
            position = (await stream_head(db, conversation_id)).event_seq
            if not await _addressable(db, conversation_id, after):
                raise PositionUnusableError(f"{after=} is not a position this conversation's log can answer from")
            rows = await _touched_item_rows(db, conversation_id, after=after, limit=limit + 1)
            if len(rows) > limit:
                raise PositionUnusableError(f"more than {limit} rows have moved since {after=}")
            narration = await setup_narration(db, current.session_id)
            attachments = (await _live_attachments(db, {conversation_id}))[conversation_id]
            responding = await _open_turn(db, conversation_id) is not None
        return ConversationUpdate(
            position=position,
            session=session_view(current, responding=responding),
            narration=narration,
            attachments=attachments,
            earlier_sessions=[session_view(row, responding=False) for row in earlier],
            items=[item_of(row) for row in rows],
        )

    async def authenticate_bridge(self, session_id: UUID, token: str) -> BridgeAuthentication:
        """Admit a runner to its session — the first time, and every time after.

        **Taking the lease is the admission.** A live session admits any runner that can take its
        lease, and the lease is what stops two replicas adopting one CLI: whoever writes it under
        this row lock has it, for as long as it keeps renewing.

        **A lease changing hands is recorded** in this transaction, because it crosses no wire —
        nothing in the frame log can say it happened.
        """
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            record = await db.get(Session, session_id, with_for_update=True)
            if (
                record is None
                or record.bridge_token_fingerprint is None
                or not secrets.compare_digest(record.bridge_token_fingerprint, self._fingerprint(token))
            ):
                return BridgeAuthentication.REJECTED
            if record.status in ENDED_SESSION_STATUSES:
                return BridgeAuthentication.TERMINAL
            # `provisioning` *is* "allocated and never attached", so the stamp below is the whole
            # transition to `ready`.
            first_attach = record.status == SessionStatus.PROVISIONING
            if first_attach:
                record.bridge_connected_at = now
            elif (
                record.lease_holder not in (None, REPLICA)
                and record.lease_expires_at is not None
                and record.lease_expires_at > now
            ):
                # Somebody else is still serving this session and saying so. Turning this runner
                # away keeps one CLI answering to one console — but only until that lease lapses,
                # which is why it is `HELD` rather than `REJECTED`.
                return BridgeAuthentication.HELD
            previous_holder = record.lease_holder
            record.lease_holder = REPLICA
            record.lease_expires_at = now + LEASE_TTL
            record.updated_at = now
            # The first attach is the session being served rather than taken over, and a runner
            # redialling the replica that already holds it is neither.
            if not first_attach and previous_holder != REPLICA:
                writer = await log.writer_for(db, record.conversation_id, session_id=session_id, turn_id=None, now=now)
                writer.authored(conversation_event.SessionAdopted(previous_holder=previous_holder, holder=REPLICA))
            return BridgeAuthentication.ACCEPTED

    async def release_lease(self, session_id: UUID) -> None:
        """Hand a live session back for adoption, without declaring it dead.

        "This session is over" and "I am no longer holding it" are different, and only the second
        is true during a roll. Expiring the lease says the second: the session is unowned as of
        now, and `expire_stale_leases` gives it the same `ADOPTION_GRACE` it gives a lease nobody
        released. A courtesy, not the mechanism — a SIGKILL runs no finalizer, so the sweep must be
        correct without it.
        """
        async with self._sessions.begin() as db:
            chat = await db.get(Session, session_id, with_for_update=True)
            if chat is not None and chat.status in LEASED_SESSION_STATUSES:
                chat.lease_holder = None
                chat.lease_expires_at = datetime.now(UTC)
                chat.updated_at = datetime.now(UTC)

    async def release_held_leases(self) -> int:
        """Hand back every live session this replica holds, and report how many.

        The graceful-shutdown counterpart to `release_lease`: called once from the lifespan on the
        way down, so a rolling replica's sessions become adoptable at once rather than each waiting
        out `ADOPTION_GRACE`. One statement keyed on this replica, so it runs safely beside the
        per-connection releases and does not depend on every cancelled `handle_journal_runner`
        completing its own commit.
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

        An allocated session's rendezvous fingerprint is deliberately left alone: it verifies a
        bearer that was never stored, and a cleaned-up session cannot be admitted anyway. Keeping it
        lets a redialling runner with the right bearer receive the truthful `TERMINAL` answer.
        """
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            chat = await db.get(Session, session_id, with_for_update=True)
            if chat is None:
                return
            chat.claim_cleaned_at = now
            if chat.status == SessionStatus.CLOSING:
                await self._end_session(db, chat, error=None, now=now)
                await notify_update(db, session_id=session_id, conversation_id=chat.conversation_id)
            else:
                chat.updated_at = now

    async def enqueue_prompt(
        self,
        operator_id: UUID,
        session_id: UUID,
        prompt_text: str,
        origin: PromptOrigin,
        records: PromptRecords | None = None,
    ) -> UUID:
        """Accept a prompt, recording which surface it arrived through. Returns its item.

        The prompt is an item like any other — opened, spoken and closed in one breath, because its
        whole text is known when it is accepted — and it takes the authored provenance arm, since
        admission happens before anything crosses a wire.

        The origin rides on the item's opening event, because that is the prompt's provider-neutral
        place in the stream, and a surface deciding whether it has already shown this prompt reads
        the stream. Required, with no default: a default of the console's own surface would mean a
        channel that forgot to pass one recorded the operator as having typed it into a browser, and
        the reader this exists for would post that prompt into every room including the one it came
        from. Silent, and in the one direction that matters, so the type system holds it instead.
        """
        async with self._sessions() as db:
            chat = await db.scalar(
                select(Session).where(Session.session_id == session_id, Session.operator_id == operator_id)
            )
        if chat is None:
            raise KeyError(session_id)
        return await self._enqueue_conversation_prompt(
            operator_id, chat.conversation_id, prompt_text, origin, records, required_session_id=session_id
        )

    async def enqueue_conversation_prompt(
        self,
        operator_id: UUID,
        conversation_id: UUID,
        prompt_text: str,
        origin: PromptOrigin,
        records: PromptRecords | None = None,
    ) -> UUID:
        """Accept a prompt for a conversation, whether or not it has a session yet."""
        return await self._enqueue_conversation_prompt(
            operator_id, conversation_id, prompt_text, origin, records, required_session_id=None
        )

    async def _enqueue_conversation_prompt(
        self,
        operator_id: UUID,
        conversation_id: UUID,
        prompt_text: str,
        origin: PromptOrigin,
        records: PromptRecords | None,
        *,
        required_session_id: UUID | None,
    ) -> UUID:
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            conversation = await db.scalar(
                select(Conversation)
                .where(Conversation.conversation_id == conversation_id, Conversation.operator_id == operator_id)
                .with_for_update()
            )
            if conversation is None:
                raise KeyError(conversation_id)
            if required_session_id is None:
                chat = await db.scalar(
                    select(Session)
                    .where(Session.conversation_id == conversation_id, Session.status.in_(OPEN_SESSION_STATUSES))
                    .order_by(Session.created_at.desc(), Session.session_id.desc())
                    .limit(1)
                )
            else:
                chat = await db.scalar(
                    select(Session).where(
                        Session.session_id == required_session_id,
                        Session.operator_id == operator_id,
                        Session.conversation_id == conversation_id,
                    )
                )
                if chat is None:
                    raise KeyError(required_session_id)
            if chat is not None and chat.status not in {SessionStatus.IDLE, SessionStatus.READY}:
                raise PromptRefusedError(PromptRejection.SESSION_NOT_READY)
            if await _open_turn(db, conversation_id) is not None:
                raise PromptRefusedError(PromptRejection.TURN_IN_FLIGHT)
            if await _queued_prompt(db, conversation_id) is not None:
                raise PromptRefusedError(PromptRejection.PROMPT_QUEUED)
            writer = await log.writer_for(
                db, conversation_id, session_id=chat.session_id if chat is not None else None, turn_id=None, now=now
            )
            item_id = await writer.authored_prompt(prompt_text, origin)
            db.add(
                ConversationPrompt(prompt_id=uuid4(), conversation_id=conversation_id, item_id=item_id, queued_at=now)
            )
            if chat is not None:
                chat.updated_at = now
            if records is not None:
                await records(db, item_id)
            # A neutral supervisor consumes conversation demand; once a session exists, the
            # session channel's prompt wake reaches its runner and the allocator directly.
            await notify_conversation(db, ConversationWakeKind.RUNTIME_DEMAND, conversation_id)
            if chat is not None:
                await notify(db, SessionEventKind.PROMPT, chat.session_id)
                await notify_update(db, session_id=chat.session_id, conversation_id=conversation_id)
        return item_id

    async def submit_prompt(
        self,
        operator_id: UUID,
        conversation_id: UUID,
        prompt_text: str,
        origin: PromptOrigin,
        records: PromptRecords | None = None,
    ) -> UUID:
        """Accept a prompt into the durable inbox for the neutral-operation generation (#4667).

        A prompt is a durable command first and a transcript item only when the runner admits it:
        this writes the `submitted_prompt` row that is authoritative for text and origin, and the
        journal consumer materialises the item on `prompt.admitted`. Returns the `prompt_id` the
        runner will echo back verbatim.

        No busy/queued refusals: the runner accepts prompts while working and orders admission
        itself (#4667 § Prompt ordering), so the inbox deliberately queues where the v3 path
        refused. Demand and the runner's dispatch are woken the same way the v3 queue was.
        """
        return await self._submit_prompt(operator_id, conversation_id, prompt_text, origin, records, exclusive=False)

    async def submit_exclusive_prompt(
        self,
        operator_id: UUID,
        conversation_id: UUID,
        prompt_text: str,
        origin: PromptOrigin,
        records: PromptRecords | None = None,
    ) -> UUID:
        """Accept a prompt into the inbox only when nothing is ahead of it — the refusing variant.

        Admission policy is the surface's, not the inbox's (<prompt_inbox.py>): the Matrix channel
        promises a batch that arrives mid-turn or behind a pending prompt is refused, not held
        (<../channels/matrix/SPEC.md> § Batching and admission), so it submits through this. Decided
        under the conversation row lock, in the accepting transaction, with the v3 queue's reasons.
        """
        return await self._submit_prompt(operator_id, conversation_id, prompt_text, origin, records, exclusive=True)

    async def _submit_prompt(
        self,
        operator_id: UUID,
        conversation_id: UUID,
        prompt_text: str,
        origin: PromptOrigin,
        records: PromptRecords | None,
        *,
        exclusive: bool,
    ) -> UUID:
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            conversation = await db.scalar(
                select(Conversation)
                .where(Conversation.conversation_id == conversation_id, Conversation.operator_id == operator_id)
                .with_for_update()
            )
            if conversation is None:
                raise KeyError(conversation_id)
            chat = await db.scalar(
                select(Session)
                .where(Session.conversation_id == conversation_id, Session.status.in_(OPEN_SESSION_STATUSES))
                .order_by(Session.created_at.desc(), Session.session_id.desc())
                .limit(1)
            )
            if exclusive:
                if chat is not None and chat.status not in {SessionStatus.IDLE, SessionStatus.READY}:
                    raise PromptRefusedError(PromptRejection.SESSION_NOT_READY)
                if await _open_turn(db, conversation_id) is not None:
                    raise PromptRefusedError(PromptRejection.TURN_IN_FLIGHT)
                if await _has_pending_prompt(db, conversation_id):
                    raise PromptRefusedError(PromptRejection.PROMPT_QUEUED)
            row = await prompt_inbox.submit(
                db, conversation_id=conversation_id, text=prompt_text, origin=origin, now=now
            )
            if records is not None:
                await records(db, row.prompt_id)
            # RUNTIME_DEMAND provisions a session for a conversation that has none; PROMPT wakes the
            # runner bridge of a live one to dispatch what it now owes.
            await notify_conversation(db, ConversationWakeKind.RUNTIME_DEMAND, conversation_id)
            if chat is not None:
                await notify(db, SessionEventKind.PROMPT, chat.session_id)
        return row.prompt_id

    async def pending_dispatch(self, session_id: UUID) -> tuple[DispatchablePrompt, ...]:
        """The inbox prompts this session's runner is owed, oldest first (#4667 dispatch).

        Keyed by the session's conversation, so a replacement session dispatches what its
        predecessor never got admitted, exactly as the durable inbox intends.
        """
        async with self._sessions() as db:
            conversation_id = await db.scalar(select(Session.conversation_id).where(Session.session_id == session_id))
            if conversation_id is None:
                raise KeyError(session_id)
            return tuple(
                DispatchablePrompt(prompt_id=row.prompt_id, text=row.text)
                for row in await prompt_inbox.pending(db, conversation_id)
            )

    async def withdraw_prompt(self, operator_id: UUID, conversation_id: UUID, prompt_id: UUID) -> None:
        """Take a pending inbox prompt back, if this Operator owns its conversation.

        The withdraw half of the inbox state machine, preserved from the v3 unqueue: whichever of
        withdrawal and admission stamps the row first wins, and the loser is told the state it lost
        to (`prompt_inbox.PromptNotPendingError`).
        """
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            owned = await db.scalar(
                select(Conversation.conversation_id).where(
                    Conversation.conversation_id == conversation_id, Conversation.operator_id == operator_id
                )
            )
            if owned is None:
                raise KeyError(conversation_id)
            row = await prompt_inbox.withdraw(db, prompt_id, now=now)
            if row.conversation_id != conversation_id:
                raise KeyError(prompt_id)

    async def next_prompt(self, session_id: UUID) -> TurnStart | None:
        """Take the queued prompt and open the turn that will answer it, or None if there is none.

        Dequeue and open are one transaction: splitting them would leave a window in which the
        prompt is claimed with no turn to name it, which is what admission and abort ask about.

        **Opening the turn anchors the projection cursor**, in that same transaction: everything
        recorded so far has been projected, because the previous turn's own frames were and the
        handshake frames between turns project to nothing. So the turn begins with a cursor it can
        be resumed from rather than one inherited from whatever last wrote it.

        With the v3 queue empty, the oldest pending inbox prompt is admitted here instead
        (`_admit_from_inbox`): this call is the native turn loop's admission fence, as the runner's
        `prompt.admitted` is the journal loop's (#4667).
        """
        async with self._sessions.begin() as db:
            chat = await db.get(Session, session_id, with_for_update=True)
            if chat is None or chat.status in ENDED_SESSION_STATUSES:
                return None
            now = datetime.now(UTC)
            queued = await _queued_prompt(db, chat.conversation_id, lock=True)
            if queued is None:
                queued = await _admit_from_inbox(db, chat.conversation_id, session_id=session_id, now=now)
            if queued is None:
                return None
            item = await db.get(ConversationItem, queued.item_id)
            if item is None:
                # The item the queue points at is gone, so there is no prompt to run and no text to
                # run it with. Claiming it anyway is what stops the session retrying a prompt it
                # can never read.
                logger.error("prompt %s has no item row", queued.prompt_id)
                queued.claimed_at = now
                queued.claimed_by_session_id = session_id
                return None
            # The bracket's lower bound, taken before the prompt reaches the CLI so every frame
            # the exchange produces falls inside it.
            highest = await db.scalar(
                select(func.max(SessionFrame.frame_seq)).where(SessionFrame.session_id == session_id)
            )
            chat.projected_frame_seq = highest or 0
            turn, writer = await log.open_turn(db, chat.conversation_id, session_id=session_id, now=now)
            turn.first_frame_seq = (highest or 0) + 1
            queued.claimed_at = now
            queued.claimed_by_session_id = session_id
            # Many prompts may name one turn — mid-turn steering, said once — which is what this
            # replaces the join table with.
            queued.turn_id = turn.turn_id
            item.session_id = session_id
            item.turn_id = turn.turn_id
            item.updated_at = now
            chat.updated_at = now
            await notify_update(
                db,
                session_id=session_id,
                conversation_id=chat.conversation_id,
                position=writer.conversation.next_event_seq - 1,
            )
            return TurnStart(turn_id=turn.turn_id, item_id=item.item_id, prompt=item.item_text)

    async def open_wake_turn(self, session_id: UUID, description: str, *, first_frame_seq: int) -> WakeTurn | None:
        """Open the turn bracketing an exchange the harness began itself, or None if the session ended.

        *first_frame_seq* is the exchange's first content frame, already received and recorded, so
        the cursor anchors just before it: an adopting replica replays the exchange from its own
        first frame, and the idle frames before it — which project to nothing — stay outside the
        bracket.

        The wake is admitted as a prompt item in the harness's voice (`HARNESS_ORIGIN`), so the
        transcript says what woke the session where the answer would otherwise follow nothing.
        """
        async with self._sessions.begin() as db:
            chat = await db.get(Session, session_id, with_for_update=True)
            if chat is None or chat.status in ENDED_SESSION_STATUSES:
                return None
            now = datetime.now(UTC)
            chat.projected_frame_seq = first_frame_seq - 1
            turn, writer = await log.open_turn(db, chat.conversation_id, session_id=session_id, now=now)
            turn.first_frame_seq = first_frame_seq
            item = await db.get(ConversationItem, await writer.authored_prompt(description, HARNESS_ORIGIN))
            assert item is not None
            item.turn_id = turn.turn_id
            chat.updated_at = now
            await notify_update(
                db,
                session_id=session_id,
                conversation_id=chat.conversation_id,
                position=writer.conversation.next_event_seq - 1,
            )
            return WakeTurn(turn_id=turn.turn_id)

    async def turn_state(self, turn_id: UUID) -> TurnState:
        """How far *turn_id* has got, read off its row.

        Where a turn's progress is read off its row, so a turn this process opened a moment ago and
        one a departed replica left half answered are the same question.
        """
        async with self._sessions() as db:
            if await db.get(ConversationTurn, turn_id) is None:
                raise KeyError(turn_id)
            return await _turn_state(db, turn_id)

    async def end_turn(
        self,
        turn_id: UUID,
        ended: conversation_event.TurnEnd,
        *,
        last_frame_seq: int | None = None,
        projected_frame_seq: int | None = None,
    ) -> None:
        """Close *turn_id* at the frame it ended on.

        *last_frame_seq* is the turn's own last frame, and only the runtime adapter knows which
        native frame produced its terminal effect. A provider may emit session-level bookkeeping
        immediately after that frame, so a bound re-derived here from the head of the log can land
        beyond the turn. A turn that ended on no frame at all — a failure, or an abort before a
        terminal effect arrived — passes none, and the bound is then the last frame recorded since
        the turn opened, which
        `uq_session_turns_open` keeps inside this turn because no second turn can have opened. NULL
        still means a turn that recorded nothing.

        *projected_frame_seq* is the frame that ended the turn, and this is the transaction that
        takes the cursor past it — the turn's last word is written before the close, so advancing
        in `apply_frame` for that frame would move the cursor ahead of writes still to come. A turn
        ending any other way passes none and leaves the cursor where it is; the next `next_prompt`
        re-anchors it.

        **Every close writes `turn_ended`**, not only an abort: the exchange's end is a fact a
        channel folding the stream needs whatever the outcome was, and an abort is one of the three
        outcomes rather than an event of its own — which is where every backend protocol puts it.
        The lock and the early return make it exactly once.

        Idempotent on an already-closed turn: the first outcome is the one that happened.
        """
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            turn = await db.get(ConversationTurn, turn_id, with_for_update=True)
            if turn is None or turn.ended_at is not None:
                return
            writer = await log.writer_for(
                db, turn.conversation_id, session_id=turn.session_id, turn_id=turn_id, now=now
            )
            # Read before the turn is touched. `ck_conversation_turn_last_seq` ties `last_seq` to
            # `ended_at`, and a query issued between the two autoflushes the half-written row.
            bound = (
                last_frame_seq
                if last_frame_seq is not None
                else await db.scalar(
                    select(func.max(SessionFrame.frame_seq)).where(
                        SessionFrame.session_id == turn.session_id, SessionFrame.frame_seq >= turn.first_frame_seq
                    )
                )
            )
            turn.last_seq = writer.conversation.next_event_seq
            turn.last_frame_seq = bound
            turn.ended_at = now
            turn.outcome = ended.outcome
            turn.failure = ended.failure if isinstance(ended, conversation_event.TurnFailed) else None
            writer.authored(ended, turn_id=turn_id)
            chat = await db.get(Session, turn.session_id)
            if chat is not None:
                # `responding` is derived from this turn being open, so closing it retires the
                # state and the SPA has to be told.
                _advance_cursor(chat, projected_frame_seq)
                chat.updated_at = now
                await notify_update(
                    db,
                    session_id=turn.session_id,
                    conversation_id=turn.conversation_id,
                    position=writer.conversation.next_event_seq - 1,
                )

    async def list_turns(
        self, session_id: UUID, *, cursor: TurnCursor | None, limit: int, scope: ConversationReadScope
    ) -> list[TurnRecord]:
        """A session's exchanges from *cursor*, newest first, for the `haku_conversations` tools.

        Keyset on `(started_at, turn_id)`: two turns of one session can share a start instant, and
        a cursor naming only the timestamp would hand a tied pair out twice or step over one.
        Inclusive of the row the cursor names, which is the first row the previous page did not
        return.
        """
        query = select(ConversationTurn).where(ConversationTurn.session_id == session_id)
        if cursor is not None:
            query = query.where(
                tuple_(ConversationTurn.started_at, ConversationTurn.turn_id)
                <= tuple_(literal(cursor.started_at), literal(cursor.turn_id))
            )
        async with self._sessions() as db:
            await _require_readable_session(db, session_id, scope)
            rows = (
                await db.scalars(
                    query.order_by(ConversationTurn.started_at.desc(), ConversationTurn.turn_id.desc()).limit(limit)
                )
            ).all()
        return [
            TurnRecord(
                turn_id=row.turn_id,
                first_frame_seq=row.first_frame_seq,
                last_frame_seq=row.last_frame_seq,
                started_at=row.started_at,
                ended_at=row.ended_at,
                end=turn_end_of(row),
            )
            for row in rows
        ]

    async def record_frame(
        self,
        session_id: UUID,
        direction: FrameDirection,
        kind: BridgeFrameKind,
        payload: dict[str, Any],
        *,
        runner_seq: int | None = None,
    ) -> RecordedFrame:
        """Append one frame to the session's rollout, unless this session already has it.

        `fresh` says whether the caller should act on the frame; `frame_seq` is the row's sequence
        either way, which is what a projection built from this frame points back at. **False means
        a replay** — the same runner position already exists in this log — and the caller must not
        act on it again. Console-authored records have no runner position and are always appended.

        *kind* is passed rather than read out of the payload: it is the bridge record class, while
        the native harness discriminator stays inside the opaque payload.

        *runner_seq* is the runner's own number for the frame, where one came from a runner that
        numbers. Nothing here orders by it; what reads it is `highest_runner_seq`. Default None
        because most writers have no such number to give — this console's writes to the CLI, and
        the rows it authors itself.

        Failures are not swallowed — a rollout with quiet holes looks complete while being wrong.
        """
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            return await self._record_frame(db, session_id, direction, kind, payload, runner_seq=runner_seq, now=now)

    @staticmethod
    async def _record_frame(
        db: AsyncSession,
        session_id: UUID,
        direction: FrameDirection,
        kind: BridgeFrameKind,
        payload: dict[str, Any],
        *,
        runner_seq: int | None,
        now: datetime,
    ) -> RecordedFrame:
        """The transactional half of `record_frame`, shared by narration's compatibility write."""
        # Runner positions are the identity for every native frame, including deltas and
        # JSON-RPC notifications. Payload identity is intentionally not consulted: two identical
        # native messages may be real events, while a replayed opaque frame has the same position.
        insert = (
            pg_insert(SessionFrame)
            .values(
                session_id=session_id,
                direction=direction,
                kind=kind,
                payload=payload,
                runner_seq=runner_seq,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=["session_id", "runner_seq"], index_where=text("runner_seq IS NOT NULL")
            )
        )
        inserted = await db.execute(insert.returning(SessionFrame.frame_seq))
        if (inserted_seq := inserted.scalar_one_or_none()) is not None:
            return RecordedFrame(fresh=True, frame_seq=int(inserted_seq))
        # Nothing was inserted, so the position already exists. Read back the row it collided with
        # so a replay still names the original log position.
        existing_seq = await db.scalar(
            select(SessionFrame.frame_seq).where(
                SessionFrame.session_id == session_id, SessionFrame.runner_seq == runner_seq
            )
        )
        if existing_seq is None:
            raise RuntimeError(f"replayed frame disappeared from the rollout for {runner_seq=}")
        return RecordedFrame(fresh=False, frame_seq=int(existing_seq))

    async def narrate(self, session_id: UUID, text: str) -> None:
        """Record one thing the sandbox said while coming up, and wake whoever is watching.

        The conversation event is the fact. The `setup_output` frame is a temporary compatibility
        copy for a replica whose session read still gets narration from the frame log; both commit
        together, so neither reader can observe a half-written line during the rollout. Once every
        reader uses `setup_narration`, the frame write goes away.
        """
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            chat = await db.get(Session, session_id)
            if chat is None:
                raise KeyError(session_id)
            await self._record_frame(
                db,
                session_id,
                FrameDirection.FROM_AGENT,
                SETUP_OUTPUT_KIND,
                setup_output_frame(text),
                runner_seq=None,
                now=now,
            )
            writer = await log.writer_for(db, chat.conversation_id, session_id=session_id, turn_id=None, now=now)
            writer.authored(conversation_event.SetupNarration(text=text))
            await notify_update(
                db,
                session_id=session_id,
                conversation_id=chat.conversation_id,
                position=writer.conversation.next_event_seq - 1,
            )

    async def highest_runner_seq(self, session_id: UUID) -> int | None:
        """The resume cursor for one session: the highest number a runner gave a frame in it.

        **Per session, not per connection**: two consoles can be adopting one runner's window
        during a roll, and both compute this from the same rows, so they agree on what has been
        recorded. None is a session whose log holds nothing a runner numbered, and the runner
        reading it replays its whole window.

        It is a **floor**, and the runner treats it as one (`SessionPump.seed`). Console-authored
        `setup_output` rows do not participate in this native-frame cursor, so holes in the
        recorded runner numbers are expected — a gap is not evidence of loss, and nothing yet
        checks for one.
        """
        async with self._sessions() as db:
            return await db.scalar(
                select(func.max(SessionFrame.runner_seq)).where(SessionFrame.session_id == session_id)
            )

    async def list_sessions(
        self, *, cursor: SessionCursor | None, limit: int, scope: ConversationReadScope
    ) -> list[SessionRecord]:
        """Past sessions from *cursor*, newest first, for the `haku_conversations` read tools.

        Keyset paging on `(created_at, session_id)`: an offset counts from the top of an order that
        grows at the top while a reader walks it, so every session created mid-walk would push a
        row across a page boundary. `session_id` is in the key because `created_at` alone is not a
        total order.

        Cross-room and cross-session on purpose — the fence is *scope*, the caller's profile-DAG
        read closure, applied inside the query so a page stays a page. Inclusive of the row the
        cursor names, which is the first row the previous page did not return.
        """
        query = (
            select(Session, Conversation.agent_id, Conversation.access_profile_id, Conversation.harness_kind)
            .join(Conversation, Conversation.conversation_id == Session.conversation_id)
            .order_by(Session.created_at.desc(), Session.session_id.desc())
        )
        if isinstance(scope, ProfileScopedReads):
            query = query.where(Conversation.access_profile_id.in_(sorted(scope.readable_profile_ids)))
        if cursor is not None:
            query = query.where(
                tuple_(Session.created_at, Session.session_id)
                <= tuple_(literal(cursor.created_at), literal(cursor.session_id))
            )
        async with self._sessions() as db:
            sessions = (await db.execute(query.limit(limit))).all()
            attachments = await _live_attachments(db, {row.conversation_id for row, *_ in sessions})
        return [
            SessionRecord(
                session_id=row.session_id,
                conversation_id=row.conversation_id,
                agent_id=agent_id,
                access_profile_id=access_profile_id,
                harness_kind=harness_kind,
                attachments=attachments[row.conversation_id],
                status=row.status,
                created_at=row.created_at,
                error=row.error,
            )
            for row, agent_id, access_profile_id, harness_kind in sessions
        ]

    async def read_session_frames(
        self,
        session_id: UUID,
        *,
        cursor: int | None,
        limit: int,
        scope: ConversationReadScope,
        kinds: Sequence[BridgeFrameKind] | None = None,
    ) -> list[FrameRecord]:
        """One page of a session's frame log, in wire order, from the start of the log onwards.

        Keyset paging on `frame_seq` rather than an offset: the log is append-only, so a cursor
        cannot skip or repeat a row the way an offset would once new frames land between pages.
        The cursor names the first frame to return rather than the last one already returned, so
        an item's `first_frame_seq` is a cursor as it stands.
        """
        query = _bridge_frames(select(SessionFrame).where(SessionFrame.session_id == session_id), kinds)
        if cursor is not None:
            query = query.where(SessionFrame.frame_seq >= cursor)
        async with self._sessions() as db:
            await _require_readable_session(db, session_id, scope)
            rows = (await db.scalars(query.order_by(SessionFrame.frame_seq).limit(limit))).all()
        return [_frame_record(row) for row in rows]

    async def read_item_rows(
        self, conversation_id: UUID, *, after_seq: int | None, limit: int | None, scope: ConversationReadScope
    ) -> list[ConversationPageRow]:
        """One page of the conversation's item rows, oldest first, each in its current state.

        **The materialised rows, not a fold of the log.** `conversation_item` is a fold the writer
        keeps, so reading it cannot disagree with the log it was folded from — one keyset on
        `opened_seq`, plus one grouped lookup of each row's `conversation_event` frame span for its
        provenance. A conversation's length buys its reader nothing to refold, so page N of a long
        thread costs what page one does. The price is the fold's price still: a projection fix
        does not reach a conversation that already happened.

        Faithfully every row, whatever its lifecycle: an item still open is served with its text
        as of the read, and a later read serves the same position settled — `opened_seq` never
        moves, so the page boundaries hold while the content is still arriving.
        Conversation-keyed on purpose: a session is one runner's life and the thread outlives it,
        so the read that follows one thread must not stop where a sandbox died. What the rows fold
        to on the wire is the reader's business (`item_reads.item_of`), not this store's.
        *limit* may be None because the browser's snapshot is the conversation whole; the paging
        surfaces always bound it.
        """
        async with self._sessions() as db:
            await _require_readable_conversation(db, conversation_id, scope)
            return await _item_page_rows(db, conversation_id, after_seq=after_seq, limit=limit)

    async def read_operator_frames(
        self,
        operator_id: UUID,
        session_id: UUID,
        *,
        before_seq: int | None,
        limit: int,
        kinds: Sequence[BridgeFrameKind] | None = None,
    ) -> SessionFramePage:
        """The tail of an Operator-owned session's rollout, for the console's frame inspector.

        Two things differ from `read_session_frames`, which serves the MCP reader. It is scoped, because a
        browser surface must never read another Operator's session. And its keyset runs backwards:
        the frames an operator opens this for are a session's *last* ones — an answer that was cut
        off, a turn that died — so paging forward from frame one to reach them punishes a long
        session. The rows still come back in wire order; only which page is the first one differs.

        A message's inclusive `frame_seq` range is a bound on this same query and `before_seq` is
        already its upper half, so per-message provenance is a filter over this view rather than a
        second read path.
        """
        before = before_seq
        async with self._sessions() as db:
            owned = (
                await db.execute(
                    select(Session, Conversation.harness_kind)
                    .join(Conversation, Conversation.conversation_id == Session.conversation_id)
                    .where(Session.session_id == session_id, Session.operator_id == operator_id)
                )
            ).one_or_none()
            if owned is None:
                raise KeyError(session_id)
            harness_kind = owned[1]
            query = _bridge_frames(select(SessionFrame).where(SessionFrame.session_id == session_id), kinds)
            if before is not None:
                query = query.where(SessionFrame.frame_seq < before)
            rows = (await db.scalars(query.order_by(SessionFrame.frame_seq.desc()).limit(limit))).all()
        session, harness_kind = owned
        return frame_page(
            list(reversed(rows)), limit=limit, conversation_id=session.conversation_id, harness_kind=harness_kind
        )

    async def apply_frame(
        self, session_id: UUID, turn_id: UUID, frame_seq: int, events: Sequence[ConversationEvent]
    ) -> None:
        """Append what one frame's events say, and move the cursor past that frame, together.

        **One transaction, and the cursor is inside it.** The log rows, the items they materialise
        and `sessions.projected_frame_seq` commit or do not commit as one, which is what makes those
        effects exactly-once: a process that dies anywhere leaves the cursor naming the last frame
        whose effects are durable, so whoever adopts the session redoes exactly the frames whose
        effects did not commit.

        **Nothing is queued for a channel here.** The turn writes the log and stops; a channel reads
        forward from its own cursor and decides what it owes. That is the seam this method used to
        cross — it wrote the room's outbox row inside the turn's transaction, which tied the
        conversation's writer to one channel's address.

        **A frame that ends the turn does not come here.** Closing the turn is `end_turn`'s
        transaction, so advancing the cursor here for that frame would put it ahead of writes still
        to come; `end_turn` takes it instead.
        """
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            turn = await db.get(ConversationTurn, turn_id, with_for_update=True)
            if turn is None:
                raise KeyError(turn_id)
            chat = await db.get(Session, session_id)
            if chat is None:
                raise KeyError(session_id)
            writer = await log.writer_for(db, turn.conversation_id, session_id=session_id, turn_id=turn_id, now=now)
            for event in events:
                await writer.append(event)
            _advance_cursor(chat, frame_seq)
            chat.updated_at = now
            await notify_update(
                db,
                session_id=session_id,
                conversation_id=turn.conversation_id,
                position=writer.conversation.next_event_seq - 1,
            )

    async def complete_frame(
        self,
        session_id: UUID,
        turn_id: UUID,
        frame_seq: int,
        events: Sequence[ConversationEvent],
        *,
        ended: conversation_event.TurnEnd,
        final_text: str,
    ) -> bool:
        """Commit a terminal frame's neutral effects, answer close and turn close together.

        A provider may put durable conversation facts on the same native frame that ends a turn.
        Dropping those effects would make the live write differ from replay; committing them first
        in a separate transaction would let the projection cursor outrun the turn's close. This is
        therefore the terminal counterpart of :meth:`apply_frame`: every event, any neutral
        fallback message, the turn outcome and the cursor advance commit or roll back as one.

        ``final_text`` is only a fallback for a provider whose terminal frame is the first place
        its prose appears. If the effects already completed a message, or left one open after
        streaming segments, it is not repeated.
        """
        now = datetime.now(UTC)
        provenance = FrameRange(first_frame_seq=frame_seq, last_frame_seq=frame_seq)
        async with self._sessions.begin() as db:
            turn = await db.get(ConversationTurn, turn_id, with_for_update=True)
            if turn is None:
                raise KeyError(turn_id)
            if turn.ended_at is not None:
                return (await _turn_state(db, turn_id)).said_anything
            chat = await db.get(Session, session_id)
            if chat is None:
                raise KeyError(session_id)
            writer = await log.writer_for(db, turn.conversation_id, session_id=session_id, turn_id=turn_id, now=now)
            for event in events:
                await writer.append(event)

            state = await _turn_state(db, turn_id)
            if state.streaming is None and state.said_anything:
                said = True
            elif state.streaming is not None:
                await writer.append(MessageCompleted(backend_item_id=None, provenance=provenance))
                said = bool(state.streaming.strip())
            elif final_text:
                await writer.append(MessageStarted(provenance=provenance))
                await writer.append(
                    ItemSegment(item=OpenRef(item_type=ItemType.MESSAGE), text=final_text, provenance=provenance)
                )
                await writer.append(MessageCompleted(backend_item_id=None, provenance=provenance))
                said = True
            else:
                said = False

            turn.last_seq = writer.conversation.next_event_seq
            turn.last_frame_seq = frame_seq
            turn.ended_at = now
            turn.outcome = ended.outcome
            turn.failure = ended.failure if isinstance(ended, conversation_event.TurnFailed) else None
            writer.authored(ended, turn_id=turn_id)
            _advance_cursor(chat, frame_seq)
            chat.updated_at = now
            await notify_update(
                db,
                session_id=session_id,
                conversation_id=turn.conversation_id,
                position=writer.conversation.next_event_seq - 1,
            )
            return said

    async def close_answer(self, session_id: UUID, turn_id: UUID, *, final_text: str, frame_seq: int) -> bool:
        """Bring the turn's answer to a close, and say whether the turn said anything at all.

        Three cases and one rule between them, which is why they are one method rather than three
        calls the loop chooses between:

        - **A message the stream left open** closes on its own segments. Nothing is appended,
          because the adapter's terminal `final_text` repeats what the stream already carried, and
          minting it again would say the answer twice.
        - **A turn that already completed a message** needs nothing: its prose is on its items and
          the terminal effect repeats the last of them.
        - **A turn whose prose arrived only with the terminal effect** — no stream, no completed
          block — gets that text as one whole message, opened and closed on that effect's frame.

        Empty prose mints nothing: a turn that only ran tools said nothing, and an empty message
        item would be an answer the room owes a copy of.
        """
        now = datetime.now(UTC)
        provenance = FrameRange(first_frame_seq=frame_seq, last_frame_seq=frame_seq)
        async with self._sessions.begin() as db:
            turn = await db.get(ConversationTurn, turn_id, with_for_update=True)
            if turn is None:
                raise KeyError(turn_id)
            state = await _turn_state(db, turn_id)
            if state.streaming is None and state.said_anything:
                return True
            writer = await log.writer_for(db, turn.conversation_id, session_id=session_id, turn_id=turn_id, now=now)
            if state.streaming is not None:
                await writer.append(MessageCompleted(backend_item_id=None, provenance=provenance))
                said = bool(state.streaming.strip())
            elif final_text:
                await writer.append(MessageStarted(provenance=provenance))
                await writer.append(
                    ItemSegment(item=OpenRef(item_type=ItemType.MESSAGE), text=final_text, provenance=provenance)
                )
                await writer.append(MessageCompleted(backend_item_id=None, provenance=provenance))
                said = True
            else:
                said = False
            await notify_update(
                db,
                session_id=session_id,
                conversation_id=turn.conversation_id,
                position=writer.conversation.next_event_seq - 1,
            )
            return said

    async def fail(self, session_id: UUID, error: str) -> None:
        # Logged as well as persisted. The column is the operator-facing record, but it is not
        # reachable from `kubectl logs`, and a Matrix session that dies leaves no other trace —
        # the room just stops answering.
        logger.error("session %s failed: %s", session_id, error)
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            chat = await db.get(Session, session_id, with_for_update=True)
            if chat is not None and chat.status not in {
                SessionStatus.CLOSING,
                SessionStatus.CLOSED,
                SessionStatus.FAILED,
            }:
                await self._end_session(db, chat, error=error, now=now)
                await notify_update(db, session_id=session_id, conversation_id=chat.conversation_id)
            # An item still open when the session died says so on its own row: `failed` is one of
            # the three lifecycle states, so a half-written answer is neither lost nor shown as if
            # it had finished.
            for item in await db.scalars(
                select(ConversationItem).where(
                    ConversationItem.session_id == session_id, ConversationItem.status == ItemStatus.OPEN
                )
            ):
                item.status = ItemStatus.FAILED
                item.closed_seq = item.opened_seq
                item.updated_at = now

    async def request_close(self, operator_id: UUID, session_id: UUID) -> None:
        """Ask this session to end, and wake whoever is running it.

        `CLOSING` is an ended status, so the turn loop stops as soon as it re-reads one — but it
        re-reads only after a wake, and is otherwise parked in a 30-second prompt timeout. Without
        the `PROMPT` notify a closing session's runner holds its sandbox for the rest of that wait.
        """
        async with self._sessions.begin() as db:
            chat = await db.scalar(
                select(Session)
                .where(Session.session_id == session_id, Session.operator_id == operator_id)
                .with_for_update()
            )
            if chat is None:
                raise KeyError(session_id)
            if chat.status in {SessionStatus.CLOSED, SessionStatus.FAILED}:
                return
            if chat.close_requested_at is None:
                chat.close_requested_at = datetime.now(UTC)
            chat.updated_at = datetime.now(UTC)
            await notify(db, SessionEventKind.PROMPT, session_id)
            await notify_update(db, session_id=session_id, conversation_id=chat.conversation_id)

    async def attached(self, session_id: UUID) -> bool:
        """Whether a channel holds a copy of the thread this session runs.

        The conversation's attachments rather than the session's own record of a room: an
        attachment outlives every session that has run under it, so a replacement answers this the
        same way the session it replaced did.
        """
        async with self._sessions() as db:
            return (
                await db.scalar(
                    select(ChannelAttachmentRow.attachment_id)
                    .join(Session, Session.conversation_id == ChannelAttachmentRow.conversation_id)
                    .where(Session.session_id == session_id, ChannelAttachmentRow.detached_at.is_(None))
                )
            ) is not None

    async def session_of(self, conversation_id: UUID) -> UUID | None:
        """The session running *conversation_id*, or None where none has been started for it.

        The newest, because only one session holds a conversation at a time and a replacement is
        started after the one it replaces ended.
        """
        async with self._sessions() as db:
            session_id: UUID | None = await db.scalar(
                select(Session.session_id)
                .where(Session.conversation_id == conversation_id)
                .order_by(Session.created_at.desc(), Session.session_id.desc())
                .limit(1)
            )
            return session_id

    async def outcome(self, session_id: UUID) -> SessionOutcome | None:
        async with self._sessions() as db:
            chat = await db.get(Session, session_id)
            return None if chat is None else SessionOutcome(status=chat.status, error=chat.error)

    async def status(self, session_id: UUID) -> SessionStatus | None:
        outcome = await self.outcome(session_id)
        return outcome.status if outcome is not None else None

    async def harness_kind_of(self, session_id: UUID) -> HarnessKind:
        """Return the immutable harness discriminator of a session's conversation."""
        async with self._sessions() as db:
            kind = await db.scalar(
                select(Conversation.harness_kind)
                .join(Session, Session.conversation_id == Conversation.conversation_id)
                .where(Session.session_id == session_id)
            )
            if kind is None:
                raise KeyError(session_id)
            return kind

    async def session_identity(self, session_id: UUID) -> OperatorSessionIdentity:
        """Look up the immutable Agent/profile/harness for internal execution paths."""
        async with self._sessions() as db:
            row = (
                await db.execute(
                    select(
                        Session.status, Conversation.agent_id, Conversation.access_profile_id, Conversation.harness_kind
                    )
                    .join(Conversation, Conversation.conversation_id == Session.conversation_id)
                    .where(Session.session_id == session_id)
                )
            ).one_or_none()
            if row is None:
                raise KeyError(session_id)
            return OperatorSessionIdentity(
                status=row.status,
                agent_id=row.agent_id,
                access_profile_id=row.access_profile_id,
                harness_kind=row.harness_kind,
            )

    async def conversation_identity(self, conversation_id: UUID, operator_id: UUID) -> OperatorSessionIdentity:
        """Read a conversation's pinned identity for replacement-session authorization."""
        async with self._sessions() as db:
            row = (
                await db.execute(
                    select(Conversation.agent_id, Conversation.access_profile_id, Conversation.harness_kind).where(
                        Conversation.conversation_id == conversation_id, Conversation.operator_id == operator_id
                    )
                )
            ).one_or_none()
            if row is None or row.agent_id is None or row.access_profile_id is None:
                raise KeyError(conversation_id)
            return OperatorSessionIdentity(
                status=SessionStatus.READY,
                agent_id=row.agent_id,
                access_profile_id=row.access_profile_id,
                harness_kind=row.harness_kind,
            )

    async def operator_session_identity(self, operator_id: UUID, session_id: UUID) -> OperatorSessionIdentity:
        """The immutable conversation identity behind one Operator-owned session."""
        async with self._sessions() as db:
            row = (
                await db.execute(
                    select(
                        Session.status, Conversation.agent_id, Conversation.access_profile_id, Conversation.harness_kind
                    )
                    .join(Conversation, Conversation.conversation_id == Session.conversation_id)
                    .where(Session.session_id == session_id, Session.operator_id == operator_id)
                )
            ).one_or_none()
            if row is None:
                raise KeyError(session_id)
            return OperatorSessionIdentity(
                status=row.status,
                agent_id=row.agent_id,
                access_profile_id=row.access_profile_id,
                harness_kind=row.harness_kind,
            )

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
        without its finalizer — SIGKILL, OOM, node loss — leaves a row claiming a turn is in flight
        that `supervise_once` reads as healthy. This is the only observer that is not that process.

        **An expired lease means unowned, not dead**, and the threshold below is that distinction.
        `authenticate_bridge` admits any runner once the lease has lapsed, so an expired session is
        adoptable without anything having to hand it back — but not *instantly* adopted, since the
        runner redials on a backoff. A session is therefore dead only once it has been adoptable
        for a whole `ADOPTION_GRACE` and nobody took it.

        Set-based and idempotent: any replica may run it, concurrent runners converge, and a merely
        slow owner renews well before the TTL.
        """
        async with self._sessions.begin() as db:
            expired = (
                await db.scalars(
                    select(Session.session_id).where(
                        Session.status.in_(LEASED_SESSION_STATUSES),
                        Session.lease_expires_at <= datetime.now(UTC) - self._adoption_grace,
                    )
                )
            ).all()
            for session_id in expired:
                # Row-at-a-time rather than one UPDATE: `notify` is per session, and a room that
                # is not told its session died simply goes quiet.
                chat = await db.get(Session, session_id, with_for_update=True)
                if chat is None or chat.status not in LEASED_SESSION_STATUSES:
                    continue
                # Read off two columns the failure below then overwrites, so it is recorded as an
                # event rather than only rendered into the error prose the operator sees.
                if chat.lease_holder is not None:
                    reason = LeaseExpiryReason.HOLDER_GONE
                elif chat.bridge_connected_at is not None:
                    reason = LeaseExpiryReason.UNADOPTED
                else:
                    reason = LeaseExpiryReason.NEVER_ATTACHED
                detail = _expiry_detail(reason, chat.lease_holder)
                logger.error("session %s lease expired: %s", session_id, detail)
                now = datetime.now(UTC)
                writer = await log.writer_for(db, chat.conversation_id, session_id=session_id, turn_id=None, now=now)
                writer.authored(conversation_event.LeaseExpired(reason=reason, last_holder=chat.lease_holder))
                await self._end_session(db, chat, error=f"console session ended: {detail}", now=now)
                await notify_update(db, session_id=session_id, conversation_id=chat.conversation_id)
            return len(expired)

    async def closed(self, session_id: UUID) -> None:
        async with self._sessions.begin() as db:
            chat = await db.get(Session, session_id, with_for_update=True)
            if chat is not None and chat.status not in {SessionStatus.CLOSED, SessionStatus.FAILED}:
                now = datetime.now(UTC)
                await self._end_session(db, chat, error=None, now=now)
                await notify_update(db, session_id=session_id, conversation_id=chat.conversation_id)

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

        Returns False when no turn is in flight. Over NOTIFY rather than an in-process registry
        because the two ends land on different replicas: the abort event belongs to the pod holding
        the runner's bridge websocket, while the operator's HTTP request is balanced across all.
        """
        async with self._sessions.begin() as db:
            chat = await db.get(Session, session_id)
            if chat is None or await _open_turn(db, chat.conversation_id) is None:
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


def _frame_record(row: SessionFrame) -> FrameRecord:
    """One stored frame as its wire variant, told apart by the row's own kind."""
    match row.kind:
        case BridgeFrameKind.HARNESS_FRAME:
            return HarnessFrameRecord(
                frame_seq=row.frame_seq, direction=row.direction, created_at=row.created_at, payload=row.payload
            )
        case BridgeFrameKind.SETUP_OUTPUT:
            text = row.payload.get("text")
            if not isinstance(text, str):
                # Console-authored, so the shape is ours; a row without its line is corruption.
                raise ValueError(f"a setup narration row carries no text: {row.frame_seq=}")
            return SetupOutputRecord(frame_seq=row.frame_seq, created_at=row.created_at, text=text)


def _advance_cursor(chat: Session, frame_seq: int | None) -> None:
    """Move the session's projection cursor to *frame_seq*, never backwards.

    Monotone because two writers can reach it out of order: `end_turn` carries the frame that ended
    the turn while the turn's last word carries none, and a retried adoption re-projects frames the
    cursor has already passed.
    """
    if frame_seq is not None and (chat.projected_frame_seq is None or chat.projected_frame_seq < frame_seq):
        chat.projected_frame_seq = frame_seq


async def _queued_prompt(db: AsyncSession, conversation_id: UUID, *, lock: bool = False) -> ConversationPrompt | None:
    """The prompt this conversation is waiting to run, if it has one.

    Keyed by the conversation, so a prompt accepted before any runner exists is still findable —
    which is what lets a thread hold demand while its sandbox is still being provisioned.

    `SKIP LOCKED` when claiming, so two replicas racing on one conversation take different rows
    rather than blocking on each other — though a partial unique index means there is at most one.
    """
    query = (
        select(ConversationPrompt)
        .where(ConversationPrompt.conversation_id == conversation_id, ConversationPrompt.claimed_at.is_(None))
        .order_by(ConversationPrompt.queued_at)
    )
    prompt: ConversationPrompt | None = await db.scalar(query.with_for_update(skip_locked=True) if lock else query)
    return prompt


async def _admit_from_inbox(
    db: AsyncSession, conversation_id: UUID, *, session_id: UUID, now: datetime
) -> ConversationPrompt | None:
    """Admit the oldest pending inbox prompt for the native turn loop, or None with none pending.

    The Console-side admission fence (#4667): under the neutral-operation generation a prompt is a
    durable `submitted_prompt` command until something admits it into the transcript, and for the
    deletion-scheduled native loop that something is this call — the journal loop's runner says
    `prompt.admitted` instead, and a session is served by exactly one of the two. Materialises the
    authored item exactly as the journal consumer does, stamps the admission, and hands back an
    unclaimed v3 queue row carrying the inbox row's age, so everything downstream — the claim, the
    requeue on adoption, the replacement re-pointing — is the v3 machinery unchanged.
    """
    oldest_pending = (
        select(SubmittedPrompt)
        .where(
            SubmittedPrompt.conversation_id == conversation_id,
            SubmittedPrompt.admitted_at.is_(None),
            SubmittedPrompt.withdrawn_at.is_(None),
        )
        .order_by(SubmittedPrompt.submitted_at, SubmittedPrompt.prompt_id)
        .limit(1)
    )
    # Unlocked emptiness check first, so the idle wait's polling never takes the conversation lock.
    if await db.scalar(oldest_pending) is None:
        return None
    # The conversation row lock (the writer's), then the queue re-check under it: a v3 enqueue that
    # raced past the caller's unlocked check has now committed its unclaimed row, and inserting a
    # second would trip `uq_conversation_prompt_unclaimed`. The caller's next pass claims it.
    writer = await log.writer_for(db, conversation_id, session_id=session_id, turn_id=None, now=now)
    if await _queued_prompt(db, conversation_id) is not None:
        return None
    row = await db.scalar(oldest_pending.with_for_update())
    if row is None:
        return None
    item_id = await writer.authored_prompt(row.text, row.origin)
    row.admitted_at = now
    row.admitted_item_id = item_id
    queued = ConversationPrompt(
        prompt_id=uuid4(), conversation_id=conversation_id, item_id=item_id, queued_at=row.submitted_at
    )
    db.add(queued)
    await db.flush([queued])
    return queued


def _pending_prompts() -> Subquery:
    """Every prompt still owed a runner, as one (conversation_id, pending_since) relation.

    The two representations a pending prompt has during stage 4 of #4667: the `submitted_prompt`
    inbox every surface submits into, and the v3 `conversation_prompt` queue — deletion-scheduled
    with the native turn loop (stage 5), and holding that loop's demand until then.
    """
    inbox = select(
        SubmittedPrompt.conversation_id.label("conversation_id"), SubmittedPrompt.submitted_at.label("pending_since")
    ).where(SubmittedPrompt.admitted_at.is_(None), SubmittedPrompt.withdrawn_at.is_(None))
    queued = select(
        ConversationPrompt.conversation_id.label("conversation_id"), ConversationPrompt.queued_at.label("pending_since")
    ).where(ConversationPrompt.claimed_at.is_(None))
    return inbox.union_all(queued).subquery("pending_prompt")


async def _has_pending_prompt(db: AsyncSession, conversation_id: UUID) -> bool:
    """Whether this conversation still owes a runner a prompt, in either representation (#4667).

    Unlocked — a work hint the reconciler repeats under the conversation row lock, exactly as it
    did for the v3 queue alone.
    """
    pending = _pending_prompts()
    return (
        await db.scalar(select(pending.c.conversation_id).where(pending.c.conversation_id == conversation_id).limit(1))
    ) is not None


async def _turn_state(db: AsyncSession, turn_id: UUID) -> TurnState:
    """How far a turn has got, derived from the items it opened rather than from columns on it."""
    streaming: str | None = await db.scalar(
        select(ConversationItem.item_text).where(
            ConversationItem.turn_id == turn_id,
            ConversationItem.item_type == ItemType.MESSAGE,
            ConversationItem.status == ItemStatus.OPEN,
        )
    )
    spoke = await db.scalar(
        select(func.count())
        .select_from(ConversationItem)
        .where(
            ConversationItem.turn_id == turn_id,
            ConversationItem.item_type == ItemType.MESSAGE,
            ConversationItem.status == ItemStatus.COMPLETE,
        )
    )
    return TurnState(streaming=streaming, said_anything=bool(spoke))


async def _addressable(db: AsyncSession, conversation_id: UUID, after: int) -> bool:
    """Whether *after* is a position this conversation's log can still be read from.

    The positions a follow hands out are 0 and this conversation's own `event_seq` values, so
    membership is the whole check: anything else names a row that has been deleted or was never
    this conversation's. It cannot be a comparison, because `event_seq` is a global `Identity` —
    one conversation's rows are not contiguous, so a number below its first row is "before this log
    begins" and a number between two of its rows is not.
    """
    if after == 0:
        return True
    found = await db.scalar(
        select(ConversationEventRow.event_seq)
        .join(Session, Session.session_id == ConversationEventRow.session_id)
        .where(Session.conversation_id == conversation_id, ConversationEventRow.event_seq == after)
    )
    return found is not None


async def _spans(db: AsyncSession, items: Sequence[ConversationItem]) -> list[ConversationPageRow]:
    """*items* with the frame span each row's events were read off, one grouped lookup for all."""
    spans = {
        item_id: (first, last)
        for item_id, first, last in (
            await db.execute(
                select(
                    ConversationEventRow.item_id,
                    func.min(ConversationEventRow.source_first_frame_seq),
                    func.max(ConversationEventRow.source_last_frame_seq),
                )
                .where(ConversationEventRow.item_id.in_([item.item_id for item in items]))
                .group_by(ConversationEventRow.item_id)
            )
        ).all()
    }
    return [
        ConversationPageRow(item=item, first_frame_seq=first, last_frame_seq=last)
        for item in items
        for first, last in [spans.get(item.item_id, (None, None))]
    ]


async def _item_page_rows(
    db: AsyncSession, conversation_id: UUID, *, after_seq: int | None, limit: int | None
) -> list[ConversationPageRow]:
    """One keyset page of the conversation's item rows in opening order, spans attached."""
    query = (
        select(ConversationItem)
        .where(
            ConversationItem.conversation_id == conversation_id,
            ConversationItem.opened_seq >= (after_seq if after_seq is not None else 0),
        )
        .order_by(ConversationItem.opened_seq)
    )
    items = (await db.scalars(query if limit is None else query.limit(limit))).all()
    return await _spans(db, items)


async def _touched_item_rows(
    db: AsyncSession, conversation_id: UUID, *, after: int, limit: int
) -> list[ConversationPageRow]:
    """The item rows the log's events after *after* are about, whole and in opening order.

    Every change to an item is an event naming it — its opening, each prose segment, its
    completion — so this is the exact "what moved" read, an open item's growing text included.
    """
    touched = (
        select(ConversationEventRow.item_id)
        .where(
            ConversationEventRow.conversation_id == conversation_id,
            ConversationEventRow.event_seq > after,
            ConversationEventRow.item_id.is_not(None),
        )
        .distinct()
    )
    items = (
        await db.scalars(
            select(ConversationItem)
            .where(ConversationItem.item_id.in_(touched))
            .order_by(ConversationItem.opened_seq)
            .limit(limit)
        )
    ).all()
    return await _spans(db, items)


async def _open_turn(db: AsyncSession, conversation_id: UUID) -> UUID | None:
    """The turn *session_id* is in the middle of, if it is in the middle of one.

    The one question behind three: whether a prompt may be accepted, whether there is anything to
    abort, and what the SPA should be told. A partial unique index makes "at most one" a schema
    property.
    """
    turn_id: UUID | None = await db.scalar(
        select(ConversationTurn.turn_id).where(
            ConversationTurn.conversation_id == conversation_id, ConversationTurn.ended_at.is_(None)
        )
    )
    return turn_id
