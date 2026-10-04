"""Owned LISTEN connection; notifications invalidate durable state rather than replace it."""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
from sqlalchemy.engine import URL
from tenacity import AsyncRetrying, RetryCallState, retry_if_exception_type, wait_exponential

logger = logging.getLogger(__name__)
CONNECTION_ERRORS = (OSError, TimeoutError, asyncpg.PostgresConnectionError, asyncpg.CannotConnectNowError)


class PostgresListener:
    """Adapters own payload parsing/fanout; invalidate readers on both connection loss and registration."""

    def __init__(
        self,
        url: URL,
        *,
        channels: tuple[str, ...],
        application_name: str,
        notified: Callable[[str, object], None],
        invalidated: Callable[[], None],
    ) -> None:
        if not channels:
            raise ValueError("PostgreSQL listener needs at least one channel")
        self._dsn = url.set(drivername="postgresql").render_as_string(hide_password=False)
        self._channels = channels
        self._application_name = application_name
        self._notified = notified
        self._invalidated = invalidated
        self._connection: asyncpg.Connection[Any] | None = None
        self._lost = asyncio.Event()
        self._owned = False
        self._generation = 0

    @property
    def connected(self) -> bool:
        return self._connection is not None and not self._connection.is_closed() and not self._lost.is_set()

    @property
    def generation(self) -> int:
        """Successful connection registrations; consumers can fence a wait across reconnects."""
        return self._generation

    async def wait_until_disconnected(self) -> None:
        await self._lost.wait()

    async def _connect(self) -> None:
        connection = await asyncpg.connect(
            self._dsn, timeout=10, server_settings={"application_name": self._application_name}
        )
        try:
            connection.add_termination_listener(self._terminated)
            for channel in self._channels:
                await connection.add_listener(channel, self._receive)
            if connection.is_closed():
                raise ConnectionError("PostgreSQL listener disconnected during startup")
        except BaseException:
            await connection.close(timeout=2)
            raise
        self._connection = connection
        self._lost.clear()
        self._generation += 1
        # LISTEN first, then catch up: commits during startup/reconnect are only in durable state.
        self._invalidated()

    async def _disconnect(self) -> None:
        self._mark_disconnected()
        if self._connection is not None:
            connection, self._connection = self._connection, None
            await connection.close(timeout=2)

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[None]:
        """Own one connection without recovery, for callers with their own retry loop."""
        if self._owned:
            raise RuntimeError("PostgreSQL listener already started")
        self._owned = True
        try:
            await self._connect()
            yield
        finally:
            try:
                await self._disconnect()
            finally:
                self._owned = False

    @asynccontextmanager
    async def listen(self) -> AsyncIterator[None]:
        """Fail startup immediately; supervise recovery and close on every scope exit."""
        async with self.connection(), asyncio.TaskGroup() as tasks:
            task = tasks.create_task(self._recover(), name=self._application_name)
            try:
                yield
            finally:
                task.cancel()

    def _receive(self, _connection: object, _pid: int, channel: str, payload: object) -> None:
        self._notified(channel, payload)

    def _terminated(self, connection: object) -> None:
        # An old connection's queued termination callback must not invalidate its replacement.
        if connection is self._connection:
            self._mark_disconnected()

    def _mark_disconnected(self) -> None:
        if not self._lost.is_set():
            self._lost.set()
            self._invalidated()

    def _reconnect_failed(self, state: RetryCallState) -> None:
        assert state.outcome is not None
        error = state.outcome.exception()
        # Exception text can contain credentials. Log only the configured name, type and SQLSTATE.
        logger.warning(
            "PostgreSQL listener %s reconnect failed: type=%s sqlstate=%s attempt=%d",
            self._application_name,
            type(error).__name__,
            error.sqlstate if isinstance(error, asyncpg.PostgresError) else None,
            state.attempt_number,
        )

    async def _recover(self) -> None:
        while True:
            await self._lost.wait()
            await self._disconnect()
            # Back off after a lost connection as well as after unsuccessful reconnects.
            await asyncio.sleep(1)
            async for attempt in AsyncRetrying(
                retry=retry_if_exception_type(CONNECTION_ERRORS),
                wait=wait_exponential(min=1, max=30),
                before_sleep=self._reconnect_failed,
                reraise=True,
            ):
                with attempt:
                    await self._connect()
