"""Shared LISTEN lifecycle against real PostgreSQL, independent of service schemas."""

import asyncio
from collections.abc import Callable
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import asyncpg
import pytest
import pytest_bazel
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from agentplane.postgres.listener import PostgresListener


@pytest.fixture
async def changed() -> asyncio.Event:
    return asyncio.Event()


@pytest.fixture
async def listener(postgres_container: PostgresContainer, changed: asyncio.Event) -> PostgresListener:
    url = make_url(
        f"postgresql://postgres:postgres@{postgres_container.get_container_host_ip()}:"
        f"{postgres_container.get_exposed_port(5432)}/postgres"
    )
    return PostgresListener(
        url,
        channels=("listener_test_first", "listener_test_second"),
        application_name=f"listener-test-{uuid4().hex}",
        notified=lambda channel, payload: changed.set(),
        invalidated=changed.set,
    )


async def test_context_cleans_up_on_error_and_cancellation(listener: PostgresListener) -> None:
    with pytest.RaisesGroup(pytest.RaisesExc(ValueError, match="body failed")):
        async with listener.listen():
            assert listener.connected
            raise ValueError("body failed")
    assert not listener.connected
    entered = asyncio.Event()

    async def wait() -> None:
        async with listener.listen():
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(wait())
    try:
        async with asyncio.timeout(10):
            await entered.wait()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not listener.connected
    # Neither exceptional exit leaves a reconnect task or prevents subsequent scoped use.
    async with listener.listen():
        assert listener.connected
    assert not listener.connected


async def test_reconnect_invalidates_without_a_new_notification_and_logs_safe_diagnostics(
    listener: PostgresListener, changed: asyncio.Event, caplog: pytest.LogCaptureFixture
) -> None:
    async with listener.listen():
        assert changed.is_set()
        changed.clear()
        connect = listener._connect
        attempts = 0

        async def reconnect() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise asyncpg.CannotConnectNowError("secret-bearing connection details")
            await connect()

        assert listener._connection is not None
        old_connection = listener._connection
        with patch.object(listener, "_connect", side_effect=reconnect):
            old_connection.terminate()
            async with asyncio.timeout(10):
                await changed.wait()
                assert not listener.connected
                while attempts < 2 or not listener.connected:
                    changed.clear()
                    await changed.wait()
            assert attempts == 2
        # Late callbacks from the old connection cannot mark the replacement unavailable.
        changed.clear()
        listener._terminated(old_connection)
        assert listener.connected
        assert not changed.is_set()
    assert "CannotConnectNowError" in caplog.text
    assert "sqlstate=57P03" in caplog.text
    assert "secret-bearing" not in caplog.text


@pytest.mark.parametrize("error", [ValueError("programming error"), asyncpg.InvalidPasswordError("bad credentials")])
async def test_nontransient_reconnect_errors_escape_scope(listener: PostgresListener, error: Exception) -> None:
    with pytest.RaisesGroup(pytest.RaisesExc(type(error))):
        async with listener.listen():
            with patch.object(listener, "_connect", side_effect=error):
                assert listener._connection is not None
                listener._connection.terminate()
                async with asyncio.timeout(10):
                    await asyncio.Event().wait()
    assert not listener.connected


async def test_duplicate_start_does_not_close_active_listener(listener: PostgresListener) -> None:
    async with listener.listen():
        with pytest.raises(RuntimeError, match="already started"):
            async with listener.connection():
                pytest.fail("duplicate connection was accepted")
        with pytest.raises(RuntimeError, match="already started"):
            async with listener.listen():
                pytest.fail("duplicate scope was accepted")
        assert listener.connected
        assert listener._connection is not None
        listener._connection.terminate()
        async with asyncio.timeout(10):
            await listener.wait_until_disconnected()
        # Ownership includes the reconnect gap, even when there is no healthy connection.
        with pytest.raises(RuntimeError, match="already started"):
            async with listener.connection():
                pytest.fail("a second owner entered during recovery")


@pytest.mark.parametrize("error", [ValueError("registration failed"), asyncio.CancelledError()])
async def test_failed_listen_registration_closes_connection(listener: PostgresListener, error: BaseException) -> None:
    # The second registration fails after a real connection is already listening on the first.
    add_listener = asyncpg.Connection.add_listener
    connections: list[asyncpg.Connection[Any]] = []

    async def fail(
        connection: asyncpg.Connection[Any], channel: str, callback: Callable[[object, int, str, object], None]
    ) -> None:
        assert not listener.connected
        if channel == "listener_test_second":
            connections.append(connection)
            raise error
        await add_listener(connection, channel, callback)

    with patch.object(asyncpg.Connection, "add_listener", new=fail), pytest.raises(type(error)):
        async with listener.connection():
            pytest.fail("failed registration entered the scope")
    assert len(connections) == 1
    assert connections[0].is_closed()
    assert not listener.connected
    async with listener.listen():
        assert listener.connected


if __name__ == "__main__":
    pytest_bazel.main()
