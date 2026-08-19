"""The conversation the room Haku services is attached to.

A `chat_attachment` row binds the room to a conversation, and the conversation outlives every
session that runs under it — so the binding, the room's transcript and what the room admits are all
read across sessions, and a replacement session joins the conversation the attachment already names
rather than the attachment being re-pointed at it.
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

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from haku.console.chat_models import (
    OPEN_SESSION_STATUSES,
    ChatSurface,
    ItemStatus,
    ItemType,
    MatrixOrigin,
    PromptRejection,
    SessionStatus,
)
from haku.console.config import ClaudeRuntimeConfig, MatrixConfig
from haku.console.database_schema import ChatAttachment, Conversation, ConversationItem, Session
from haku.console.operator_identity_store import PostgresOperatorIdentityStore
from haku.console.x import session_events
from haku.console.x.channels.matrix.client import InboundMessage, RoomEventKind, UnmappableEvent
from haku.console.x.channels.matrix.ingress_ledger import IngressLedger
from haku.console.x.session_events import PromptRejectedBody, UnreadableInputBody
from haku.console.x.session_notifications import SessionEventKind, SessionNotifications
from haku.console.x.session_runtime import SessionService
from haku.console.x.session_store import REPLICA, PromptRefusedError, SessionStore
from haku.console.x.system_prompt import HistoryMessage, SessionIntroduction, SystemPromptTemplate

logger = logging.getLogger(__name__)

# Distinct from the sync loop's lock in `sync` and the OAuth refresh lock. Its own rather than the
# sync loop's because a stalled claim must not wedge ingress, which keeps reading the room and
# telling it what it is waiting for while no sandbox is up, and sharing one lock would mean a
# supervisor stall could only be resolved by giving up ingress leadership too.
_SUPERVISOR_ADVISORY_LOCK = 0x4D58_5345  # "MXSE"

# What the room hears when a turn ends with no text at all. Phrased as an outcome rather than an
# error, because a turn that only ran tools is legitimate — it just must not look like the console
# lost the answer.
NOTHING_SAID = "the turn finished without saying anything"

SUPERVISE_INTERVAL = datetime.timedelta(seconds=10)
# How long a replica that lost the election waits before contending again.
LEADER_RETRY = datetime.timedelta(seconds=30)
# A failed provision should not be retried as fast as a healthy poll: claim creation talks
# to Kubernetes, and a persistent failure would otherwise become a hot loop against it.
PROVISION_BACKOFF = datetime.timedelta(seconds=60)

# Emits a lifecycle line into the live room. Supplied by the sync service, which owns the access
# token and the send path: the supervisor never gets a Matrix credential of its own, so there is
# still exactly one login and one device.
Announce = Callable[[str], Awaitable[None]]


class RoomChannel(Protocol):
    """Everything `MatrixSurface` needs said to, or read from, the live room.

    Answers do not travel through here: they are rows the outbox drain says into the room, which is
    the only way a producer can be told that the room actually heard one.

    Implemented by the sync loop, the only holder of the credential and the only object that knows
    which room is bound. `bound_room` and `recent_history` are the two methods that ask it for
    something rather than telling it something, and the second is answered out of our own
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
# agent at `haku_index` for it. There is deliberately no summarisation step: a rotation mid-topic
# loses the earlier reasoning, and the operator can say so and be answered from the room.
#
# Counted in **recorded rows**, not room events: a batch the operator sent as three messages is one
# prompt row.
RE_AWAKENING_MESSAGES = 20


async def live_attachment(db: AsyncSession, room_id: str) -> UUID | None:
    """This room's live attachment, which is what its deliveries hang off.

    None where the room holds no conversation — a room this console never bound, or one detached
    since — in which case there is nothing to record a send against and the room notice is the only
    account of it.

    Takes the caller's session so a channel can record what it sent in the transaction that records
    the send itself (`outbox.RoomOutbox.mark_sent`).
    """
    attachment_id: UUID | None = await db.scalar(
        select(ChatAttachment.attachment_id).where(
            ChatAttachment.surface == ChatSurface.MATRIX,
            ChatAttachment.address == room_id,
            ChatAttachment.detached_at.is_(None),
        )
    )
    return attachment_id


@dataclass(frozen=True)
class BoundRoom:
    """The room this console services, and the conversation it holds a copy of."""

    room_id: str
    conversation_id: UUID


async def _live_binding(db: AsyncSession) -> BoundRoom | None:
    """The bound room, read off the attachment that is holding it.

    Ordered so that the answer is the room bound first. One room at a time is `bind_room`'s
    refusal rather than the schema's — `chat_attachment` deliberately admits many live rows,
    because one bot serving several rooms is where this goes — so a second row that somehow
    appeared must not make the bound room flip between reads.
    """
    row = (
        await db.execute(
            select(ChatAttachment.address, ChatAttachment.conversation_id)
            .where(ChatAttachment.surface == ChatSurface.MATRIX, ChatAttachment.detached_at.is_(None))
            .order_by(ChatAttachment.attached_at, ChatAttachment.attachment_id)
            .limit(1)
        )
    ).first()
    return None if row is None else BoundRoom(room_id=row.address, conversation_id=row.conversation_id)


class MatrixConversationStore:
    """Which conversation the bound room holds a copy of, and which session is running under it.

    The conversation is the durable half and the one a replacement session joins, so which session
    is serving the room is derived from it (`session_serving`) rather than kept as a pointer that
    has to be re-aimed.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    async def attachment(self, room_id: str) -> UUID | None:
        async with self._sessions() as db:
            return await live_attachment(db, room_id)

    async def bound_room(self) -> BoundRoom | None:
        """The room this console services, or None before the operator has invited it into one."""
        async with self._sessions() as db:
            return await _live_binding(db)

    async def bind_room(self, room_id: str, operator_id: UUID) -> BoundRoom:
        """Attach `room_id` if no room is attached yet; return whichever room is live.

        A caller that gets back a different room than it asked for has been refused. Binding opens
        the conversation the room holds a copy of, in the same transaction — so a bound room always
        has one, and the attachment outlives every session that serves it: a replacement joins the
        conversation the attachment already names instead of the attachment being re-pointed at it.

        Read-then-insert rather than insert-or-nothing, serialized by the sync loop's election:
        only its leader handles invites, so the read and the insert cannot interleave with another
        replica's. `uq_chat_attachment_live_address` is the backstop if that ever stops holding.
        """
        async with self._sessions() as db, db.begin():
            if (live := await _live_binding(db)) is not None:
                return live
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
            return BoundRoom(room_id=room_id, conversation_id=conversation_id)

    async def session_serving(self) -> UUID | None:
        """The session behind the bound room, or None while nothing is serving it.

        Read through the conversation the room's live attachment names rather than through a
        pointer: successive sessions of one thread share `conversation_id`, so the newest of them
        is the answer and a replacement needs nothing re-pointed at it.
        """
        async with self._sessions() as db:
            session_id: UUID | None = await db.scalar(
                select(Session.session_id)
                .join(ChatAttachment, ChatAttachment.conversation_id == Session.conversation_id)
                .where(ChatAttachment.surface == ChatSurface.MATRIX, ChatAttachment.detached_at.is_(None))
                .order_by(Session.created_at.desc(), Session.session_id.desc())
                .limit(1)
            )
            return session_id

    async def attachment_of_room(self, room_id: str) -> tuple[UUID, UUID] | None:
        """The conversation *room_id* holds a copy of and the attachment holding it, or None.

        Both, because a subscriber needs one of each: the conversation to read the log, and the
        attachment to key the position it reads from. Addressed by room rather than answered from
        the binding, because a subscriber is told which room it is reading for and a room that is
        not the bound one holds nothing.
        """
        async with self._sessions() as db:
            found = (
                await db.execute(
                    select(ChatAttachment.conversation_id, ChatAttachment.attachment_id).where(
                        ChatAttachment.surface == ChatSurface.MATRIX,
                        ChatAttachment.address == room_id,
                        ChatAttachment.detached_at.is_(None),
                    )
                )
            ).first()
            return None if found is None else (found.conversation_id, found.attachment_id)


@dataclass(frozen=True)
class RecordedMessage:
    """One thing that was said in a room, as the console wrote it down.

    `item_type` is the neutral vocabulary's, not a chat role: who said it follows from what kind of
    item it is, and the channel is what turns that into an address.
    """

    item_type: ItemType
    body: str
    sent_at: datetime.datetime


class RoomTranscript:
    """The room's conversation, read back out of the console's own record.

    Keyed by **conversation** and spanning every session that has run it, which is why it is a
    separate object from `SessionStore`: a store scoped to one session cannot answer it, since a
    replacement session's whole problem is that the rows it needs belong to its predecessor.
    `sessions.conversation_id` is what makes that chain readable: sessions of one thread share it,
    and it outlives each of them.

    **Prompts and messages only, and only finished ones.** Reasoning and tool calls are the session's
    own working, not what was said; an item still streaming and the empty item a tool-only turn
    leaves are excluded because the room was never told either of them.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    async def recent(self, conversation_id: UUID, *, before_session: UUID, limit: int) -> tuple[RecordedMessage, ...]:
        """The last *limit* things said in *conversation_id*, oldest first, minus *before_session*'s.

        The only way the session being started has an item before its first turn is the batch it is
        about to be handed, and a message in both the history and the prompt reads as having been
        said twice. `is_distinct_from` rather than `!=` so an item no session has claimed still
        counts — a comparison against NULL is neither true nor false, and would silently drop it.
        """
        said = (
            select(ConversationItem.item_type, ConversationItem.item_text, ConversationItem.created_at)
            .where(
                ConversationItem.conversation_id == conversation_id,
                ConversationItem.session_id.is_distinct_from(before_session),
                ConversationItem.item_type.in_((ItemType.PROMPT, ItemType.MESSAGE)),
                ConversationItem.status == ItemStatus.COMPLETE,
                func.trim(ConversationItem.item_text) != "",
            )
            # Descending with a limit, then reversed: the tail is what is wanted, and paging from
            # the front of a long-lived room to reach it would read the whole conversation.
            .order_by(ConversationItem.created_at.desc(), ConversationItem.item_id.desc())
            .limit(limit)
        )
        async with self._sessions() as db:
            rows = (await db.execute(said)).all()
        return tuple(
            RecordedMessage(item_type=item_type, body=text, sent_at=created_at)
            for item_type, text, created_at in reversed(rows)
        )


@dataclass(frozen=True)
class ConversationFacts:
    """Console-authored events belonging to one conversation, for the caller to append.

    **Bodies rather than rows.** An authored event's position is allocated under the conversation's
    lock, so only the transaction that writes it can say where it goes — and that is the transaction
    that acknowledges the batch (`sync.MatrixSyncStore.advance`), which is what keeps the record of
    what a pass decided from being lost by a crash after the watermark moved.

    `session_id` is absent where no session was up to refuse the batch: what a refusal is about is
    the conversation, which exists as soon as the room is bound.
    """

    conversation_id: UUID
    session_id: UUID | None
    bodies: tuple[session_events.AuthoredBody, ...]

    def then(self, *more: session_events.AuthoredBody) -> ConversationFacts:
        return ConversationFacts(
            conversation_id=self.conversation_id, session_id=self.session_id, bodies=self.bodies + more
        )


@dataclass(frozen=True)
class PromptAccepted:
    """The batch is a prompt item on the live session, and a turn will answer it."""

    item_id: UUID


@dataclass(frozen=True)
class PromptRejected:
    """The batch was refused, and is not coming back: what to say, and the fact that records it.

    `facts` is None only where no room is bound, so there is no conversation to record against —
    and nowhere to say it either, which makes the two absences the same one.
    """

    reason: PromptRejection
    facts: ConversationFacts | None


type Admission = PromptAccepted | PromptRejected


class MatrixTurns:
    """Ingress: offers the operator's messages to the conversation the room is attached to.

    Refusal is a first-class answer and a terminal one. `enqueue_prompt` accepts a prompt only on a
    `ready` session with nothing already queued, so a message arriving mid-turn, mid-provision, or
    between a session dying and its replacement is rejected rather than held: the operator is told
    so and sends it again. What the caller does with a rejection is acknowledge it, recording the
    row this hands back in the same transaction (`sync.MatrixSyncStore.advance`).

    A prompt this accepts is the conversation's, not the accepting session's, so a session that dies
    before claiming it strands nothing: its replacement finds the same queued row. What the record
    keeps against the events themselves (`ingress_ledger`) is only what makes a re-delivery
    recognisable.
    """

    def __init__(
        self,
        config: MatrixConfig,
        conversations: MatrixConversationStore,
        chat_store: SessionStore,
        identities: PostgresOperatorIdentityStore,
        ledger: IngressLedger,
    ):
        self._config = config
        self._conversations = conversations
        self._chat_store = chat_store
        self._identities = identities
        self._ledger = ledger

    async def offer(self, messages: Sequence[InboundMessage]) -> Admission:
        """Enqueue `messages` as one prompt, or say why the session would not take them.

        The whole batch or none of it: a partial enqueue would leave half a sentence delivered and
        half of it rejected, which is a worse answer than either.
        """
        return await self._enqueue(_as_prompt(messages), tuple(message.event_id for message in messages))

    async def _enqueue(self, prompt_text: str, event_ids: tuple[str, ...]) -> Admission:
        # The binding is read for the room alone — which session is serving comes through the
        # conversation — because the room is the address this prompt's origin names.
        binding = await self._conversations.bound_room()
        if binding is None:
            logger.info("Matrix: no room bound, rejecting %d event(s)", len(event_ids))
            return PromptRejected(reason=PromptRejection.NO_SESSION, facts=None)
        session_id = await self._conversations.session_serving()
        if session_id is None:
            logger.info("Matrix: no session behind the room, rejecting %d event(s)", len(event_ids))
            return self._refused(binding, None, PromptRejection.NO_SESSION, prompt_text)
        operator_id = await self._identities.resolve_configured_external_user_key(self._config.operator_subject)
        try:
            item_id = await self._chat_store.enqueue_prompt(
                operator_id,
                session_id,
                prompt_text,
                _origin(binding.room_id, event_ids),
                self._ledger.carrying(event_ids),
            )
        except KeyError:
            # The session row has gone under us — the supervisor is between sessions. Recorded
            # against the conversation with no session named, which is what happened.
            logger.info("Matrix: session %s is gone, rejecting the batch", session_id)
            return self._refused(binding, None, PromptRejection.NO_SESSION, prompt_text)
        except PromptRefusedError as refusal:
            # Admission is `enqueue_prompt`'s alone, decided under `SELECT … FOR UPDATE`: a status
            # read here could only agree with a decision that had not been made yet.
            logger.info("Matrix: session %s rejected the batch: %s", session_id, refusal.reason)
            return self._refused(binding, session_id, refusal.reason, prompt_text)
        return PromptAccepted(item_id=item_id)

    def _refused(
        self, binding: BoundRoom, session_id: UUID | None, reason: PromptRejection, prompt_text: str
    ) -> PromptRejected:
        return PromptRejected(
            reason=reason,
            facts=ConversationFacts(
                conversation_id=binding.conversation_id,
                session_id=session_id,
                bodies=(PromptRejectedBody(reason=reason, text=prompt_text),),
            ),
        )

    async def unreadable(self, events: Sequence[UnmappableEvent]) -> ConversationFacts | None:
        """The facts for events Haku has no way to read, one each, for the caller to append.

        None where no room is bound, on the same terms as `PromptRejected.facts`: there is no
        conversation to record against, and no room to say it in either.
        """
        binding = await self._conversations.bound_room()
        if binding is None:
            return None
        return ConversationFacts(
            conversation_id=binding.conversation_id,
            session_id=await self._conversations.session_serving(),
            bodies=tuple(UnreadableInputBody(media_type=event.msgtype) for event in events),
        )


def _origin(room_id: str, event_ids: tuple[str, ...]) -> MatrixOrigin:
    """This batch, as the origin the rest of the console may hold but not read.

    One origin rather than one per message: a batch arrives through a single attachment and
    becomes a single prompt, so the room is the origin and the events are what it folded.

    **The room travels with the events**, even though only one room is serviced today: a surface
    deciding whether a prompt is already in front of its reader compares origins, and a bare event
    id cannot tell a sibling room's copy from this room's the moment one bot serves several.
    """
    return MatrixOrigin(address=room_id, refs=event_ids)


def _as_prompt(messages: Sequence[InboundMessage]) -> str:
    """Render a batch as one prompt: what the operator said, in the order they said it.

    The event ids are not rendered into it: they ride on the prompt item's origin, which is what
    the room read tools resolve a citation through and what a reply answering a specific message
    addresses itself with.
    """
    return "\n".join(message.body for message in messages)


class MatrixSurface:
    """What a turn running under this room's conversation says into it.

    No session filtering in any method: the console picks this surface by asking whether a channel
    holds a copy of the session's conversation, so being called at all is the statement that this
    session serves the bound room.

    History is read here rather than carried forward from the previous session, because by the time
    a replacement session starts, the one that held the context is gone. **Our own transcript is
    the source, not the homeserver's copy of the room**
    (<../../../debug/channel_write_audit.md>, #4130): Matrix is one channel among several, and a
    session re-awakened from the channel's record is one whose memory a second channel could not
    reproduce.

    The two can still disagree, in both directions, and the record wins by construction: a reply
    the outbox has not drained yet is here before it is in the room, and an operator message
    redacted after we recorded it stays here after the room has forgotten it.

    The `RoomChannel` is the sync service, which holds the only Matrix credential and services one
    room — so this frontend is bound to its address by construction and takes none.
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
        """Say that a turn finished with nothing to show for it.

        Every turn speaks, and there is deliberately no silence token. A notice rather than a
        reply, because nothing was said: this is the console reporting an outcome, not the agent
        talking.
        """
        logger.warning("Matrix: a turn finished with no text to send")
        await self._room.announce(NOTHING_SAID, RoomEventKind.NARRATION)

    async def report(self, detail: str) -> None:
        """Narrate the sandbox's setup into the room."""
        await self._room.announce(detail, RoomEventKind.NARRATION)

    async def show_status(self, text: str) -> None:
        """Say what the turn is doing now, on the room's one status line."""
        await self._room.show_status(text)

    async def clear_status(self) -> None:
        """Retire that line once the turn is over, however it ended."""
        await self._room.clear_status()

    async def set_typing(self, active: bool) -> None:
        """Show a turn in progress without the agent doing anything about it."""
        await self._room.set_typing(active)

    async def _recent(self, session_id: UUID) -> Sequence[HistoryMessage]:
        """The tail of the conversation, or none of it if our own record would not answer.

        The one degradation in this path worth taking rather than failing the session over: a
        session that starts without its last twenty messages is still Haku and can be told what it
        missed, where a session that never starts is a room that goes quiet. Loud, though — a
        failed read here is our own store, and worth seeing on its own.
        """
        try:
            return await self._room.recent_history(session_id, RE_AWAKENING_MESSAGES)
        except Exception:
            logger.exception("Matrix: could not read this room's transcript; starting the session without it")
            return []


class MatrixSessionSupervisor:
    """Provisions and replaces the session running under the room's conversation.

    The console's other chat surface is driven by an operator browser gesture: a `POST` creates a
    session, mints a bridge token and provisions a SandboxClaim. Matrix has no gesture, so something
    has to own *"this conversation has a session and it has a live sandbox"* — this.
    """

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
        """The canonical Operator behind the configured MXID.

        Resolved per pass rather than cached at startup: the console must come up with the Matrix
        surface configured even if identity resolution is not yet possible, and a supervisor that
        had cached a failure would never recover.
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
        binding = await self._conversations.bound_room()
        if binding is None:
            return  # No room yet — nothing to serve, and nowhere to say so.

        # Before believing a live status, give a session whose holder has gone away the chance
        # to become an ended one. Otherwise this method reads `responding` off a row nobody is
        # working on and returns satisfied, which is how a room stops being answered without
        # anything reporting a failure.
        await self._chat_store.expire_stale_leases()

        session_id = await self._conversations.session_serving()
        outcome = await self._chat_store.outcome(session_id) if session_id is not None else None
        status = outcome.status if outcome is not None else None
        if status in OPEN_SESSION_STATUSES:
            await self._report(str(status), f"session {session_id} is {status}")
            return

        if session_id is not None:
            # With the reason, not just the status: every path that ends a session records a
            # specific sentence in `error`, and the room is where the operator is looking.
            reason = f" — {outcome.error}" if outcome is not None and outcome.error else ""
            await self._report(
                f"ended:{status}", f"session {session_id} ended ({status or 'gone'}){reason}; starting a new one"
            )
            # The claim may already be gone — `handle_runner` deletes it on the way out — so
            # this is the idempotent sweep rather than a targeted delete.
            await self._chat.reconcile_terminal_claims()

        # The replacement joins the conversation the room is already attached to, so the attachment
        # is not touched and the thread survives the session that was running it.
        session = await self._chat.create(await self._operator_id(), conversation_id=binding.conversation_id)
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

        The chat store notifies on every status transition, so waiting on that channel reports them
        as they happen rather than up to a full interval late. The interval stays as the backstop
        for what no transition announces — a room bound for the first time, or a session row
        disappearing underneath us.
        """
        session_id = await self._conversations.session_serving()
        if session_id is None:
            await asyncio.sleep(SUPERVISE_INTERVAL.total_seconds())
            return
        await self._notifications.wait(
            SessionEventKind.UPDATE, session_id, timeout_seconds=SUPERVISE_INTERVAL.total_seconds()
        )

    async def _run(self) -> None:
        """Contend for leadership, and supervise for as long as we hold it.

        Without the lock every replica provisions: two would each create a session for the same
        room, overwrite each other's pointer and narrate the result. It is held for the loop's
        lifetime rather than per pass, so a session cannot be created between one replica's status
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
                # Said in the room, not just logged. `_last_announced` is per-process, so a new
                # leader re-announces the current status; naming the replica makes that read as a
                # handover rather than as the session having changed.
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
