"""Verify concurrent Plaid startup initializes the current schema once."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager

import pytest_bazel
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from finance.plaid.db.link_store import PlaidLinkStorage
from util.db_url import render_postgresql_url
from util.testing.postgres import DatabaseNames, create_temporary_database


@contextmanager
def _connection(db_url: str) -> Iterator[Connection]:
    with ExitStack() as resources:
        engine = create_engine(db_url)
        resources.callback(engine.dispose)
        yield resources.enter_context(engine.connect())


async def test_concurrent_initialization_serializes_migrations(
    postgres_admin_url: str, database_names: DatabaseNames
) -> None:
    with create_temporary_database(postgres_admin_url, database_names()) as db_url:
        async_url = render_postgresql_url(db_url, "postgresql+asyncpg")
        stores = await asyncio.gather(PlaidLinkStorage.initialize(async_url), PlaidLinkStorage.initialize(async_url))
        try:
            with _connection(db_url) as connection:
                assert connection.scalar(text("SELECT count(*) FROM holding_captures")) == 0
        finally:
            await asyncio.gather(*(store.close() for store in stores))


if __name__ == "__main__":
    pytest_bazel.main()
