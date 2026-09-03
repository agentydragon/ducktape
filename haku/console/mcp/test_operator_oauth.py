"""Tests for the operator OAuth helpers and store (haku.console.mcp.operator_oauth)."""

from __future__ import annotations

import asyncio
import datetime
from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_bazel
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from haku.console.database_schema import McpOperatorOAuthAssociation, McpOperatorOAuthFlow, OAuthTokenState, Operator
from haku.console.identity.operator_identity import InactiveOperatorError, OperatorStatus
from haku.console.mcp.operator_oauth import (
    McpOperatorAuthConnected,
    McpOperatorAuthDegraded,
    PostgresMcpOperatorOAuthStore,
    _BuiltOperatorOAuthFlow,
    _OperatorOAuthTokenClient,
    _refresh_operator_oauth_token,
    _token_request_auth,
)
from haku.console.mcp_config import (
    DynamicOAuthClientRegistration,
    McpServerEntry,
    RemoteMcpBackend,
    RemoteServerOAuthAuth,
)
from haku.console.oauth.token_state import (
    PostgresTokenStateStore,
    RefreshBlockedError,
    RefreshError,
    RefreshFailureAction,
    RefreshFailureKind,
    _refresh_retry_delay,
    new_token_state,
)


@pytest.fixture
async def oauth_store_for(
    migrated_sessions, migrated_identity_store
) -> Callable[[str], Awaitable[tuple[PostgresMcpOperatorOAuthStore, UUID]]]:
    """Build a `(store, operator_id)` pair on the shared migrated test database."""

    async def build(external_user_key: str) -> tuple[PostgresMcpOperatorOAuthStore, UUID]:
        operator_id = await migrated_identity_store.resolve_configured_external_user_key(external_user_key)
        store = PostgresMcpOperatorOAuthStore(
            migrated_sessions,
            operator_identity_store=migrated_identity_store,
            token_states=PostgresTokenStateStore(migrated_sessions, operator_identity_store=migrated_identity_store),
            token_timeout_seconds=30.0,
        )
        return store, operator_id

    return build


async def _disable_operator(engine: AsyncEngine, operator_id: UUID) -> None:
    async with async_sessionmaker(engine)() as session, session.begin():
        operator = await session.get(Operator, operator_id)
        assert operator is not None
        operator.status = OperatorStatus.DISABLED
        operator.updated_at = datetime.datetime.now(datetime.UTC)


@pytest.fixture
def dynamic_remote_oauth_server() -> McpServerEntry:
    return McpServerEntry(
        id="grocy-sf",
        backend=RemoteMcpBackend(
            url="https://grocy.test/mcp",
            auth=RemoteServerOAuthAuth(client_registration=DynamicOAuthClientRegistration()),
        ),
    )


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

    monkeypatch.setattr("haku.console.mcp.operator_oauth.httpx.AsyncClient", client)
    with pytest.raises(RefreshError) as raised:
        await _refresh_operator_oauth_token(
            _OperatorOAuthTokenClient(
                client_id="client", token_endpoint="https://auth.test/token", timeout_seconds=37.0
            ),
            "refresh-token",
        )

    assert raised.value.kind == RefreshFailureKind.OUTCOME_UNKNOWN
    # Retryable, not terminal: a response that never arrived leaves rotation unknown, and the next
    # attempt settles it — success if the server never processed the request, `invalid_grant` (which
    # classifies RECONNECT) if it did. Giving up here instead cost an association a manual reconnect
    # for every transient timeout.
    assert raised.value.action == RefreshFailureAction.RETRYING
    assert str(raised.value) == "MCP OAuth token refresh timed out after 37 seconds"


def test_token_request_auth_explicitly_requests_json_token_responses() -> None:
    data, headers = _token_request_auth(
        {"grant_type": "authorization_code"},
        _OperatorOAuthTokenClient(
            client_id="client",
            token_endpoint="https://authorization.example/token",
            client_secret="secret",
            token_endpoint_auth_method="client_secret_post",
        ),
    )

    assert data == {"grant_type": "authorization_code", "client_secret": "secret"}
    assert headers == {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}


async def test_operator_oauth_callback_rechecks_operator_after_token_exchange(
    migrated_engine: AsyncEngine,
    oauth_store_for: Callable[[str], Awaitable[tuple[PostgresMcpOperatorOAuthStore, UUID]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oauth_store, operator_id = await oauth_store_for("callback-race-operator")
    now = datetime.datetime.now(datetime.UTC)
    async with async_sessionmaker(migrated_engine)() as session, session.begin():
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
        await _disable_operator(migrated_engine, operator_id)
        return OAuthToken(access_token="must-not-be-persisted")

    monkeypatch.setattr("haku.console.mcp.operator_oauth._exchange_operator_oauth_code", exchange_after_disable)

    with pytest.raises(InactiveOperatorError):
        await oauth_store.complete_callback(
            state="callback-race-state",
            code="authorization-code",
            operator_id=operator_id,
            username="operator@example.com",
        )

    async with async_sessionmaker(migrated_engine)() as session:
        assert await session.get(McpOperatorOAuthAssociation, ("grocy-sf", operator_id)) is None


async def test_operator_oauth_connect_rechecks_operator_after_discovery_and_dcr(
    migrated_engine: AsyncEngine,
    oauth_store_for: Callable[[str], Awaitable[tuple[PostgresMcpOperatorOAuthStore, UUID]]],
    monkeypatch: pytest.MonkeyPatch,
    dynamic_remote_oauth_server: McpServerEntry,
) -> None:
    oauth_store, operator_id = await oauth_store_for("connect-race-operator")
    server = dynamic_remote_oauth_server
    now = datetime.datetime.now(datetime.UTC)

    async def build_flow_after_disable(_server: McpServerEntry, _public_base_url: str) -> _BuiltOperatorOAuthFlow:
        await _disable_operator(migrated_engine, operator_id)
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

    monkeypatch.setattr("haku.console.mcp.operator_oauth._build_operator_oauth_flow", build_flow_after_disable)

    with pytest.raises(InactiveOperatorError):
        await oauth_store.connect_flow(server=server, operator_id=operator_id, public_base_url="https://haku.test")

    async with async_sessionmaker(migrated_engine)() as session:
        assert await session.get(McpOperatorOAuthFlow, "connect-race-state") is None


async def test_operator_oauth_refresh_rechecks_operator_before_write_and_return(
    migrated_engine: AsyncEngine,
    oauth_store_for: Callable[[str], Awaitable[tuple[PostgresMcpOperatorOAuthStore, UUID]]],
    monkeypatch: pytest.MonkeyPatch,
    dynamic_remote_oauth_server: McpServerEntry,
) -> None:
    oauth_store, operator_id = await oauth_store_for("refresh-race-operator")
    server = dynamic_remote_oauth_server
    now = datetime.datetime.now(datetime.UTC)
    async with async_sessionmaker(migrated_engine)() as session, session.begin():
        session.add(
            McpOperatorOAuthAssociation(
                server_id=server.id,
                operator_id=operator_id,
                created_at=now,
                client_id="client-id",
                token_endpoint="https://auth.test/token",
                token_state=new_token_state(
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
        await _disable_operator(migrated_engine, operator_id)
        return OAuthToken(
            access_token="must-not-be-written-or-returned", refresh_token="must-not-be-written", expires_in=3600
        )

    monkeypatch.setattr("haku.console.mcp.operator_oauth._refresh_operator_oauth_token", refresh_after_disable)

    with pytest.raises(InactiveOperatorError):
        await oauth_store.access_token_for(server=server, operator_id=operator_id)

    async with async_sessionmaker(migrated_engine)() as session:
        association = await session.get(McpOperatorOAuthAssociation, (server.id, operator_id))
        assert association is not None
        assert association.token_state.access_token == "old-expired-token"
        assert association.token_state.refresh_token == "refresh-token"


async def test_operator_oauth_refresh_does_not_overwrite_concurrent_reconnect(
    migrated_engine: AsyncEngine,
    oauth_store_for: Callable[[str], Awaitable[tuple[PostgresMcpOperatorOAuthStore, UUID]]],
    monkeypatch: pytest.MonkeyPatch,
    dynamic_remote_oauth_server: McpServerEntry,
) -> None:
    oauth_store, operator_id = await oauth_store_for("refresh-reconnect-race")
    server = dynamic_remote_oauth_server
    now = datetime.datetime.now(datetime.UTC)
    replacement_association_id = uuid4()
    async with async_sessionmaker(migrated_engine)() as session, session.begin():
        session.add(
            McpOperatorOAuthAssociation(
                server_id=server.id,
                operator_id=operator_id,
                created_at=now,
                client_id="old-client",
                token_endpoint="https://old-auth.test/token",
                token_state=new_token_state(
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
        async with async_sessionmaker(migrated_engine)() as session, session.begin():
            association = await session.get(McpOperatorOAuthAssociation, (server.id, operator_id))
            assert association is not None
            replacement_now = datetime.datetime.now(datetime.UTC)
            await session.delete(association)
            await session.flush()
            session.add(
                McpOperatorOAuthAssociation(
                    association_id=replacement_association_id,
                    server_id=server.id,
                    operator_id=operator_id,
                    created_at=replacement_now,
                    client_id="replacement-client",
                    token_endpoint="https://replacement-auth.test/token",
                    token_state=new_token_state(
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

    monkeypatch.setattr("haku.console.mcp.operator_oauth._refresh_operator_oauth_token", refresh_after_reconnect)

    returned = await oauth_store.access_token_for(server=server, operator_id=operator_id)

    assert returned == "replacement-access-token"
    async with async_sessionmaker(migrated_engine)() as session:
        association = await session.get(McpOperatorOAuthAssociation, (server.id, operator_id))
        assert association is not None
        assert association.association_id == replacement_association_id
        assert association.client_id == "replacement-client"
        assert association.token_state.access_token == "replacement-access-token"
        assert association.token_state.refresh_token == "replacement-refresh-token"


async def test_operator_oauth_concurrent_callers_share_one_refresh(
    migrated_engine: AsyncEngine,
    oauth_store_for: Callable[[str], Awaitable[tuple[PostgresMcpOperatorOAuthStore, UUID]]],
    monkeypatch: pytest.MonkeyPatch,
    dynamic_remote_oauth_server: McpServerEntry,
) -> None:
    oauth_store, operator_id = await oauth_store_for("shared-refresh-claim")
    server = dynamic_remote_oauth_server
    now = datetime.datetime.now(datetime.UTC)
    async with async_sessionmaker(migrated_engine)() as session, session.begin():
        session.add(
            McpOperatorOAuthAssociation(
                server_id=server.id,
                operator_id=operator_id,
                created_at=now,
                client_id="client-id",
                token_endpoint="https://auth.test/token",
                token_state=new_token_state(
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

    monkeypatch.setattr("haku.console.mcp.operator_oauth._refresh_operator_oauth_token", controlled_refresh)
    first = asyncio.create_task(oauth_store.access_token_for(server=server, operator_id=operator_id))
    await refresh_started.wait()
    second = asyncio.create_task(oauth_store.access_token_for(server=server, operator_id=operator_id))
    await asyncio.sleep(0.1)
    allow_refresh.set()

    # typeshed models a two-argument gather as returning a tuple so it can type the elements; the
    # runtime value is a list, and comparing the two shapes is what strict_equality objects to.
    assert list(await asyncio.gather(first, second)) == ["fresh", "fresh"]
    assert refresh_count == 1


async def test_operator_oauth_ambiguous_timeout_retries_then_stops_on_invalid_grant(
    migrated_engine: AsyncEngine,
    oauth_store_for: Callable[[str], Awaitable[tuple[PostgresMcpOperatorOAuthStore, UUID]]],
    monkeypatch: pytest.MonkeyPatch,
    dynamic_remote_oauth_server: McpServerEntry,
) -> None:
    oauth_store, operator_id = await oauth_store_for("refresh-timeout-operator")
    server = dynamic_remote_oauth_server
    now = datetime.datetime.now(datetime.UTC)
    async with async_sessionmaker(migrated_engine)() as session, session.begin():
        session.add(
            McpOperatorOAuthAssociation(
                server_id=server.id,
                operator_id=operator_id,
                created_at=now,
                client_id="client-id",
                token_endpoint="https://auth.test/token",
                token_state=new_token_state(
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

    async def refresh(_client: object, _refresh_token: str) -> OAuthToken:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RefreshError(
                "MCP OAuth token refresh timed out after 30 seconds",
                kind=RefreshFailureKind.OUTCOME_UNKNOWN,
                action=RefreshFailureAction.RETRYING,
            )
        # The server had in fact rotated the token, so the replay is refused — the definitive
        # answer the retry existed to obtain.
        raise RefreshError(
            'MCP OAuth token refresh failed: 400 {"error":"invalid_grant"}',
            kind=RefreshFailureKind.OAUTH_REJECTED,
            action=RefreshFailureAction.RECONNECT,
        )

    monkeypatch.setattr("haku.console.mcp.operator_oauth._refresh_operator_oauth_token", refresh)
    with pytest.raises(RefreshError):
        await oauth_store.access_token_for(server=server, operator_id=operator_id)

    # An ambiguous timeout leaves the association retryable, not terminally wedged.
    status = (
        await oauth_store.list_statuses(servers=[server], operator_id=operator_id, username="operator")
    ).associations[0]
    assert isinstance(status.state, McpOperatorAuthDegraded)
    assert status.state.refresh_failure.initial.kind == RefreshFailureKind.OUTCOME_UNKNOWN
    assert status.state.refresh_failure.resolution == "Retry scheduled automatically."
    assert status.state.refresh_failure.next_retry_at is not None

    # Backoff still applies between attempts, so the association is blocked only until it is due.
    with pytest.raises(RefreshBlockedError):
        await oauth_store.access_token_for(server=server, operator_id=operator_id)
    assert attempts == 1

    async with async_sessionmaker(migrated_engine)() as session, session.begin():
        association = await session.get(McpOperatorOAuthAssociation, (server.id, operator_id))
        assert association is not None
        association.token_state.refresh_retry_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=1)

    with pytest.raises(RefreshError):
        await oauth_store.access_token_for(server=server, operator_id=operator_id)
    assert attempts == 2

    # `invalid_grant` is definitive, so the retries stop there and an operator is asked to reconnect.
    status = (
        await oauth_store.list_statuses(servers=[server], operator_id=operator_id, username="operator")
    ).associations[0]
    assert isinstance(status.state, McpOperatorAuthDegraded)
    assert status.state.refresh_failure.latest.kind == RefreshFailureKind.OAUTH_REJECTED
    assert status.state.refresh_failure.resolution == "Reconnect the account before retrying."
    assert status.state.refresh_failure.next_retry_at is None
    with pytest.raises(RefreshBlockedError):
        await oauth_store.access_token_for(server=server, operator_id=operator_id)
    assert attempts == 2


async def test_operator_oauth_retryable_failure_backs_off_and_clears_after_success(
    migrated_engine: AsyncEngine,
    oauth_store_for: Callable[[str], Awaitable[tuple[PostgresMcpOperatorOAuthStore, UUID]]],
    monkeypatch: pytest.MonkeyPatch,
    dynamic_remote_oauth_server: McpServerEntry,
) -> None:
    oauth_store, operator_id = await oauth_store_for("refresh-retry-operator")
    server = dynamic_remote_oauth_server
    now = datetime.datetime.now(datetime.UTC)
    async with async_sessionmaker(migrated_engine)() as session, session.begin():
        session.add(
            McpOperatorOAuthAssociation(
                server_id=server.id,
                operator_id=operator_id,
                created_at=now,
                client_id="client-id",
                token_endpoint="https://auth.test/token",
                token_state=new_token_state(
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
            raise RefreshError(
                "MCP OAuth token refresh request failed: ConnectError",
                kind=RefreshFailureKind.CONNECT,
                action=RefreshFailureAction.RETRYING,
            )
        return OAuthToken(access_token="fresh", refresh_token="rotated", expires_in=3600)

    monkeypatch.setattr("haku.console.mcp.operator_oauth._refresh_operator_oauth_token", refresh)
    with pytest.raises(RefreshError):
        await oauth_store.access_token_for(server=server, operator_id=operator_id)
    with pytest.raises(RefreshBlockedError):
        await oauth_store.access_token_for(server=server, operator_id=operator_id)

    async with async_sessionmaker(migrated_engine)() as session, session.begin():
        association = await session.get(McpOperatorOAuthAssociation, (server.id, operator_id))
        assert association is not None
        assert association.token_state.refresh_retry_at is not None
        association.token_state.refresh_retry_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=1)

    assert await oauth_store.access_token_for(server=server, operator_id=operator_id) == "fresh"
    status = (
        await oauth_store.list_statuses(servers=[server], operator_id=operator_id, username="operator")
    ).associations[0]
    assert isinstance(status.state, McpOperatorAuthConnected)
    assert attempts == 2


def test_refresh_retry_delay_doubles_from_base_and_saturates_at_max() -> None:
    """The persisted backoff sequence is operator-visible policy: 30s after the first failure,
    doubling to the 15-minute cap from the sixth on, and saturated — never overflowed — for
    arbitrarily long episodes (2**1024 exceeds float range; attempt 1025 froze episodes before)."""
    assert [_refresh_retry_delay(attempt) for attempt in (1, 2, 3)] == [
        datetime.timedelta(seconds=30),
        datetime.timedelta(minutes=1),
        datetime.timedelta(minutes=2),
    ]
    assert _refresh_retry_delay(5) == datetime.timedelta(minutes=8)
    assert _refresh_retry_delay(6) == datetime.timedelta(minutes=15)
    assert _refresh_retry_delay(1024) == datetime.timedelta(minutes=15)
    assert _refresh_retry_delay(1025) == datetime.timedelta(minutes=15)


async def test_operator_oauth_failure_recording_survives_long_episodes(
    migrated_engine: AsyncEngine,
    oauth_store_for: Callable[[str], Awaitable[tuple[PostgresMcpOperatorOAuthStore, UUID]]],
    monkeypatch: pytest.MonkeyPatch,
    dynamic_remote_oauth_server: McpServerEntry,
) -> None:
    """The 1025th consecutive failure must still be recorded: 2**1024 exceeds float range, so an
    unclamped backoff exponent raises OverflowError inside the failure write, rolling it back and
    freezing the persisted episode (and its surfaced status) at attempt 1024 forever."""
    oauth_store, operator_id = await oauth_store_for("refresh-long-episode-operator")
    server = dynamic_remote_oauth_server
    now = datetime.datetime.now(datetime.UTC)
    failure_message = "MCP OAuth token refresh request failed: ConnectError"
    async with async_sessionmaker(migrated_engine)() as session, session.begin():
        token_state = new_token_state(
            operator_id=operator_id,
            access_token="expired",
            refresh_token="refresh-token",
            token_type="Bearer",
            scope=None,
            expires_at=now - datetime.timedelta(days=10),
            now=now,
        )
        token_state.refresh_failure_started_at = now - datetime.timedelta(days=10)
        token_state.refresh_failure_initial_kind = RefreshFailureKind.CONNECT
        token_state.refresh_failure_initial_message = failure_message
        token_state.refresh_failure_latest_at = now - datetime.timedelta(minutes=15)
        token_state.refresh_failure_latest_kind = RefreshFailureKind.CONNECT
        token_state.refresh_failure_latest_message = failure_message
        token_state.refresh_failure_count = 1024
        token_state.refresh_failure_action = RefreshFailureAction.RETRYING
        token_state.refresh_retry_at = now - datetime.timedelta(seconds=1)
        session.add(
            McpOperatorOAuthAssociation(
                server_id=server.id,
                operator_id=operator_id,
                created_at=now - datetime.timedelta(days=30),
                client_id="client-id",
                token_endpoint="https://auth.test/token",
                token_state=token_state,
            )
        )

    async def refresh(_client: object, _refresh_token: str) -> OAuthToken:
        raise RefreshError(failure_message, kind=RefreshFailureKind.CONNECT, action=RefreshFailureAction.RETRYING)

    monkeypatch.setattr("haku.console.mcp.operator_oauth._refresh_operator_oauth_token", refresh)
    with pytest.raises(RefreshError):
        await oauth_store.access_token_for(server=server, operator_id=operator_id)

    async with async_sessionmaker(migrated_engine)() as session, session.begin():
        association = await session.get(McpOperatorOAuthAssociation, (server.id, operator_id))
        assert association is not None
        state = association.token_state
        assert state.refresh_failure_count == 1025
        assert state.refresh_failure_started_at == now - datetime.timedelta(days=10)
        assert state.refresh_failure_latest_at is not None
        assert state.refresh_failure_latest_at > now - datetime.timedelta(minutes=15)
        # Saturated backoff: the next retry stays scheduled at the cap rather than overflowing.
        assert state.refresh_retry_at == state.refresh_failure_latest_at + datetime.timedelta(minutes=15)
        assert state.refresh_claim_id is None


async def test_forget_unconfigured_servers_drops_the_association_and_its_token(
    migrated_engine: AsyncEngine,
    oauth_store_for: Callable[[str], Awaitable[tuple[PostgresMcpOperatorOAuthStore, UUID]]],
    dynamic_remote_oauth_server: McpServerEntry,
) -> None:
    """A server leaving the catalog takes its per-Operator grant with it.

    Left behind, the row is a refresh token for a server nothing can call, and the
    background sweep rediscovers and fails on it every 30 seconds forever.
    """
    oauth_store, operator_id = await oauth_store_for("forget-unconfigured-operator")
    removed = dynamic_remote_oauth_server
    kept = McpServerEntry(
        id="tana",
        backend=RemoteMcpBackend(
            url="https://tana.test/mcp",
            auth=RemoteServerOAuthAuth(client_registration=DynamicOAuthClientRegistration()),
        ),
    )
    now = datetime.datetime.now(datetime.UTC)
    async with async_sessionmaker(migrated_engine)() as session, session.begin():
        for server in (removed, kept):
            session.add(
                McpOperatorOAuthAssociation(
                    server_id=server.id,
                    operator_id=operator_id,
                    created_at=now,
                    client_id="client-id",
                    token_endpoint="https://auth.test/token",
                    token_state=new_token_state(
                        operator_id=operator_id,
                        access_token=f"{server.id}-access",
                        refresh_token=f"{server.id}-refresh",
                        token_type="Bearer",
                        scope=None,
                        expires_at=now + datetime.timedelta(hours=1),
                        now=now,
                    ),
                )
            )
    async with async_sessionmaker(migrated_engine)() as session:
        association = await session.get(McpOperatorOAuthAssociation, (removed.id, operator_id))
        assert association is not None
        token_state_id = association.token_state_id

    await oauth_store.forget_unconfigured_servers([kept])

    async with async_sessionmaker(migrated_engine)() as session:
        assert await session.get(McpOperatorOAuthAssociation, (removed.id, operator_id)) is None
        assert await session.get(McpOperatorOAuthAssociation, (kept.id, operator_id)) is not None
        # The token row must go too, or the refresh token outlives the association holding it.
        assert await session.get(OAuthTokenState, token_state_id) is None


async def test_forget_unconfigured_servers_keeps_everything_when_nothing_was_removed(
    migrated_engine: AsyncEngine,
    oauth_store_for: Callable[[str], Awaitable[tuple[PostgresMcpOperatorOAuthStore, UUID]]],
    dynamic_remote_oauth_server: McpServerEntry,
) -> None:
    """It runs on every startup, so the ordinary case must be a no-op."""
    oauth_store, operator_id = await oauth_store_for("forget-unconfigured-noop-operator")
    server = dynamic_remote_oauth_server
    now = datetime.datetime.now(datetime.UTC)
    async with async_sessionmaker(migrated_engine)() as session, session.begin():
        session.add(
            McpOperatorOAuthAssociation(
                server_id=server.id,
                operator_id=operator_id,
                created_at=now,
                client_id="client-id",
                token_endpoint="https://auth.test/token",
                token_state=new_token_state(
                    operator_id=operator_id,
                    access_token="access",
                    refresh_token="refresh",
                    token_type="Bearer",
                    scope=None,
                    expires_at=now + datetime.timedelta(hours=1),
                    now=now,
                ),
            )
        )

    await oauth_store.forget_unconfigured_servers([server])

    async with async_sessionmaker(migrated_engine)() as session:
        assert await session.get(McpOperatorOAuthAssociation, (server.id, operator_id)) is not None


if __name__ == "__main__":
    pytest_bazel.main()
