"""MCP OAuth linkage against PostgreSQL with a mocked provider: linking a server, and refreshing its token."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
import pytest_bazel
from more_itertools import one
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.action_service.db import McpOAuthTokenStateRow, make_sessionmaker
from agentplane.action_service.mcp_linkage import McpLinkageAuthority, McpLinkageStart, McpLinkageStatus, McpOAuthServer
from agentplane.action_service.models import OperatorPrincipal

OPERATOR = OperatorPrincipal(issuer="test-linkage", subject="operator")
TOKEN_ENDPOINT = "https://idp.example.test/token"
SERVER = McpOAuthServer(
    server_id="kubernetes",
    server_url="https://mcp.example.test/mcp",
    authorization_endpoint="https://idp.example.test/authorize",
    token_endpoint=TOKEN_ENDPOINT,
    client_id="test-client",
    redirect_uri="https://app.example.test/mcp-linkage/callback",
)


def _provider(refresh: httpx2.Response) -> httpx2.MockTransport:
    """No discovery metadata, so the configured endpoints apply. The linked token is already due for
    refresh: it expires inside the authority's refresh skew."""

    def handle(request: httpx2.Request) -> httpx2.Response:
        if str(request.url) != TOKEN_ENDPOINT:
            return httpx2.Response(404)
        if parse_qs(request.content.decode())["grant_type"] == ["refresh_token"]:
            return refresh
        return httpx2.Response(
            200,
            json={
                "access_token": "test-only-access-token",
                "refresh_token": "test-only-refresh-token",
                "token_type": "Bearer",
                "expires_in": 30,
            },
        )

    return httpx2.MockTransport(handle)


async def _link(authority: McpLinkageAuthority) -> None:
    started = await authority.start(SERVER.server_id, McpLinkageStart(), OPERATOR)
    await authority.callback(one(parse_qs(urlparse(started.authorization_url).query)["state"]), "test-code")


async def test_first_linkage_creates_token_state(engine: AsyncEngine) -> None:
    sessions = make_sessionmaker(engine)
    async with httpx2.AsyncClient(transport=_provider(httpx2.Response(500))) as http:
        authority = McpLinkageAuthority(sessions, {SERVER.server_id: SERVER}, http=http, engine=engine)
        await _link(authority)
        view = await authority.status(SERVER.server_id)
    assert view.status is McpLinkageStatus.LINKED
    assert view.revision == 1
    async with sessions() as db:
        assert await db.scalar(select(McpOAuthTokenStateRow.token_revision)) == 1


async def test_a_refreshed_token_is_served_and_wakes_the_servers_executors(engine: AsyncEngine) -> None:
    refreshed = httpx2.Response(200, json={"access_token": "test-only-refreshed-token", "expires_in": 3600})
    async with httpx2.AsyncClient(transport=_provider(refreshed)) as http:
        authority = McpLinkageAuthority(make_sessionmaker(engine), {SERVER.server_id: SERVER}, http=http)
        await _link(authority)
        changed = authority.subscribe_changes(SERVER.server_id)
        assert await authority.access_token_for_execution(SERVER.server_id) == "test-only-refreshed-token"
    assert changed.is_set()


@pytest.mark.parametrize(
    ("refresh", "status", "action", "error"),
    [
        (
            httpx2.Response(400, json={"error": "invalid_grant", "error_description": "Token is not active"}),
            McpLinkageStatus.DEGRADED,
            "reconnect",
            "the OAuth provider refused the token request: invalid_grant: Token is not active",
        ),
        (
            httpx2.Response(503),
            McpLinkageStatus.LINKED,
            "retrying",
            "the token endpoint answered HTTP 503 Service Unavailable",
        ),
    ],
)
async def test_a_failed_refresh_reports_what_the_provider_answered(
    engine: AsyncEngine, refresh: httpx2.Response, status: McpLinkageStatus, action: str, error: str
) -> None:
    """A refusal needs the account linked again; an endpoint that did not answer is retried, and is
    reported while the token it holds is still good."""
    async with httpx2.AsyncClient(transport=_provider(refresh)) as http:
        authority = McpLinkageAuthority(make_sessionmaker(engine), {SERVER.server_id: SERVER}, http=http)
        await _link(authority)
        changed = authority.subscribe_changes(SERVER.server_id)
        await authority.access_token_for_execution(SERVER.server_id)
        view = await authority.status(SERVER.server_id)
    assert view.status is status
    assert view.refresh_failure is not None
    assert (view.refresh_failure.action, view.refresh_failure.error, view.refresh_failure.attempts) == (
        action,
        error,
        1,
    )
    assert changed.is_set()


if __name__ == "__main__":
    pytest_bazel.main()
