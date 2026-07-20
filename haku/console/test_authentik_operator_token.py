"""Tests for PostgresAuthentikOperatorTokenStore: login capture, fresh read, refresh, and misses.

Postgres-backed (requires_docker); respx mocks the Authentik token endpoint for the refresh path.
"""

from __future__ import annotations

import datetime
from uuid import UUID

import httpx
import pytest
import pytest_bazel
import respx

from haku.console.authentik_operator_token import PostgresAuthentikOperatorTokenStore
from haku.console.conftest import console_sessions, operator_identity_store
from haku.console.oauth_token_state import PostgresOAuthTokenStateStore

ISSUER = "https://auth.test/application/o/haku-console/"
# The store derives this endpoint from ISSUER (strips the provider slug); respx mocks it.
TOKEN_ENDPOINT = "https://auth.test/application/o/token/"


def _utc(offset_seconds: int) -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=offset_seconds)


@pytest.fixture
def _env(migrated_db_url: str) -> tuple[PostgresAuthentikOperatorTokenStore, UUID]:
    identity_store = operator_identity_store(migrated_db_url)
    operator_id = identity_store.resolve_configured_external_user_key("op-hostexec")
    sessions = console_sessions(migrated_db_url)
    store = PostgresAuthentikOperatorTokenStore(
        sessions,
        operator_identity_store=identity_store,
        token_states=PostgresOAuthTokenStateStore(sessions, operator_identity_store=identity_store),
        client_id="op-client",
        client_secret="op-secret",
        issuer=ISSUER,
    )
    return store, operator_id


@pytest.fixture
def store(_env: tuple[PostgresAuthentikOperatorTokenStore, UUID]) -> PostgresAuthentikOperatorTokenStore:
    return _env[0]


@pytest.fixture
def operator_id(_env: tuple[PostgresAuthentikOperatorTokenStore, UUID]) -> UUID:
    return _env[1]


async def test_stores_and_reads_fresh_token(store: PostgresAuthentikOperatorTokenStore, operator_id: UUID) -> None:
    store.store_login_token(
        operator_id=operator_id,
        access_token="at-1",
        refresh_token="rt-1",
        token_type="Bearer",
        scope="openid",
        expires_at=_utc(3600),
    )
    assert await store.access_token_for(operator_id=operator_id) == "at-1"


async def test_missing_token_returns_none(store: PostgresAuthentikOperatorTokenStore, operator_id: UUID) -> None:
    # Active operator, no stored token yet.
    assert await store.access_token_for(operator_id=operator_id) is None


async def test_refreshes_expired_token(store: PostgresAuthentikOperatorTokenStore, operator_id: UUID) -> None:
    store.store_login_token(
        operator_id=operator_id,
        access_token="at-old",
        refresh_token="rt-old",
        token_type="Bearer",
        scope="openid",
        expires_at=_utc(-10),
    )
    with respx.mock:
        route = respx.post(TOKEN_ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json={"access_token": "at-new", "refresh_token": "rt-new", "token_type": "Bearer", "expires_in": 3600},
            )
        )
        assert await store.access_token_for(operator_id=operator_id) == "at-new"
    body = route.calls.last.request.content.decode()
    assert "grant_type=refresh_token" in body
    assert "refresh_token=rt-old" in body
    # The refreshed token is persisted, so a later read serves it without another refresh (outside mock).
    assert await store.access_token_for(operator_id=operator_id) == "at-new"


async def test_expired_without_refresh_token_returns_none(
    store: PostgresAuthentikOperatorTokenStore, operator_id: UUID
) -> None:
    store.store_login_token(
        operator_id=operator_id,
        access_token="at-old",
        refresh_token=None,
        token_type="Bearer",
        scope="openid",
        expires_at=_utc(-10),
    )
    assert await store.access_token_for(operator_id=operator_id) is None


async def test_relogin_replaces_token(store: PostgresAuthentikOperatorTokenStore, operator_id: UUID) -> None:
    for access in ("at-1", "at-2"):
        store.store_login_token(
            operator_id=operator_id,
            access_token=access,
            refresh_token="rt",
            token_type="Bearer",
            scope="openid",
            expires_at=_utc(3600),
        )
    assert await store.access_token_for(operator_id=operator_id) == "at-2"


if __name__ == "__main__":
    pytest_bazel.main()
