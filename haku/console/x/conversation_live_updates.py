"""Conversation changes, delivered to open tabs as console-socket invalidations.

<../notifications/console_events.py>'s contract applied to conversations: the socket the shell already holds says
*which conversation* moved, and the page refetches.

**Where the publish happens.** Nowhere new. Every write that changes what a conversation shows
already emits a wake on the conversation channel inside the transaction that makes the change
(`notify_conversation` fires on commit), so a change that rolled back never announces itself. This
module only listens — the same registration every conversation subscriber holds
(`ConversationWakes.watch`), with the console socket as this consumer's transport.

**No second channel and no second NOTIFY.** `LISTEN` is broadcast, so every replica's
`ConversationWakes` already hears every `conversation_wakes` notification. Each replica
therefore turns what it hears into sends on the console sockets **it** holds; relaying through
`ConsoleEventHub.broadcast` would `NOTIFY` a second time for one change and deliver it twice.

**Every kind invalidates.** A wake means "re-check", never "act", so waking on a kind this release
does not know is safe — and filtering to `update` would be wrong besides: a prompt queued into a
conversation with no open session emits only `runtime_demand`, and the queued prompt is exactly a
row the list shows.

**Coalescing is load-bearing, not tidiness.** `update` fires per stream delta — hundreds in a turn
— and each event costs every listening tab a refetch. So the fan-out is the coalescing point: one
event per conversation per `COALESCE_WINDOW`, however many changes landed inside it. The client
half of the same discipline is one refresh in flight per page.

**Lossy on purpose.** A missed notification (a listener reconnect, a replica that was mid-roll)
delays a refresh and never loses one, because the browser also resyncs on a bounded timer and on
every socket reconnect. That is what lets this be a set of ids rather than a queue — and why a
reconnect's `RecheckHeld` forwards nothing: it names no conversation, and the tabs' own resync
already answers "re-check everything".
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.database_schema import Conversation
from haku.console.notifications.console_events import ConsoleEventHub, ConversationChangedEvent
from haku.console.x.conversation_wakes import ConversationWakeEvent, ConversationWakes, RecheckHeld

logger = logging.getLogger(__name__)

# How long changes to one conversation pile up before that conversation's tabs are told once.
#
# The floor is set by what an update costs: a refetch reads a whole page, so a window near zero
# would hand a streaming turn's per-delta notifications straight through to every open tab. Half a
# second bounds that at two refetches per second per tab while staying under the second or so at
# which an arriving message stops reading as live. Below that the client's own one-refresh-in-flight
# rule and the server's response time decide the real rate anyway.
COALESCE_WINDOW = timedelta(milliseconds=500)


class ConversationLiveUpdates:
    """Turns this replica's conversation wakes into per-conversation console invalidations."""

    def __init__(
        self,
        notifications: ConversationWakes,
        hub: ConsoleEventHub,
        db_sessions: async_sessionmaker[AsyncSession],
        *,
        window: timedelta = COALESCE_WINDOW,
    ) -> None:
        self._notifications = notifications
        self._hub = hub
        self._db_sessions = db_sessions
        self._window = window
        self._changed: set[UUID] = set()
        self._pending = asyncio.Event()
        self._operators: dict[UUID, UUID] = {}

    @contextlib.asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        with self._notifications.watch(self._record):
            publishing = asyncio.create_task(self._publish_loop())
            try:
                yield
            finally:
                publishing.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await publishing

    def _record(self, wake: ConversationWakeEvent | RecheckHeld) -> None:
        """Note that this conversation changed. Runs on the listener's reader task: no awaiting."""
        match wake:
            case ConversationWakeEvent(conversation_id=conversation_id):
                self._changed.add(conversation_id)
                self._pending.set()
            case RecheckHeld():
                # Names no conversation, so there is nothing to route; every tab re-reads on its
                # own bounded timer and on socket reconnect, which is the same answer.
                pass

    async def _publish_loop(self) -> None:
        while True:
            await self._pending.wait()
            # Sleep first, so the changes that arrive during the window join this flush rather
            # than each buying one of their own.
            await asyncio.sleep(self._window.total_seconds())
            self._pending.clear()
            changed, self._changed = self._changed, set()
            try:
                await self._publish(changed)
            except Exception:
                # An invalidation is lossy by construction, so one that fails must not take the
                # loop — and with it every later conversation — down with it.
                logger.exception("failed to publish conversation invalidations for %d conversations", len(changed))

    async def _publish(self, changed: set[UUID]) -> None:
        for conversation_id in changed:
            operator_id = await self._operator_of(conversation_id)
            if operator_id is None:
                # The id came off a broadcast channel, so it can name a conversation this database
                # no longer has. There is no tab to tell, and nothing to route it to.
                logger.warning("a conversation wake named an unknown conversation: %s", conversation_id)
                continue
            await self._hub.deliver_locally(operator_id, ConversationChangedEvent(conversation_id=conversation_id))

    async def _operator_of(self, conversation_id: UUID) -> UUID | None:
        """Whose tabs this conversation's invalidation goes to.

        `ConversationWakeEvent` carries no operator and the hub routes by one, so this lookup joins
        them — once per conversation rather than once per wake, which is the other reason the
        window above matters. The cache needs no invalidation: a conversation's owner is written
        when the row is created and never updated.
        """
        if (cached := self._operators.get(conversation_id)) is not None:
            return cached
        async with self._db_sessions() as db:
            operator_id: UUID | None = await db.scalar(
                select(Conversation.operator_id).where(Conversation.conversation_id == conversation_id)
            )
        if operator_id is not None:
            self._operators[conversation_id] = operator_id
        return operator_id
