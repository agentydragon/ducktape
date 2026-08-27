"""The layer-neutral Postgres LISTEN/NOTIFY wake transport.

A wake is a level-triggered "come and look": it names a scope, never carries the fact, and the
durable record stays the authority. This module owns the transport that carries one — the emit and
the listen halves — with nothing said about which layer a channel belongs to. A layer builds its
own `WakeListener` on its own channel with its own payload model (`session_wakes.py`,
`conversation_wakes.py`); the machinery here is written once and instantiated per layer, so two
listeners share the code without sharing a module or a connection.

**Deviation from a pooled connection:** one long-lived connection with a reconnect loop, rather
than borrowing from the SQLAlchemy pool per wait. Two problems go with the pooled shape — a
listener that dies takes its waiters with it, and a session-lifetime watcher holds a pool
connection for as long as it lives.

The driver is asyncpg, the same one the application's engine uses, so nothing in the console's
async path speaks two dialects. (psycopg remains for synchronous Alembic; see
<../database_migrate.py>.)

The notify half stays inside the caller's transaction (see `notify_raw`), because `pg_notify`
delivers on commit: emitting it anywhere else would announce work that a rollback then un-did.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from datetime import timedelta
from typing import Any

import asyncpg
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_RECONNECT_DELAY = timedelta(seconds=2)
_CONNECT_TIMEOUT_SECONDS = 10
_CLOSE_TIMEOUT_SECONDS = 2


def libpq_dsn(database_url: str) -> str:
    """Drop the SQLAlchemy driver suffix, which a direct driver connection does not take."""
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


async def notify_raw(db: AsyncSession, channel: str, payload: str) -> None:
    """One `pg_notify` inside the caller's transaction, so it fires on commit.

    The single emission every typed wake serializes into. Tests drive it directly with payloads the
    typed emitters cannot produce — a garbled envelope, a kind from a release that does not exist.
    """
    await db.execute(text("SELECT pg_notify(:channel, :payload)"), {"channel": channel, "payload": payload})


def _parse[M: BaseModel](model: type[M], payload: str) -> M | None:
    try:
        return model.model_validate_json(payload)
    except ValueError:
        # Pydantic's ValidationError is a ValueError. Not raised onward: this runs on asyncpg's
        # reader task, and one bad payload must not cost the connection every other consumer is
        # being woken through.
        logger.exception("wake notification carried an unreadable payload: %r", payload)
        return None


def _terminator(terminated: asyncio.Event) -> Callable[[object], None]:
    """Bind the event per connection; a bare lambda in the loop would close over the last one."""
    return lambda _connection: terminated.set()


class WakeListener[M: BaseModel]:
    """One LISTEN connection on a single channel, parsing each payload and driving a reconnect gap.

    Layer-neutral: the channel name, the payload model, what a wake means, and what a reconnect gap
    means are all the owning layer's. Each parsed payload is handed to *on_wake*; every (re)connect
    calls *on_reconnect*, because the notifications committed while the socket was down are gone and
    "re-check" is the only correct answer to a gap. Both callbacks run on asyncpg's reader task, so
    they must neither block nor await: record what arrived and do the work elsewhere.

    A payload that does not parse is logged and dropped rather than delivered (`_parse`), so one bad
    envelope cannot cost the connection every consumer it wakes.
    """

    def __init__(
        self,
        database_url: str,
        channel: str,
        model: type[M],
        on_wake: Callable[[M], None],
        on_reconnect: Callable[[], None],
        *,
        task_name: str,
    ) -> None:
        self._dsn = libpq_dsn(database_url)
        self._channel = channel
        self._model = model
        self._on_wake = on_wake
        self._on_reconnect = on_reconnect
        self._task_name = task_name
        self._task: asyncio.Task[None] | None = None
        self._listening = asyncio.Event()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._listen_loop(), name=self._task_name)
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

    def _on_notification(self, _connection: object, _pid: int, _channel: str, payload: object) -> None:
        """asyncpg dispatches on its reader task, so this must not block or await.

        The parameter types are asyncpg's, not ours: its `_Listener` protocol declares the payload
        as `object` and the connection as a union with the pool proxy, so narrowing either here
        would stop matching.
        """
        parsed = _parse(self._model, str(payload))
        if parsed is None:
            return
        self._on_wake(parsed)

    async def _listen_loop(self) -> None:
        while True:
            connection: asyncpg.Connection[Any] | None = None
            try:
                connection = await asyncpg.connect(self._dsn, timeout=_CONNECT_TIMEOUT_SECONDS)
                terminated = asyncio.Event()
                connection.add_termination_listener(_terminator(terminated))
                await connection.add_listener(self._channel, self._on_notification)
                self._listening.set()
                self._on_reconnect()
                await terminated.wait()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Logged rather than raised: a dropped listener must not end the loop, or
                # every waiter in the process silently stops being woken.
                logger.exception("wake notification listener on %r failed; reconnecting", self._channel)
            finally:
                self._listening.clear()
                if connection is not None:
                    with suppress(Exception):
                        await connection.close(timeout=_CLOSE_TIMEOUT_SECONDS)
            await asyncio.sleep(_RECONNECT_DELAY.total_seconds())
