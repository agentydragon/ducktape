"""Keeps one live chat session bound to the one room Haku services.

The console's chat machinery is driven by an operator browser gesture: a `POST` creates a session,
mints a bridge token, and provisions a SandboxClaim; the sandbox dials back and `handle_runner`
lives for that WebSocket. Matrix has no gesture, so something has to own *"there is one session and
it has a live sandbox"* — this.

A sibling task to the sync loop, under an advisory lock of its own. The lock keeps exactly one
replica provisioning; being a separate task from `/sync` keeps a slow or stalled claim from wedging
ingress, which must keep accepting messages while no sandbox is up (R1.4). Its own lock rather than
the sync loop's, because the two need single-execution but not co-location — sharing one would mean
a supervisor stall could only be resolved by giving up ingress leadership too.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from haku.console.chat_models import (
    OPEN_SESSION_STATUSES,
    ChatMessageRole,
    ChatMessageStatus,
    ChatSurface,
    PromptRejection,
    SessionStatus,
)
from haku.console.config import ClaudeRuntimeConfig, MatrixConfig
from haku.console.database_schema import (
    ChatAttachment,
    Conversation,
    MatrixConversation,
    Session,
    SessionEvent,
    SessionMessage,
)
from haku.console.operator_identity_store import PostgresOperatorIdentityStore
from haku.console.x import session_events
from haku.console.x.channels.matrix.client import InboundMessage, RoomEventKind, UnmappableEvent
from haku.console.x.session_events import PromptRejectedBody, UnreadableInputBody
from haku.console.x.session_notifications import SessionEventKind, SessionNotifications
from haku.console.x.session_runtime import SessionService
from haku.console.x.session_store import REPLICA, MatrixSession, PromptRefusedError, SessionStore
from haku.console.x.system_prompt import HistoryMessage, SessionIntroduction, SystemPromptTemplate

logger = logging.getLogger(__name__)

# Distinct from the sync loop's lock in `sync` and the OAuth refresh lock.
_SUPERVISOR_ADVISORY_LOCK = 0x4D58_5345  # "MXSE"

# What the room hears when a turn ends with no text at all (R11.2). Phrased as an outcome the
# operator can act on rather than as an error, because a turn that only ran tools is a legitimate
# thing to have happened — it just must not look like the console lost the answer.
NOTHING_SAID = "the turn finished without saying anything"

# How this room renders a `turn_aborted` event. The words are the channel's own: what is recorded
# is that the turn was aborted, and every channel gets to say so differently.
ABORTED_BY_OPERATOR = "[aborted by operator]"

SUPERVISE_INTERVAL = datetime.timedelta(seconds=10)
# How long a replica that lost the election waits before contending again.
LEADER_RETRY = datetime.timedelta(seconds=30)
# A failed provision should not be retried as fast as a healthy poll: claim creation talks
# to Kubernetes, and a persistent failure would otherwise become a hot loop against it.
PROVISION_BACKOFF = datetime.timedelta(seconds=60)

# Emits a lifecycle line into the live room. Supplied by the sync service, which owns the
# access token and the send path — the supervisor never gets a Matrix credential of its own,
# so there is still exactly one login and one device (R10.3a).
Announce = Callable[[str], Awaitable[None]]


class RoomChannel(Protocol):
    """Everything `MatrixSurface` needs said to, or read from, the live room.

    One port rather than the separate dependencies this used to take — a history callable, an
    announce callable and a status object, all bound to the same sync service at composition. The
    callables were narrow because the *supervisor* must never hold a Matrix credential (`Announce`
    above, still one function for exactly that reason); passing several of them plus the whole
    service to one collaborator bought nothing and hid that they were one thing.

    Answers do not travel through here: they are rows the outbox drain says into the room, which
    is the only way a producer can be told that the room actually heard one.

    Implemented by the sync loop, the only holder of the credential and the only object that
    knows which room is bound. `bound_room` and `recent_history` are the two methods that ask it
    for something rather than telling it something, and the second is answered out of our own
    transcript — the channel is still who to ask, because it knows which room this is, but not
    where the answer comes from.
    """

    async def bound_room(self) -> str | None: ...

    async def recent_history(self, before_session: UUID, limit: int) -> Sequence[HistoryMessage]: ...

    async def announce(self, body: str, kind: RoomEventKind = ...) -> None: ...

    async def show_status(self, body: str, session_id: UUID | None = ...) -> None: ...

    async def set_typing(self, active: bool) -> None: ...

    async def clear_status(self) -> None: ...


# How much of the conversation a replacement session is handed. Enough to pick up a thread
# mid-topic, not enough to be a transcript — anything older is indexed, and the prompt points the
# agent at `haku_index` for it (R3.3a, [v1] "start without the summary").
#
# Counted in **recorded rows**, where it used to count room events: a batch the operator sent as
# three messages is one prompt row, so twenty here is twenty exchanges' worth rather than twenty
# events of a conversation that may be mostly one side.
RE_AWAKENING_MESSAGES = 20


class MatrixConversationStore:
    """The bound room and the session serving it."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    async def load(self, user_id: str) -> MatrixConversation | None:
        async with self._sessions() as db:
            row: MatrixConversation | None = await db.scalar(
                select(MatrixConversation).where(MatrixConversation.user_id == user_id)
            )
            return row

    async def claim_room(self, user_id: str, room_id: str) -> str:
        """Bind `room_id` if no room is bound yet; return whichever room is live.

        A caller that gets back a different room than it asked for has been refused
        (R3.6a). The insert-or-nothing is what makes that decision atomic — two replicas
        racing on the same invite cannot each conclude they bound it.
        """
        async with self._sessions() as db, db.begin():
            await db.execute(
                insert(MatrixConversation)
                .values(
                    user_id=user_id, room_id=room_id, session_id=None, joined_at=datetime.datetime.now(datetime.UTC)
                )
                .on_conflict_do_nothing(index_elements=["user_id"])
            )
        row = await self.load(user_id)
        if row is None:
            raise RuntimeError(f"matrix conversation vanished immediately after claiming {room_id=}")
        return row.room_id

    async def set_session(self, user_id: str, session_id: UUID | None) -> None:
        async with self._sessions() as db, db.begin():
            row = await db.scalar(select(MatrixConversation).where(MatrixConversation.user_id == user_id))
            if row is None:
                raise RuntimeError("cannot bind a session before a room is bound")
            row.session_id = session_id

    async def conversation_for_room(self, room_id: str, operator_id: UUID) -> UUID:
        """The conversation this room holds a copy of, opening one the first time it is asked.

        The room's live `chat_attachment` row is the answer, and it outlives every session that
        served the room — which is the whole point: a replacement session joins the conversation the
        attachment already names instead of the attachment being re-pointed at the replacement.

        Read-then-insert rather than insert-or-nothing because the only caller is the session
        supervisor, which runs under its own advisory lock; a second writer would want
        `uq_chat_attachment_live_address` as the arbiter instead.
        """
        async with self._sessions() as db, db.begin():
            conversation_id: UUID | None = await db.scalar(
                select(ChatAttachment.conversation_id).where(
                    ChatAttachment.surface == ChatSurface.MATRIX,
                    ChatAttachment.address == room_id,
                    ChatAttachment.detached_at.is_(None),
                )
            )
            if conversation_id is not None:
                return conversation_id
            now = datetime.datetime.now(datetime.UTC)
            conversation_id = uuid4()
            db.add(Conversation(conversation_id=conversation_id, operator_id=operator_id, created_at=now))
            # Flushed before the attachment that points at it. The unit of work orders a flush from
            # `relationship()` dependencies and nothing else, so a bare `ForeignKey` between two
            # mappers leaves their inserts in mapper-name order — `chat_attachment` ahead of
            # `conversation`, which the constraint rejects.
            await db.flush()
            db.add(
                ChatAttachment(
                    attachment_id=uuid4(),
                    conversation_id=conversation_id,
                    surface=ChatSurface.MATRIX,
                    address=room_id,
                    attached_at=now,
                    detached_at=None,
                )
            )
            return conversation_id


@dataclass(frozen=True)
class RecordedMessage:
    """One thing that was said in a room, as the console wrote it down."""

    role: ChatMessageRole
    body: str
    sent_at: datetime.datetime


class RoomTranscript:
    """What was said in a room, read back out of the console's own record.

    The read half of what `MatrixTurns.offer` and the turn loop write, and the reason it is a
    separate object from `SessionStore`: this question is keyed by **room** and spans every
    session that has served it, where a store scoped to one session cannot answer it — a
    replacement session's whole problem is that the rows it needs belong to its predecessor.
    `sessions.room_id` is what makes that chain readable, since it is written once and never moves
    (R11.3a), unlike the pointer in `matrix_conversation`.

    **A row is here once it was said, and the two sides say that differently.** An operator row
    exists from the moment ingress accepted the batch, which is the only statement we have that it
    was said at all; a Haku row is said when it completes, which is the exact condition under which
    `_enqueue_reply` wrote the room's copy in the same transaction. So a message still streaming, a
    turn that failed mid-answer, and the empty row a tool-only message leaves are all excluded —
    the room was never told any of them either.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    async def recent(self, room_id: str, *, before_session: UUID, limit: int) -> tuple[RecordedMessage, ...]:
        """The last *limit* things said in *room_id*, oldest first, excluding *before_session*'s own.

        Nothing keyed to the session being started belongs in its history: the only way it has a row
        before its first turn is the re-offer of the batch it is about to be handed, and a message
        in both the history and the prompt reads as having been said twice.
        """
        said = (
            select(SessionMessage.role, SessionMessage.content, SessionMessage.created_at)
            .join(Session, Session.session_id == SessionMessage.session_id)
            .where(
                Session.room_id == room_id,
                SessionMessage.session_id != before_session,
                or_(
                    SessionMessage.role == ChatMessageRole.USER,
                    and_(SessionMessage.status == ChatMessageStatus.COMPLETE, func.trim(SessionMessage.content) != ""),
                ),
            )
            # Descending with a limit, then reversed: the tail is what is wanted, and paging from
            # the front of a long-lived room to reach it would read the whole conversation.
            .order_by(SessionMessage.created_at.desc(), SessionMessage.message_id.desc())
            .limit(limit)
        )
        async with self._sessions() as db:
            rows = (await db.execute(said)).all()
        return tuple(
            RecordedMessage(role=role, body=content, sent_at=created_at) for role, content, created_at in reversed(rows)
        )


@dataclass(frozen=True)
class PromptAccepted:
    """The batch is a prompt row on the live session, and a turn will answer it."""

    message_id: UUID


@dataclass(frozen=True)
class PromptRejected:
    """The batch was refused, and is not coming back: what to say, and the row that records it.

    `event` is None only where there is no session row to key the fact to — nothing provisioned
    yet, or the supervisor between sessions — so there the room notice is the only account of it.
    Giving that case a home needs an entity above the session to own the event, which does not
    exist yet; a `session_events` row names a session (<../../../plans/session_channels.md> § 3).
    """

    reason: PromptRejection
    event: SessionEvent | None


type Admission = PromptAccepted | PromptRejected


class MatrixTurns:
    """Ingress: hands the operator's messages to the session behind the live room.

    Refusal is a first-class answer and a terminal one. `enqueue_prompt` accepts a prompt only on
    a `ready` session with nothing already queued, so a message can arrive mid-turn, mid-provision,
    or between a session dying and its replacement — and in each case the message is rejected
    rather than held: the operator is told so and sends it again, and nothing queues behind a
    running turn. What the caller does with a rejection is acknowledge it, recording the row this
    hands back in the same transaction (`sync.MatrixSyncStore.advance`).

    Nothing here is delivery either. A prompt this accepts can still be stranded by a session that
    ends before claiming it, and it is acknowledged all the same; what carries it forward is the
    transcript the replacement session is woken with, where the unclaimed prompt row already is.
    """

    def __init__(
        self,
        config: MatrixConfig,
        conversations: MatrixConversationStore,
        chat_store: SessionStore,
        identities: PostgresOperatorIdentityStore,
    ):
        self._config = config
        self._conversations = conversations
        self._chat_store = chat_store
        self._identities = identities

    async def offer(self, messages: Sequence[InboundMessage]) -> Admission:
        """Enqueue `messages` as one prompt, or say why the session would not take them.

        The whole batch or none of it: a partial enqueue would leave half a sentence delivered and
        half of it rejected, which is a worse answer than either.
        """
        conversation = await self._conversations.load(self._config.user_id)
        if conversation is None or conversation.session_id is None:
            logger.info("Matrix: no session behind the room, rejecting %d message(s)", len(messages))
            return PromptRejected(reason=PromptRejection.NO_SESSION, event=None)
        operator_id = await self._identities.resolve_configured_external_user_key(self._config.operator_subject)
        prompt_text = _as_prompt(messages)
        try:
            prompt = await self._chat_store.enqueue_prompt(operator_id, conversation.session_id, prompt_text)
        except KeyError:
            # The session row has gone under us — the supervisor is between sessions. Nothing to
            # key an event to, so this reads to the operator like never having had one.
            logger.info("Matrix: session %s is gone, rejecting the batch", conversation.session_id)
            return PromptRejected(reason=PromptRejection.NO_SESSION, event=None)
        except PromptRefusedError as refusal:
            # Admission is `enqueue_prompt`'s alone, and it decides under `SELECT … FOR UPDATE`.
            # A status read here first could only ever agree with a decision that had not been
            # made yet, so it was two answers to one question and the durable one always won.
            logger.info("Matrix: session %s rejected the batch: %s", conversation.session_id, refusal.reason)
            return PromptRejected(
                reason=refusal.reason,
                event=session_events.authored(
                    PromptRejectedBody(reason=refusal.reason, text=prompt_text),
                    session_id=conversation.session_id,
                    now=datetime.datetime.now(datetime.UTC),
                ),
            )
        return PromptAccepted(message_id=prompt.message_id)

    async def unreadable(self, events: Sequence[UnmappableEvent]) -> tuple[SessionEvent, ...]:
        """The rows for events Haku has no way to read, one each, for the caller to write.

        Empty where no session is bound, on the same terms as `PromptRejected.event`: what
        arrived is still announced in the room, and the record keeps nothing.
        """
        conversation = await self._conversations.load(self._config.user_id)
        if conversation is None or conversation.session_id is None:
            return ()
        now = datetime.datetime.now(datetime.UTC)
        return tuple(
            session_events.authored(
                UnreadableInputBody(media_type=event.msgtype), session_id=conversation.session_id, now=now
            )
            for event in events
        )


def _as_prompt(messages: Sequence[InboundMessage]) -> str:
    """Render a batch as one prompt.

    Event IDs are carried inline so the agent can cite a specific message back, which is
    the cheap half of treating the room as a source (R11.2) — the read tools are not built
    yet, but referring to what it was told does not need them.
    """
    return "\n".join(f"[{message.event_id}] {message.body}" for message in messages)


class MatrixSurface:
    """Everything the turn loop does that is specific to a session serving a Matrix room.

    One class rather than three sinks, and no session filtering in any of them: the console
    picks this by reading the session's own `surface` (R11.3a), so being called at all is the
    statement that this session serves the bound room. Each method used to begin by loading the
    current room binding and comparing its `session_id` — the row's own fact, re-derived per
    delivery, in a form where getting it wrong meant silently saying nothing.

    History is read here rather than carried forward from the previous session, because by
    the time a replacement session starts, the one that held the context is gone. **Our own
    transcript is the source, not the homeserver's copy of the room** (R3.3a, as amended by the
    invariant in <../../../debug/channel_write_audit.md>, #4130): Matrix is one channel among several, and a
    session re-awakened from the channel's record is a session whose memory a second channel
    could not reproduce.

    The two can still disagree, in both directions, and the record wins by construction: a reply
    the outbox has not drained yet is here before it is in the room, and an operator message
    redacted after we recorded it stays here after the room has forgotten it.

    The `RoomChannel` is the sync service, which holds the only Matrix credential and services
    one room (R3.6a) — so this frontend is bound to its address by construction and takes none.
    """

    def __init__(
        self, config: MatrixConfig, runtime: ClaudeRuntimeConfig, template: SystemPromptTemplate, room: RoomChannel
    ):
        self._config = config
        self._runtime = runtime
        self._template = template
        self._room = room

    async def system_prompt(self, session_id: UUID) -> str:
        """Introduce the session to itself, naming the room it was started to serve.

        The room id is prompt text rather than an address — the channel is what knows where to
        speak — so it is asked for here rather than threaded through the turn loop.
        """
        room_id = await self._room.bound_room()
        if room_id is None:
            raise RuntimeError("a session serves this channel but no room is bound to it")
        return self._template.render(
            SessionIntroduction(
                session_id=session_id,
                room_id=room_id,
                operator_user_id=self._config.operator_user_id,
                workspace=self._runtime.cwd,
                recent_messages=await self._recent(session_id),
            )
        )

    async def report_silent_turn(self) -> None:
        """Say that a turn finished with nothing to show for it (R11.2).

        Every turn speaks, and there is deliberately no silence token — an empty answer that
        produced no room event at all made the empty string into one, which is the single thing
        that requirement rules out. A notice rather than a reply, because nothing was said: this
        is the console reporting an outcome, not the agent talking.
        """
        logger.warning("Matrix: a turn finished with no text to send")
        await self._room.announce(NOTHING_SAID, RoomEventKind.NARRATION)

    async def report_abort(self) -> None:
        """Show the `turn_aborted` event the closing transaction just wrote.

        A notice and no outbox row: the record already holds the fact, so the room's copy is a
        rendering a reconciler re-derives rather than a delivery anything owes
        (<../../../plans/session_channels.md> § 1).
        """
        await self._room.announce(ABORTED_BY_OPERATOR, RoomEventKind.NARRATION)

    async def report(self, detail: str) -> None:
        """Narrate the sandbox's setup into the room (R7.1)."""
        await self._room.announce(detail, RoomEventKind.NARRATION)

    async def show_status(self, text: str) -> None:
        """Say what the turn is doing now, on the room's one status line (R6.2)."""
        await self._room.show_status(text)

    async def clear_status(self) -> None:
        """Retire that line once the turn is over, however it ended (R6.5)."""
        await self._room.clear_status()

    async def set_typing(self, active: bool) -> None:
        """Show a turn in progress without the agent doing anything about it (R6.1)."""
        await self._room.set_typing(active)

    async def _recent(self, session_id: UUID) -> Sequence[HistoryMessage]:
        """The tail of the conversation, or none of it if our own record would not answer.

        The one degradation in this path that is worth taking rather than failing the
        session over: a session that starts without its last twenty messages is still Haku
        and can be told what it missed, where a session that never starts is a room that
        goes quiet. Loud, though — and what it now means is different: this used to be the
        homeserver refusing to serve history, and is now our own store failing to answer a read,
        which is a symptom worth seeing on its own rather than only as the next query's error.
        """
        try:
            return await self._room.recent_history(session_id, RE_AWAKENING_MESSAGES)
        except Exception:
            logger.exception("Matrix: could not read this room's transcript; starting the session without it")
            return []


class MatrixSessionSupervisor:
    """Provisions and replaces the session behind the live room (R3.1)."""

    def __init__(
        self,
        config: MatrixConfig,
        conversations: MatrixConversationStore,
        chat: SessionService,
        chat_store: SessionStore,
        notifications: SessionNotifications,
        identities: PostgresOperatorIdentityStore,
        announce: Announce,
        engine: AsyncEngine,
    ):
        self._config = config
        self._engine = engine
        self._conversations = conversations
        self._chat = chat
        self._chat_store = chat_store
        self._notifications = notifications
        self._identities = identities
        self._announce = announce
        self._last_announced: str | None = None

    async def _operator_id(self) -> UUID:
        """The canonical Operator behind the configured MXID (R9.3).

        Resolved per pass rather than cached at startup: the console must come up with the
        Matrix surface configured even if identity resolution is not yet possible, and a
        supervisor that had cached a failure would never recover.
        """
        return await self._identities.resolve_configured_external_user_key(self._config.operator_subject)

    async def _report(self, status: str, detail: str) -> None:
        """Announce a status the room has not already been told about.

        Every transition is reported while this is being debugged; the filter here is only
        against repeating the same one on each poll.
        """
        if status == self._last_announced:
            return
        self._last_announced = status
        await self._announce(detail)

    async def supervise_once(self) -> None:
        """Bring the live room's session back to a working state, if it is not already."""
        binding = await self._conversations.load(self._config.user_id)
        if binding is None:
            return  # No room yet — nothing to serve, and nowhere to say so.

        # Before believing a live status, give a session whose holder has gone away the chance
        # to become an ended one. Otherwise this method reads `responding` off a row nobody is
        # working on and returns satisfied, which is how a room stops being answered without
        # anything reporting a failure.
        await self._chat_store.expire_stale_leases()

        outcome = await self._chat_store.outcome(binding.session_id) if binding.session_id is not None else None
        status = outcome.status if outcome is not None else None
        if status in OPEN_SESSION_STATUSES:
            await self._report(str(status), f"session {binding.session_id} is {status}")
            return

        if binding.session_id is not None:
            # With the reason, not just the status. Every path that ends a session records a
            # specific sentence in `error` — which replica went away, what the runtime failed
            # with, that the runner disconnected — and the room used to be told only `failed`,
            # so the one place an operator was looking held the least informative version of
            # what the console knew.
            reason = f" — {outcome.error}" if outcome is not None and outcome.error else ""
            await self._report(
                f"ended:{status}",
                f"session {binding.session_id} ended ({status or 'gone'}){reason}; starting a new one",
            )
            # The claim may already be gone — `handle_runner` deletes it on the way out — so
            # this is the idempotent sweep rather than a targeted delete.
            await self._conversations.set_session(self._config.user_id, None)
            await self._chat.reconcile_terminal_claims()

        operator_id = await self._operator_id()
        # The replacement joins the conversation the room is already attached to, so the attachment
        # is not touched and the thread survives the session that was running it.
        session = await self._chat.create(
            operator_id,
            MatrixSession(room_id=binding.room_id),
            conversation_id=await self._conversations.conversation_for_room(binding.room_id, operator_id),
        )
        await self._conversations.set_session(self._config.user_id, session.session_id)
        self._last_announced = SessionStatus.PROVISIONING
        await self._announce(f"provisioning a sandbox · session {session.session_id}")
        logger.info("Matrix: provisioned session %s for room %s", session.session_id, binding.room_id)

    async def _supervise_as_leader(self) -> None:
        """Supervise until cancelled. Only ever entered holding the advisory lock."""
        while True:
            try:
                await self.supervise_once()
            except Exception:
                logger.exception("Matrix: session supervision failed")
                await asyncio.sleep(PROVISION_BACKOFF.total_seconds())
                continue
            await self._wait_for_change()

    async def _wait_for_change(self) -> None:
        """Wait until the session's status may have changed, or the interval elapses.

        Polling alone made every status notice up to a full interval late: the room said
        "responding" about five seconds after the turn had actually started, and "ready"
        about five seconds after the answer had already arrived. The chat store notifies on
        every status transition, so waiting on that channel reports them as they happen.

        The interval stays as the backstop for what no transition announces — a room bound
        for the first time, or a session row disappearing underneath us.
        """
        binding = await self._conversations.load(self._config.user_id)
        if binding is None or binding.session_id is None:
            await asyncio.sleep(SUPERVISE_INTERVAL.total_seconds())
            return
        await self._notifications.wait(
            SessionEventKind.UPDATE, binding.session_id, timeout_seconds=SUPERVISE_INTERVAL.total_seconds()
        )

    async def _run(self) -> None:
        """Contend for leadership, and supervise for as long as we hold it.

        Without this every replica provisions: two replicas each created a session for the
        same room, each overwrote the other's pointer, and each narrated the result — which
        is what the room showed on 2026-08-10. The lock is held for the loop's lifetime
        rather than per pass, so a session cannot be created between one replica's status
        read and its decision to replace it.
        """
        while True:
            async with self._engine.connect() as leader:
                locked = await leader.scalar(
                    text("SELECT pg_try_advisory_lock(:lock)"), {"lock": _SUPERVISOR_ADVISORY_LOCK}
                )
                if not locked:
                    await asyncio.sleep(LEADER_RETRY.total_seconds())
                    continue
                logger.info("Matrix: this replica (%s) is the session supervisor", REPLICA)
                # Said in the room, not just logged. `_last_announced` is per-process, so a
                # new leader re-announces whatever the current status is — which reads as the
                # session having changed when only the supervisor did. Naming the replica
                # makes a handover legible instead of looking like a duplicate notice.
                await self._report(f"leader:{REPLICA}", f"session supervisor is now {REPLICA}")
                try:
                    await self._supervise_as_leader()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Matrix: supervision loop exited, retrying")
                    await asyncio.sleep(PROVISION_BACKOFF.total_seconds())
                finally:
                    with contextlib.suppress(Exception):
                        await leader.scalar(
                            text("SELECT pg_advisory_unlock(:lock)"), {"lock": _SUPERVISOR_ADVISORY_LOCK}
                        )

    @asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        task = asyncio.create_task(self._run(), name="matrix-session-supervisor")
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
