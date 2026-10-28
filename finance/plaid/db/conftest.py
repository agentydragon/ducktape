"""Postgres-backed link storage shared by the plaid db tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable

import pytest

from finance.plaid.db.link_store import PlaidLinkStorage
from finance.plaid.db.models import async_session_factory
from finance.plaid.db.testing import db_url, plaid_database_template
from util.testing.postgres_fixtures import (
    database_clones,
    database_names,
    engine,
    postgres_admin_url,
    postgres_container,
    postgres_image,
)


@pytest.fixture
async def storage(db_url: str) -> AsyncGenerator[PlaidLinkStorage]:
    engine, sessions = async_session_factory(db_url)
    store = PlaidLinkStorage(sessions, engine)
    try:
        yield store
    finally:
        await store.close()


@pytest.fixture
def add_link(storage: PlaidLinkStorage) -> Callable[[str], Awaitable[None]]:
    async def add(item_id: str) -> None:
        await storage.upsert_link(
            item_id=item_id,
            access_token_secret=f"{item_id}-token",
            products_requested=["investments"],
            institution_id="ins_investments",
            institution_name="Investment Test",
            label=None,
        )

    return add
