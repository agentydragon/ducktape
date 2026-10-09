"""MCP OAuth linkage and token refresh against PostgreSQL with a mocked provider."""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
import pytest_bazel
from more_itertools import one
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.action_service.db import make_sessionmaker
from agentplane.action_service.mcp_linkage import McpLinkageAuthority, McpLinkageStart, McpLinkageStatus
from agentplane.action_service.mcp_settings import McpOAuthServer
from agentplane.action_service.models import OperatorPrincipal
from agentplane.action_service.updates import ActionUpdates

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


@pytest.mark.parametrize("existing_query", ["", "?tenant=a%2Bb%26c&tag=one&tag=two&empty=&state=stale&client_id=stale"])
async def test_authorization_url_merges_provider_query_parameters(engine: AsyncEngine, existing_query: str) -> None:
    server = SERVER.model_copy(
        update={
            "authorization_endpoint": f"https://idp.example.test/authorize{existing_query}",
            "scopes": ["read", "write"],
        }
    )
    async with httpx2.AsyncClient(transport=_provider(httpx2.Response(200))) as http:
        authority = McpLinkageAuthority(make_sessionmaker(engine), {server.server_id: server}, http=http)
        started = await authority.start(server.server_id, McpLinkageStart(), OPERATOR)
        url = urlparse(started.authorization_url)
        query = parse_qs(url.query, keep_blank_values=True)
        assert (url.scheme, url.netloc, url.path) == ("https", "idp.example.test", "/authorize")
        if existing_query:
            assert query["tenant"] == ["a+b&c"]
            assert query["tag"] == ["one", "two"]
            assert query["empty"] == [""]
        assert query["response_type"] == ["code"]
        assert query["client_id"] == ["test-client"]
        assert query["redirect_uri"] == [server.redirect_uri]
        assert query["scope"] == ["read write"]
        assert query["code_challenge_method"] == ["S256"]
        assert len(one(query["code_challenge"])) == 43
        state = one(query["state"])
        assert state != "stale"
        await authority.callback(state, "test-code")
        assert (await authority.status(server.server_id)).status is McpLinkageStatus.LINKED


async def test_a_refreshed_token_is_served_and_wakes_the_servers_executors(engine: AsyncEngine) -> None:
    refreshed = httpx2.Response(200, json={"access_token": "test-only-refreshed-token", "expires_in": 3600})
    async with httpx2.AsyncClient(transport=_provider(refreshed)) as http:
        authority = McpLinkageAuthority(make_sessionmaker(engine), {SERVER.server_id: SERVER}, http=http)
        await _link(authority)
        changed = authority.subscribe_changes(SERVER.server_id)
        assert await authority.access_token_for_execution(SERVER.server_id) == "test-only-refreshed-token"
    assert changed.is_set()


async def test_linkage_changes_wake_streams_on_both_replicas(engine: AsyncEngine, db_url: str) -> None:
    updates_a, updates_b = ActionUpdates(db_url), ActionUpdates(db_url)
    refreshed = httpx2.Response(200, json={"access_token": "test-only-refreshed-token", "expires_in": 3600})
    async with httpx2.AsyncClient(transport=_provider(refreshed)) as http:
        authority = McpLinkageAuthority(make_sessionmaker(engine), {SERVER.server_id: SERVER}, http=http)
        async with updates_a.listener.listen(), updates_b.listener.listen():
            with updates_a.subscribe_mcp_linkages() as first, updates_b.subscribe_mcp_linkages() as second:
                await _link(authority)
                async with asyncio.timeout(10):
                    await asyncio.gather(first.changed.wait(), second.changed.wait())
                assert (await authority.status(SERVER.server_id)).status == McpLinkageStatus.LINKED
                first.changed.clear()
                second.changed.clear()
                await authority.disconnect(SERVER.server_id, OPERATOR)
                async with asyncio.timeout(10):
                    await asyncio.gather(first.changed.wait(), second.changed.wait())
                assert (await authority.status(SERVER.server_id)).status == McpLinkageStatus.UNLINKED


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
