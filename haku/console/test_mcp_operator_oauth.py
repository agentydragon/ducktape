"""Tests for the operator OAuth helpers and store (haku.console.mcp_operator_oauth)."""

from __future__ import annotations

import asyncio
import datetime
from collections.abc import Callable, Generator
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_bazel
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from haku.console.conftest import TEST_OPERATOR_IDENTITY, TEST_OPERATOR_OIDC, console_sessions
from haku.console.database_schema import McpOperatorOAuthAssociation, McpOperatorOAuthFlow, Operator
from haku.console.mcp_config import (
    DynamicOAuthClientRegistration,
    McpServerEntry,
    RemoteMcpBackend,
    RemoteServerOAuthAuth,
)
from haku.console.mcp_operator_oauth import (
    McpOperatorAuthConnected,
    McpOperatorAuthDegraded,
    PostgresMcpOperatorOAuthStore,
    _BuiltOperatorOAuthFlow,
    _oauth_callback_response,
    _OperatorOAuthTokenClient,
    _refresh_operator_oauth_token,
)
from haku.console.oauth_token_state import (
    OAuthRefreshBlockedError,
    OAuthRefreshError,
    OAuthRefreshFailureAction,
    OAuthRefreshFailureKind,
    PostgresOAuthTokenStateStore,
    new_oauth_token_state,
)
from haku.console.operator_identity import InactiveOperatorError, OperatorIdentityTrust, OperatorStatus
from haku.console.operator_identity_store import PostgresOperatorIdentityStore


@pytest.fixture
def migrated_engine(migrated_db_url: str) -> Generator[Engine]:
    engine = create_engine(migrated_db_url)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def oauth_store_for(migrated_db_url: str) -> Callable[[str], tuple[PostgresMcpOperatorOAuthStore, UUID]]:
    """Build a `(store, operator_id)` pair for a configured external-user key, resolving the operator
    into the same migrated database the store reads and writes."""

    def build(external_user_key: str) -> tuple[PostgresMcpOperatorOAuthStore, UUID]:
        sessions = console_sessions(migrated_db_url)
        identity_store = PostgresOperatorIdentityStore(
            sessions,
            OperatorIdentityTrust(
                trust_domain=TEST_OPERATOR_IDENTITY.trust_domain, trusted_issuers=frozenset({TEST_OPERATOR_OIDC.issuer})
            ),
        )
        operator_id = identity_store.resolve_configured_external_user_key(external_user_key)
        store = PostgresMcpOperatorOAuthStore(
            sessions,
            operator_identity_store=identity_store,
            token_states=PostgresOAuthTokenStateStore(sessions, operator_identity_store=identity_store),
        )
        return store, operator_id

    return build


def _disable_operator(engine: Engine, operator_id: UUID) -> None:
    with sessionmaker(engine)() as session, session.begin():
        operator = session.get(Operator, operator_id)
        assert operator is not None
        operator.status = OperatorStatus.DISABLED
        operator.updated_at = datetime.datetime.now(datetime.UTC)


def _dynamic_remote_oauth_server() -> McpServerEntry:
    return McpServerEntry(
        id="grocy-sf",
        backend=RemoteMcpBackend(
            url="https://grocy.test/mcp",
            auth=RemoteServerOAuthAuth(client_registration=DynamicOAuthClientRegistration()),
        ),
    )


def test_callback_response_autoescapes_content_and_locks_down_browser_capabilities() -> None:
    hostile_message = '<img src=x onerror="alert(document.cookie)">'
    responses = [
        _oauth_callback_response(False, hostile_message, status_code=400),
        _oauth_callback_response(False, hostile_message, status_code=400),
    ]
    nonces: list[str] = []

    for response in responses:
        body = response.body.decode()
        csp = response.headers["Content-Security-Policy"]
        style_directive = csp.split("; ")[-1]
        nonce = style_directive.removeprefix("style-src 'nonce-").removesuffix("'")
        nonces.append(nonce)

        assert response.status_code == 400
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert csp == (
            "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
            f"script-src 'none'; style-src 'nonce-{nonce}'"
        )
        assert "unsafe-inline" not in csp
        assert f'<style nonce="{nonce}">' in body
        assert hostile_message not in body
        assert "&lt;img src=x onerror=&#34;alert(document.cookie)&#34;&gt;" in body

    assert nonces[0] != nonces[1]


async def test_refresh_read_timeout_is_classified_as_ambiguous_and_uses_configured_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TimeoutClient:
        async def __aenter__(self) -> TimeoutClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, url: str, **_kwargs: object) -> httpx.Response:
            request = httpx.Request("POST", url)
            raise httpx.ReadTimeout("late response", request=request)

    def client(*, timeout: float) -> TimeoutClient:
        assert timeout == 37.0
        return TimeoutClient()

    monkeypatch.setattr("haku.console.mcp_operator_oauth.httpx.AsyncClient", client)
    with pytest.raises(OAuthRefreshError) as raised:
        await _refresh_operator_oauth_token(
            _OperatorOAuthTokenClient(
                client_id="client", token_endpoint="https://auth.test/token", timeout_seconds=37.0
            ),
            "refresh-token",
        )

    assert raised.value.kind == OAuthRefreshFailureKind.OUTCOME_UNKNOWN
    assert raised.value.action == OAuthRefreshFailureAction.RECONNECT
    assert str(raised.value) == "MCP OAuth token refresh timed out after 37 seconds"


async def test_operator_oauth_callback_rechecks_operator_after_token_exchange(
    migrated_engine: Engine,
    oauth_store_for: Callable[[str], tuple[PostgresMcpOperatorOAuthStore, UUID]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oauth_store, operator_id = oauth_store_for("callback-race-operator")
    now = datetime.datetime.now(datetime.UTC)
    with sessionmaker(migrated_engine)() as session, session.begin():
        session.add(
            McpOperatorOAuthFlow(
                state="callback-race-state",
                server_id="grocy-sf",
                operator_id=operator_id,
                created_at=now,
                expires_at=now + datetime.timedelta(minutes=10),
                redirect_uri="https://haku.test/api/mcp/operator-auth/callback",
                code_verifier="verifier",
                client_id="client-id",
                token_endpoint="https://auth.test/token",
            )
        )

    async def exchange_after_disable(_flow: object, _code: str, *, timeout_seconds: float) -> OAuthToken:
        assert timeout_seconds == 30.0
        _disable_operator(migrated_engine, operator_id)
        return OAuthToken(access_token="must-not-be-persisted")

    monkeypatch.setattr("haku.console.mcp_operator_oauth._exchange_operator_oauth_code", exchange_after_disable)

    with pytest.raises(InactiveOperatorError):
        await oauth_store.complete_callback(
            state="callback-race-state",
            code="authorization-code",
            operator_id=operator_id,
            username="operator@example.com",
        )

    with sessionmaker(migrated_engine)() as session:
        assert session.get(McpOperatorOAuthAssociation, ("grocy-sf", operator_id)) is None


async def test_operator_oauth_connect_rechecks_operator_after_discovery_and_dcr(
    migrated_engine: Engine,
    oauth_store_for: Callable[[str], tuple[PostgresMcpOperatorOAuthStore, UUID]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oauth_store, operator_id = oauth_store_for("connect-race-operator")
    server = _dynamic_remote_oauth_server()
    now = datetime.datetime.now(datetime.UTC)

    async def build_flow_after_disable(_server: McpServerEntry, _public_base_url: str) -> _BuiltOperatorOAuthFlow:
        _disable_operator(migrated_engine, operator_id)
        return _BuiltOperatorOAuthFlow(
            state="connect-race-state",
            authorization_url="https://auth.test/authorize?state=connect-race-state",
            expires_at=now + datetime.timedelta(minutes=10),
            redirect_uri="https://haku.test/api/mcp/operator-auth/callback",
            code_verifier="verifier",
            client_info=OAuthClientInformationFull(
                client_id="dynamic-client", redirect_uris=["https://haku.test/api/mcp/operator-auth/callback"]
            ),
            token_endpoint="https://auth.test/token",
        )

    monkeypatch.setattr("haku.console.mcp_operator_oauth._build_operator_oauth_flow", build_flow_after_disable)

    with pytest.raises(InactiveOperatorError):
        await oauth_store.connect_flow(server=server, operator_id=operator_id, public_base_url="https://haku.test")

    with sessionmaker(migrated_engine)() as session:
        assert session.get(McpOperatorOAuthFlow, "connect-race-state") is None


async def test_operator_oauth_refresh_rechecks_operator_before_write_and_return(
    migrated_engine: Engine,
    oauth_store_for: Callable[[str], tuple[PostgresMcpOperatorOAuthStore, UUID]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oauth_store, operator_id = oauth_store_for("refresh-race-operator")
    server = _dynamic_remote_oauth_server()
    now = datetime.datetime.now(datetime.UTC)
    with sessionmaker(migrated_engine)() as session, session.begin():
        session.add(
            McpOperatorOAuthAssociation(
                server_id=server.id,
                operator_id=operator_id,
                created_at=now,
                client_id="client-id",
                token_endpoint="https://auth.test/token",
                token_state=new_oauth_token_state(
                    operator_id=operator_id,
                    access_token="old-expired-token",
                    refresh_token="refresh-token",
                    token_type="Bearer",
                    scope=None,
                    expires_at=now - datetime.timedelta(minutes=1),
                    now=now,
                ),
            )
        )

    async def refresh_after_disable(_client: object, _refresh_token: str) -> OAuthToken:
        _disable_operator(migrated_engine, operator_id)
        return OAuthToken(
            access_token="must-not-be-written-or-returned", refresh_token="must-not-be-written", expires_in=3600
        )

    monkeypatch.setattr("haku.console.mcp_operator_oauth._refresh_operator_oauth_token", refresh_after_disable)

    with pytest.raises(InactiveOperatorError):
        await oauth_store.access_token_for(server=server, operator_id=operator_id)

    with sessionmaker(migrated_engine)() as session:
        association = session.get(McpOperatorOAuthAssociation, (server.id, operator_id))
        assert association is not None
        assert association.token_state.access_token == "old-expired-token"
        assert association.token_state.refresh_token == "refresh-token"


async def test_operator_oauth_refresh_does_not_overwrite_concurrent_reconnect(
    migrated_engine: Engine,
    oauth_store_for: Callable[[str], tuple[PostgresMcpOperatorOAuthStore, UUID]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oauth_store, operator_id = oauth_store_for("refresh-reconnect-race")
    server = _dynamic_remote_oauth_server()
    now = datetime.datetime.now(datetime.UTC)
    replacement_association_id = uuid4()
    with sessionmaker(migrated_engine)() as session, session.begin():
        session.add(
            McpOperatorOAuthAssociation(
                server_id=server.id,
                operator_id=operator_id,
                created_at=now,
                client_id="old-client",
                token_endpoint="https://old-auth.test/token",
                token_state=new_oauth_token_state(
                    operator_id=operator_id,
                    access_token="old-expired-token",
                    refresh_token="old-refresh-token",
                    token_type="Bearer",
                    scope=None,
                    expires_at=now - datetime.timedelta(minutes=1),
                    now=now,
                ),
            )
        )

    async def refresh_after_reconnect(_client: object, _refresh_token: str) -> OAuthToken:
        replacement_expires_at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
        with sessionmaker(migrated_engine)() as session, session.begin():
            association = session.get(McpOperatorOAuthAssociation, (server.id, operator_id))
            assert association is not None
            replacement_now = datetime.datetime.now(datetime.UTC)
            session.delete(association)
            session.flush()
            session.add(
                McpOperatorOAuthAssociation(
                    association_id=replacement_association_id,
                    server_id=server.id,
                    operator_id=operator_id,
                    created_at=replacement_now,
                    client_id="replacement-client",
                    token_endpoint="https://replacement-auth.test/token",
                    token_state=new_oauth_token_state(
                        operator_id=operator_id,
                        access_token="replacement-access-token",
                        refresh_token="replacement-refresh-token",
                        token_type="Bearer",
                        scope=None,
                        expires_at=replacement_expires_at,
                        now=replacement_now,
                    ),
                )
            )
        return OAuthToken(
            access_token="stale-refresh-result", refresh_token="stale-rotated-refresh-token", expires_in=3600
        )

    monkeypatch.setattr("haku.console.mcp_operator_oauth._refresh_operator_oauth_token", refresh_after_reconnect)

    returned = await oauth_store.access_token_for(server=server, operator_id=operator_id)

    assert returned == "replacement-access-token"
    with sessionmaker(migrated_engine)() as session:
        association = session.get(McpOperatorOAuthAssociation, (server.id, operator_id))
        assert association is not None
        assert association.association_id == replacement_association_id
        assert association.client_id == "replacement-client"
        assert association.token_state.access_token == "replacement-access-token"
        assert association.token_state.refresh_token == "replacement-refresh-token"


async def test_operator_oauth_concurrent_callers_share_one_refresh(
    migrated_engine: Engine,
    oauth_store_for: Callable[[str], tuple[PostgresMcpOperatorOAuthStore, UUID]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oauth_store, operator_id = oauth_store_for("shared-refresh-claim")
    server = _dynamic_remote_oauth_server()
    now = datetime.datetime.now(datetime.UTC)
    with sessionmaker(migrated_engine)() as session, session.begin():
        session.add(
            McpOperatorOAuthAssociation(
                server_id=server.id,
                operator_id=operator_id,
                created_at=now,
                client_id="client-id",
                token_endpoint="https://auth.test/token",
                token_state=new_oauth_token_state(
                    operator_id=operator_id,
                    access_token="expired",
                    refresh_token="refresh-token",
                    token_type="Bearer",
                    scope=None,
                    expires_at=now - datetime.timedelta(minutes=1),
                    now=now,
                ),
            )
        )

    refresh_started = asyncio.Event()
    allow_refresh = asyncio.Event()
    refresh_count = 0

    async def controlled_refresh(_client: object, refresh_token: str) -> OAuthToken:
        nonlocal refresh_count
        assert refresh_token == "refresh-token"
        refresh_count += 1
        refresh_started.set()
        await allow_refresh.wait()
        return OAuthToken(access_token="fresh", refresh_token="rotated", expires_in=3600)

    monkeypatch.setattr("haku.console.mcp_operator_oauth._refresh_operator_oauth_token", controlled_refresh)
    first = asyncio.create_task(oauth_store.access_token_for(server=server, operator_id=operator_id))
    await refresh_started.wait()
    second = asyncio.create_task(oauth_store.access_token_for(server=server, operator_id=operator_id))
    await asyncio.sleep(0.1)
    allow_refresh.set()

    assert await asyncio.gather(first, second) == ["fresh", "fresh"]
    assert refresh_count == 1


async def test_operator_oauth_timeout_is_persisted_and_not_replayed(
    migrated_engine: Engine,
    oauth_store_for: Callable[[str], tuple[PostgresMcpOperatorOAuthStore, UUID]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oauth_store, operator_id = oauth_store_for("refresh-timeout-operator")
    server = _dynamic_remote_oauth_server()
    now = datetime.datetime.now(datetime.UTC)
    with sessionmaker(migrated_engine)() as session, session.begin():
        session.add(
            McpOperatorOAuthAssociation(
                server_id=server.id,
                operator_id=operator_id,
                created_at=now,
                client_id="client-id",
                token_endpoint="https://auth.test/token",
                token_state=new_oauth_token_state(
                    operator_id=operator_id,
                    access_token="expired",
                    refresh_token="one-time-refresh-token",
                    token_type="Bearer",
                    scope=None,
                    expires_at=now - datetime.timedelta(minutes=1),
                    now=now,
                ),
            )
        )

    attempts = 0

    async def timeout(_client: object, _refresh_token: str) -> OAuthToken:
        nonlocal attempts
        attempts += 1
        raise OAuthRefreshError(
            "MCP OAuth token refresh timed out after 30 seconds",
            kind=OAuthRefreshFailureKind.OUTCOME_UNKNOWN,
            action=OAuthRefreshFailureAction.RECONNECT,
        )

    monkeypatch.setattr("haku.console.mcp_operator_oauth._refresh_operator_oauth_token", timeout)
    with pytest.raises(OAuthRefreshError):
        await oauth_store.access_token_for(server=server, operator_id=operator_id)
    with pytest.raises(OAuthRefreshBlockedError):
        await oauth_store.access_token_for(server=server, operator_id=operator_id)

    status = oauth_store.list_statuses(servers=[server], operator_id=operator_id, username="operator").associations[0]
    assert isinstance(status.state, McpOperatorAuthDegraded)
    assert status.state.refresh_failure.initial.kind == OAuthRefreshFailureKind.OUTCOME_UNKNOWN
    assert status.state.refresh_failure.latest.message == "MCP OAuth token refresh timed out after 30 seconds"
    assert status.state.refresh_failure.resolution == "Reconnect the account before retrying."
    assert status.state.refresh_failure.attempts == 1
    assert attempts == 1


async def test_operator_oauth_retryable_failure_backs_off_and_clears_after_success(
    migrated_engine: Engine,
    oauth_store_for: Callable[[str], tuple[PostgresMcpOperatorOAuthStore, UUID]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oauth_store, operator_id = oauth_store_for("refresh-retry-operator")
    server = _dynamic_remote_oauth_server()
    now = datetime.datetime.now(datetime.UTC)
    with sessionmaker(migrated_engine)() as session, session.begin():
        session.add(
            McpOperatorOAuthAssociation(
                server_id=server.id,
                operator_id=operator_id,
                created_at=now,
                client_id="client-id",
                token_endpoint="https://auth.test/token",
                token_state=new_oauth_token_state(
                    operator_id=operator_id,
                    access_token="expired",
                    refresh_token="refresh-token",
                    token_type="Bearer",
                    scope=None,
                    expires_at=now - datetime.timedelta(minutes=1),
                    now=now,
                ),
            )
        )

    attempts = 0

    async def refresh(_client: object, _refresh_token: str) -> OAuthToken:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OAuthRefreshError(
                "MCP OAuth token refresh request failed: ConnectError",
                kind=OAuthRefreshFailureKind.CONNECT,
                action=OAuthRefreshFailureAction.RETRYING,
            )
        return OAuthToken(access_token="fresh", refresh_token="rotated", expires_in=3600)

    monkeypatch.setattr("haku.console.mcp_operator_oauth._refresh_operator_oauth_token", refresh)
    with pytest.raises(OAuthRefreshError):
        await oauth_store.access_token_for(server=server, operator_id=operator_id)
    with pytest.raises(OAuthRefreshBlockedError):
        await oauth_store.access_token_for(server=server, operator_id=operator_id)

    with sessionmaker(migrated_engine)() as session, session.begin():
        association = session.get(McpOperatorOAuthAssociation, (server.id, operator_id))
        assert association is not None
        assert association.token_state.refresh_retry_at is not None
        association.token_state.refresh_retry_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=1)

    assert await oauth_store.access_token_for(server=server, operator_id=operator_id) == "fresh"
    status = oauth_store.list_statuses(servers=[server], operator_id=operator_id, username="operator").associations[0]
    assert isinstance(status.state, McpOperatorAuthConnected)
    assert attempts == 2


if __name__ == "__main__":
    pytest_bazel.main()
