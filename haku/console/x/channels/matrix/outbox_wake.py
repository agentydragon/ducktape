"""The outbox's own wake wire: the enqueue's transaction telling the drain to look.

Channel-internal on purpose. An outbox row is this channel's delivery state, not a conversation
fact, so its wake rides a channel of the channel's own rather than the conversation wire — the
layers above have no reason to hear it, and nothing here reaches them. What it keeps from the
shared wake discipline (<../../pg_wake.py>, whose `notify_raw` is the one `pg_notify`
emission this reuses):

- **The emission stays inside the inserting transaction.** `pg_notify` delivers on commit, so the
  wake cannot precede the row it announces — which is the whole reason the drain can wait on it:
  every earlier signal (the conversation wake that made the enqueueing subscriber read) fires
  before the row exists, possibly heard on another replica.
- **The wire carries nothing.** A wake says to look; the table stays the authority, and the drain
  re-reads its own binding on every pass, so there is no payload for either end to disagree on
  across a roll. Anything added later must be ignorable.
- **Delivery is at-most-once.** A reconnect wakes every registration — the notifications committed
  during the gap are gone, and "look at the table" is always the right answer — and the drain's
  own backstop covers a listener that dies without saying so.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import timedelta
from typing import Any

import asyncpg
from sqlalchemy.ext.asyncio import AsyncSession

from haku.console.x.pg_wake import libpq_dsn, notify_raw

logger = logging.getLogger(__name__)

CHANNEL = "matrix_outbox_wakes"

_RECONNECT_DELAY = timedelta(seconds=2)
_CONNECT_TIMEOUT_SECONDS = 10
_CLOSE_TIMEOUT_SECONDS = 2


async def notify_outbox(db: AsyncSession) -> None:
    """Emit the drain's wake inside the caller's transaction, so it fires on commit."""
    await notify_raw(db, CHANNEL, "")


def _terminator(terminated: asyncio.Event) -> Callable[[object], None]:
    """Bind the event per connection; a bare lambda in the loop would close over the last one."""
    return lambda _connection: terminated.set()


class OutboxWakes:
    """One LISTEN connection on the outbox wire, waking this replica's registrations."""

    def __init__(self, database_url: str):
        self._dsn = libpq_dsn(database_url)
        self._watchers: set[Callable[[], None]] = set()
        self._task: asyncio.Task[None] | None = None
        self._listening = asyncio.Event()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._listen_loop(), name="matrix-outbox-wakes")
        # Wait for the first LISTEN so a caller that subscribes immediately cannot miss a
        # notification emitted between construction and the socket being live.
        with suppress(TimeoutError):
            async with asyncio.timeout(_CONNECT_TIMEOUT_SECONDS):
                await self._listening.wait()

    async def aclose(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    @contextmanager
    def watch(self, on_wake: Callable[[], None]) -> Iterator[None]:
        """Call *on_wake* on every wake this replica hears, and on every reconnect.

        It runs on asyncpg's reader task, so it must neither block nor await: record that there is
        work and do it elsewhere.
        """
        self._watchers.add(on_wake)
        try:
            yield
        finally:
            self._watchers.discard(on_wake)

    def _wake_everyone(self) -> None:
        for watcher in self._watchers:
            watcher()

    def _on_notification(self, _connection: object, _pid: int, _channel: str, _payload: object) -> None:
        """asyncpg dispatches on its reader task, so this must not block or await.

        The payload is deliberately unread: the wire carries nothing, and the table is the
        authority a woken drain goes and asks.
        """
        self._wake_everyone()

    async def _listen_loop(self) -> None:
        while True:
            connection: asyncpg.Connection[Any] | None = None
            try:
                connection = await asyncpg.connect(self._dsn, timeout=_CONNECT_TIMEOUT_SECONDS)
                terminated = asyncio.Event()
                connection.add_termination_listener(_terminator(terminated))
                await connection.add_listener(CHANNEL, self._on_notification)
                self._listening.set()
                # Notifications committed while reconnecting are gone; "look at the table" is the
                # only correct wake, and it is the only wake this wire has.
                self._wake_everyone()
                await terminated.wait()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Logged rather than raised: a dropped listener must not end the loop, or the
                # drain silently stops being woken until its backstop.
                logger.exception("outbox wake listener failed; reconnecting")
            finally:
                self._listening.clear()
                if connection is not None:
                    with suppress(Exception):
                        await connection.close(timeout=_CLOSE_TIMEOUT_SECONDS)
            await asyncio.sleep(_RECONNECT_DELAY.total_seconds())
