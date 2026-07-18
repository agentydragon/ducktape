"""Tests for the per-Operator provider connection store and auth resolution (Google)."""

from __future__ import annotations

import datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from fastapi import HTTPException
from mcp.shared.auth import OAuthToken
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from haku.console import provider_connection as provider_connection_module
from haku.console.config import ProviderOAuthClientConfig
from haku.console.conftest import console_sessions, operator_identity_store
from haku.console.database_schema import ProviderConnection
from haku.console.mcp_config import McpServerEntry
from haku.console.provider_connection import PostgresProviderConnectionStore
from haku.console.provider_connection_registry import ProviderConnectionKind
from haku.console.tool_call_service import BackendAccountNotConnectedError, backend_auth_for_operator

GOOGLE = ProviderConnectionKind.GOOGLE
_CALLBACK = "https://haku.test/api/provider-connections/callback"


@pytest.fixture
def _store_env(migrated_db_url: str) -> tuple[PostgresProviderConnectionStore, UUID]:
    identity_store = operator_identity_store(migrated_db_url)
    operator_id = identity_store.resolve_configured_external_user_key("op-provider")
    store = PostgresProviderConnectionStore(
        console_sessions(migrated_db_url),
        operator_identity_store=identity_store,
        provider_clients={GOOGLE: ProviderOAuthClientConfig(client_id="client-123", client_secret=SecretStr("s3cret"))},
    )
    return store, operator_id


@pytest.fixture
def store(_store_env: tuple[PostgresProviderConnectionStore, UUID]) -> PostgresProviderConnectionStore:
    return _store_env[0]


@pytest.fixture
def operator_id(_store_env: tuple[PostgresProviderConnectionStore, UUID]) -> UUID:
    return _store_env[1]


async def _connect(
    store: PostgresProviderConnectionStore,
    operator_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
    *,
    access_token: str = "at-1",
    refresh_token: str | None = "rt-1",
    expires_in: int | None = 3600,
) -> None:
    flow = await store.connect_flow(provider=GOOGLE, operator_id=operator_id, public_base_url="https://haku.test")
    state = parse_qs(urlsplit(flow.authorization_url).query)["state"][0]

    async def fake_exchange(
        descriptor: Any, client: Any, *, code: str, redirect_uri: str, code_verifier: str
    ) -> OAuthToken:
        assert redirect_uri == _CALLBACK
        return OAuthToken(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type="Bearer",
            expires_in=expires_in,
            scope="s",
        )

    monkeypatch.setattr(provider_connection_module, "_exchange_code", fake_exchange)
    await store.complete_callback(state=state, code="auth-code", operator_id=operator_id)


async def test_connect_flow_builds_google_consent_url(
    store: PostgresProviderConnectionStore, operator_id: UUID
) -> None:
    flow = await store.connect_flow(provider=GOOGLE, operator_id=operator_id, public_base_url="https://haku.test")
    parsed = urlsplit(flow.authorization_url)
    query = parse_qs(parsed.query)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://accounts.google.com/o/oauth2/v2/auth"
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == [_CALLBACK]
    assert "https://www.googleapis.com/auth/gmail.modify" in query["scope"][0]


async def test_callback_persists_connection(
    store: PostgresProviderConnectionStore, operator_id: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _connect(store, operator_id, monkeypatch)
    assert await store.access_token_for(provider=GOOGLE, operator_id=operator_id) == "at-1"
    connections = store.list_statuses(operator_id=operator_id).connections
    assert [(c.provider, c.status) for c in connections] == [(GOOGLE, "connected")]


async def test_access_token_for_refreshes_when_stale_and_preserves_refresh_token(
    store: PostgresProviderConnectionStore, operator_id: UUID, migrated_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _connect(store, operator_id, monkeypatch, access_token="at-1", refresh_token="rt-1")

    engine = create_engine(migrated_db_url)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        row = session.get(ProviderConnection, (operator_id, GOOGLE))
        assert row is not None
        row.token_expires_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=1)
        revision_before = row.token_revision

    async def fake_refresh(descriptor: Any, client: Any, refresh_token: str) -> OAuthToken:
        assert refresh_token == "rt-1"
        # Google omits refresh_token on refresh — the store must keep the prior one.
        return OAuthToken(access_token="at-2", refresh_token=None, token_type="Bearer", expires_in=3600, scope="s")

    monkeypatch.setattr(provider_connection_module, "_refresh_token", fake_refresh)
    assert await store.access_token_for(provider=GOOGLE, operator_id=operator_id) == "at-2"

    with sessions.begin() as session:
        row = session.get(ProviderConnection, (operator_id, GOOGLE))
        assert row is not None
        assert row.token_revision == revision_before + 1
        assert row.refresh_token == "rt-1"
    engine.dispose()


async def test_disconnect_removes_connection(
    store: PostgresProviderConnectionStore, operator_id: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _connect(store, operator_id, monkeypatch)
    store.disconnect(provider=GOOGLE, operator_id=operator_id)
    assert await store.access_token_for(provider=GOOGLE, operator_id=operator_id) is None
    connections = store.list_statuses(operator_id=operator_id).connections
    assert [c.status for c in connections] == ["unconnected"]


async def test_connect_when_already_connected_conflicts(
    store: PostgresProviderConnectionStore, operator_id: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _connect(store, operator_id, monkeypatch)
    with pytest.raises(HTTPException) as excinfo:
        await store.connect_flow(provider=GOOGLE, operator_id=operator_id, public_base_url="https://haku.test")
    assert excinfo.value.status_code == 409


async def test_access_token_for_unconnected_is_none(store: PostgresProviderConnectionStore, operator_id: UUID) -> None:
    assert await store.access_token_for(provider=GOOGLE, operator_id=operator_id) is None


def _provider_store(token: str | None) -> Any:
    class _Store:
        async def access_token_for(self, *, provider: ProviderConnectionKind, operator_id: UUID) -> str | None:
            return token

    return _Store()


def _unconsulted_store() -> Any:
    """A token store the PROVIDER auth path must not consult — raises if the wrong mode reaches it."""

    class _Unconsulted:
        async def access_token_for(self, **kwargs: object) -> str | None:
            raise AssertionError("token store consulted for a server whose auth mode ignores it")

    return _Unconsulted()


async def test_backend_auth_resolves_provider_connection() -> None:
    server = McpServerEntry(id="gmail", provider_connection=GOOGLE)
    token = await backend_auth_for_operator(
        server=server,
        operator_id=uuid4(),
        oauth_store=_unconsulted_store(),
        provider_store=_provider_store("tok"),
        authentik_store=_unconsulted_store(),
    )
    assert token == "tok"


async def test_backend_auth_raises_when_provider_unconnected() -> None:
    server = McpServerEntry(id="gmail", provider_connection=GOOGLE)
    with pytest.raises(BackendAccountNotConnectedError):
        await backend_auth_for_operator(
            server=server,
            operator_id=uuid4(),
            oauth_store=_unconsulted_store(),
            provider_store=_provider_store(None),
            authentik_store=_unconsulted_store(),
        )


if __name__ == "__main__":
    pytest_bazel.main()
