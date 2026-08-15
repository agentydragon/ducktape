"""Keeps one live chat session bound to the one room Haku services.

The console's chat machinery is driven by an operator browser gesture: a `POST` creates a
session, mints a bridge token, and provisions a SandboxClaim; the sandbox then dials back
and `handle_runner` lives for the life of that WebSocket. Matrix has no gesture, so
something has to own *"there is one session and it has a live sandbox"* — that is this.

Runs as a sibling task to the sync loop, under an advisory lock of its own. Being locked
is what keeps exactly one replica provisioning; being a separate task from `/sync` keeps a
slow or stalled claim from wedging ingress, which must keep accepting messages while no
sandbox is up (R1.4). Its own lock rather than the sync loop's, because the two only need
single-execution, not co-location — and a shared lock would mean a supervisor stall could
only be resolved by giving up ingress leadership too.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Protocol
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from haku.console.chat_models import LIVE_SESSION_STATUSES, ChatSessionStatus
from haku.console.config import ClaudeRuntimeConfig, MatrixConfig
from haku.console.database_schema import MatrixConversation
from haku.console.operator_identity_store import PostgresOperatorIdentityStore
from haku.console.x.chat_notifications import ChatEventKind, ChatNotifications
from haku.console.x.claude_chat import REPLICA, ClaudeChatService, ClaudeChatStore, MatrixSession
from haku.console.x.matrix_client import EventTag, InboundMessage, RoomEventKind
from haku.console.x.system_prompt import HistoryMessage, SessionIntroduction, SystemPromptTemplate

logger = logging.getLogger(__name__)

# Distinct from the sync loop's lock in matrix_sync and the OAuth refresh lock.
_SUPERVISOR_ADVISORY_LOCK = 0x4D58_5345  # "MXSE"

# A session is worth keeping while it is in one of these; anything else (including a
# missing row) means the room has no working sandbox behind it.

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

    One port rather than the four separate dependencies this used to take — a history callable,
    an announce callable, a reply callable, and a status object — all of which were bound to the
    same sync service at composition. The callables were narrow because the *supervisor* must
    never hold a Matrix credential (`Announce` above, still one function for exactly that
    reason); passing three of them plus the whole service to one collaborator bought nothing and
    hid that they were one thing.

    Implemented by the sync loop, the only holder of the credential and the only object that
    knows which room is bound.
    """

    async def recent_history(self, limit: int) -> Sequence[InboundMessage]: ...

    async def announce(self, body: str, kind: RoomEventKind = ...) -> None: ...

    async def reply(self, body: str, tag: EventTag) -> None: ...

    async def show_status(self, body: str, session_id: UUID | None = ...) -> None: ...

    async def set_typing(self, active: bool) -> None: ...

    async def clear_status(self) -> None: ...


# How much of the conversation a replacement session is handed. Enough to pick up a thread
# mid-topic, not enough to be a transcript — anything older is in the room, which the agent
# can be pointed at (R3.3a, [v1] "start without the summary").
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


class MatrixTurns:
    """Ingress: hands the operator's messages to the session behind the live room.

    Refusal is a first-class answer. `enqueue_prompt` accepts a prompt only on a `ready`
    session with nothing already queued, so a message can arrive mid-turn, mid-provision,
    or between a session dying and its replacement. The caller's contract is that a refused
    batch leaves the sync watermark where it is, so Matrix itself holds the message and the
    next pass re-offers it — which is what makes queue-until-turn-end (R2.2) and "no
    message is silently dropped" (R1.6) fall out of machinery that already exists, with no
    second durable queue to keep correct.
    """

    def __init__(
        self,
        config: MatrixConfig,
        conversations: MatrixConversationStore,
        chat_store: ClaudeChatStore,
        identities: PostgresOperatorIdentityStore,
    ):
        self._config = config
        self._conversations = conversations
        self._chat_store = chat_store
        self._identities = identities

    async def offer(self, messages: Sequence[InboundMessage]) -> bool:
        """Enqueue `messages` as one prompt. False if the session cannot take it yet.

        The whole batch or none of it: a partial enqueue followed by a refusal would be
        re-offered on the next pass and deliver its accepted half twice.
        """
        conversation = await self._conversations.load(self._config.user_id)
        if conversation is None or conversation.session_id is None:
            logger.info("Matrix: no session bound yet, holding %d message(s)", len(messages))
            return False
        if (status := await self._chat_store.status(conversation.session_id)) != ChatSessionStatus.READY:
            logger.info(
                "Matrix: session %s is %s, holding %d message(s)", conversation.session_id, status, len(messages)
            )
            return False
        operator_id = await self._identities.resolve_configured_external_user_key(self._config.operator_subject)
        try:
            await self._chat_store.enqueue_prompt(operator_id, conversation.session_id, _as_prompt(messages))
        except RuntimeError as error:
            # Lost a race with the turn loop, or a prompt landed between the status read and
            # here. Both mean "not now", which is the same answer as an unready session.
            logger.info("Matrix: session %s refused the batch: %s", conversation.session_id, error)
            return False
        return True


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
    statement that this session serves `room_id`. Each method used to begin by loading the
    current room binding and comparing its `session_id` — the row's own fact, re-derived per
    delivery, in a form where getting it wrong meant silently saying nothing.

    History is read here rather than carried forward from the previous session, because by
    the time a replacement session starts, the one that held the context is gone. The room is
    the source (R3.3a) — it is also what Rai sees, so what the prompt claims was said and
    what the room shows cannot disagree.

    The `RoomChannel` is the sync service, which holds the only Matrix credential and services
    one room (R3.6a) — so `room_id` names the room it already speaks to rather than choosing
    between rooms.
    """

    def __init__(
        self, config: MatrixConfig, runtime: ClaudeRuntimeConfig, template: SystemPromptTemplate, room: RoomChannel
    ):
        self._config = config
        self._runtime = runtime
        self._template = template
        self._room = room

    async def system_prompt(self, session_id: UUID, room_id: str) -> str:
        return self._template.render(
            SessionIntroduction(
                session_id=session_id,
                room_id=room_id,
                operator_user_id=self._config.operator_user_id,
                workspace=self._runtime.cwd,
                recent_messages=await self._recent(),
            )
        )

    async def deliver(
        self,
        room_id: str,
        text: str,
        session_id: UUID,
        message_id: UUID | None = None,
        agent_message_id: str | None = None,
    ) -> None:
        """Forward a finished turn's answer into the room (R11.1).

        The ids travel as a tag on the event, so the room says which transcript row it is showing
        rather than leaving that to be matched by position and timing.
        """
        del room_id  # The reply channel is already bound to the one room this console services.
        if not (body := text.strip()):
            logger.warning("Matrix: a turn finished with no text to send")
            return
        await self._room.reply(
            body,
            EventTag(
                kind=RoomEventKind.REPLY,
                session_id=session_id,
                message_id=message_id,
                agent_message_id=agent_message_id,
            ),
        )

    async def report(self, room_id: str, detail: str) -> None:
        """Narrate the sandbox's setup into the room (R7.1)."""
        del room_id
        await self._room.announce(detail, RoomEventKind.NARRATION)

    async def show_status(self, room_id: str, text: str, session_id: UUID | None = None) -> None:
        """Say what the turn is doing now, on the room's one status line (R6.2)."""
        del room_id
        await self._room.show_status(text, session_id)

    async def clear_status(self, room_id: str) -> None:
        """Retire that line once the turn is over, however it ended (R6.5)."""
        del room_id
        await self._room.clear_status()

    async def set_typing(self, room_id: str, active: bool) -> None:
        """Show a turn in progress without the agent doing anything about it (R6.1)."""
        del room_id
        await self._room.set_typing(active)

    async def _recent(self) -> list[HistoryMessage]:
        """The tail of the conversation, or none of it if the homeserver would not say.

        The one degradation in this path that is worth taking rather than failing the
        session over: a session that starts without its last twenty messages is still Haku
        and can be told what it missed, where a session that never starts is a room that
        goes quiet. Loud, though — this is a homeserver problem and should be visible.
        """
        try:
            messages = await self._room.recent_history(RE_AWAKENING_MESSAGES)
        except Exception:
            logger.exception("Matrix: could not read room history; starting the session without it")
            return []
        return [
            HistoryMessage(
                sender=message.sender,
                body=message.body,
                event_id=message.event_id,
                sent_at=datetime.datetime.fromtimestamp(message.origin_server_ts / 1000, datetime.UTC),
            )
            for message in messages
        ]


class MatrixSessionSupervisor:
    """Provisions and replaces the session behind the live room (R3.1)."""

    def __init__(
        self,
        config: MatrixConfig,
        conversations: MatrixConversationStore,
        chat: ClaudeChatService,
        chat_store: ClaudeChatStore,
        notifications: ChatNotifications,
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
        conversation = await self._conversations.load(self._config.user_id)
        if conversation is None:
            return  # No room yet — nothing to serve, and nowhere to say so.

        # Before believing a live status, give a session whose holder has gone away the chance
        # to become an ended one. Otherwise this method reads `responding` off a row nobody is
        # working on and returns satisfied, which is how a room stops being answered without
        # anything reporting a failure.
        await self._chat_store.expire_stale_leases()

        outcome = (
            await self._chat_store.outcome(conversation.session_id) if conversation.session_id is not None else None
        )
        status = outcome.status if outcome is not None else None
        if status in LIVE_SESSION_STATUSES:
            await self._report(str(status), f"session {conversation.session_id} is {status}")
            return

        if conversation.session_id is not None:
            # With the reason, not just the status. Every path that ends a session records a
            # specific sentence in `error` — which replica went away, what the runtime failed
            # with, that the runner disconnected — and the room used to be told only `failed`,
            # so the one place an operator was looking held the least informative version of
            # what the console knew.
            reason = f" — {outcome.error}" if outcome is not None and outcome.error else ""
            await self._report(
                f"ended:{status}",
                f"session {conversation.session_id} ended ({status or 'gone'}){reason}; starting a new one",
            )
            # The claim may already be gone — `handle_runner` deletes it on the way out — so
            # this is the idempotent sweep rather than a targeted delete.
            await self._conversations.set_session(self._config.user_id, None)
            await self._chat.reconcile_terminal_claims()

        session = await self._chat.create(await self._operator_id(), MatrixSession(room_id=conversation.room_id))
        await self._conversations.set_session(self._config.user_id, session.session_id)
        self._last_announced = ChatSessionStatus.PROVISIONING
        await self._announce(f"provisioning a sandbox · session {session.session_id}")
        logger.info("Matrix: provisioned session %s for room %s", session.session_id, conversation.room_id)

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
        conversation = await self._conversations.load(self._config.user_id)
        if conversation is None or conversation.session_id is None:
            await asyncio.sleep(SUPERVISE_INTERVAL.total_seconds())
            return
        await self._notifications.wait(
            ChatEventKind.UPDATE, conversation.session_id, timeout_seconds=SUPERVISE_INTERVAL.total_seconds()
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
