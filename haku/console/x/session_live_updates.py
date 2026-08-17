"""Session changes, delivered to open tabs as console-socket invalidations.

<../console_events.py>'s contract applied to sessions: the socket the shell already holds says
*which session* changed, and the page refetches.

**Where the publish happens.** Nowhere new. Every write that changes a session already emits
`SessionEventKind.UPDATE` inside the transaction that makes the change (`notify` fires on commit),
so a change that rolled back never announces itself. This module only listens.

**No second channel and no second NOTIFY.** `LISTEN` is broadcast, so every replica's
`SessionNotifications` already hears every `session_events` notification. Each replica therefore
turns what it hears into sends on the console sockets **it** holds; relaying through
`ConsoleEventHub.broadcast` would `NOTIFY` a second time for one change and deliver it twice.

**Coalescing is load-bearing, not tidiness.** `UPDATE` fires per stream delta — hundreds in a turn
— and each event costs every open tab a full transcript refetch. So the fan-out is the coalescing
point: one event per session per `COALESCE_WINDOW`, however many changes landed inside it. The
client half of the same discipline is one refresh in flight per page.

**Lossy on purpose.** A missed notification (a listener reconnect, a replica that was mid-roll)
delays a refresh and never loses one, because the browser also resyncs on a bounded timer and on
every socket reconnect. That is what lets this be a set of ids rather than a queue.
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

from haku.console.console_events import ConsoleEventHub, SessionChangedEvent
from haku.console.database_schema import Session
from haku.console.x.session_notifications import SessionEventKind, SessionNotifications

logger = logging.getLogger(__name__)

# How long changes to one session pile up before that session's tabs are told once.
#
# The floor is set by what an update costs: a refetch reads the whole transcript, so a window near
# zero would hand a streaming turn's per-delta notifications straight through to every open tab.
# Half a second bounds that at two refetches per second per tab while staying under the second or
# so at which an arriving message stops reading as live. Below that the client's own
# one-refresh-in-flight rule and the server's response time decide the real rate anyway.
COALESCE_WINDOW = timedelta(milliseconds=500)


class SessionLiveUpdates:
    """Turns this replica's session-update notifications into per-session console invalidations."""

    def __init__(
        self,
        notifications: SessionNotifications,
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
        with self._notifications.watch(SessionEventKind.UPDATE, self._record):
            publishing = asyncio.create_task(self._publish_loop())
            try:
                yield
            finally:
                publishing.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await publishing

    def _record(self, session_id: UUID) -> None:
        """Note that this session changed. Runs on the listener's reader task: no awaiting here."""
        self._changed.add(session_id)
        self._pending.set()

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
                # loop — and with it every later session — down with it.
                logger.exception("failed to publish session invalidations for %d sessions", len(changed))

    async def _publish(self, changed: set[UUID]) -> None:
        for session_id in changed:
            operator_id = await self._operator_of(session_id)
            if operator_id is None:
                # The id came off a broadcast channel, so it can name a session this database no
                # longer has. There is no tab to tell, and nothing to route it to.
                logger.warning("a session update named an unknown session: %s", session_id)
                continue
            await self._hub.deliver_locally(operator_id, SessionChangedEvent(session_id=session_id))

    async def _operator_of(self, session_id: UUID) -> UUID | None:
        """Whose tabs this session's invalidation goes to.

        `SessionEvent` carries no operator and the hub routes by one, so this lookup joins them —
        once per session rather than once per event, which is the other reason the window above
        matters. The cache needs no invalidation: a session's owner is written when the row is
        created and never updated.
        """
        if (cached := self._operators.get(session_id)) is not None:
            return cached
        async with self._db_sessions() as db:
            operator_id: UUID | None = await db.scalar(
                select(Session.operator_id).where(Session.session_id == session_id)
            )
        if operator_id is not None:
            self._operators[session_id] = operator_id
        return operator_id
