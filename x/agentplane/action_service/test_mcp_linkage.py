"""MCP OAuth linkage against PostgreSQL with a mocked provider: the first link of a server."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest_bazel
from more_itertools import one
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from x.agentplane.action_service.db import McpOAuthTokenStateRow, make_sessionmaker
from x.agentplane.action_service.mcp_linkage import (
    McpLinkageAuthority,
    McpLinkageStart,
    McpLinkageStatus,
    McpOAuthServer,
    McpProvider,
)
from x.agentplane.action_service.models import Principal, PrincipalRole

OPERATOR = Principal(issuer="test-linkage", subject="operator", role=PrincipalRole.OPERATOR)
TOKEN_ENDPOINT = "https://idp.example.test/token"


def _provider(request: httpx.Request) -> httpx.Response:
    # No discovery metadata: the configured endpoints on the server apply.
    if str(request.url) == TOKEN_ENDPOINT:
        return httpx.Response(
            200, json={"access_token": "test-only-access-token", "token_type": "Bearer", "expires_in": 3600}
        )
    return httpx.Response(404)


async def test_first_linkage_creates_token_state(engine: AsyncEngine) -> None:
    server = McpOAuthServer(
        server_id="kubernetes",
        provider=McpProvider.KUBERNETES,
        server_url="https://mcp.example.test/mcp",
        authorization_endpoint="https://idp.example.test/authorize",
        token_endpoint=TOKEN_ENDPOINT,
        client_id="test-client",
        redirect_uri="https://app.example.test/mcp-linkage/callback",
    )
    sessions = make_sessionmaker(engine)
    async with httpx.AsyncClient(transport=httpx.MockTransport(_provider)) as http:
        authority = McpLinkageAuthority(sessions, {"kubernetes": server}, http=http, engine=engine)
        started = await authority.start("kubernetes", McpLinkageStart(), OPERATOR)
        state = one(parse_qs(urlparse(started.authorization_url).query)["state"])
        view = await authority.callback(state, "test-code")
    assert view.status is McpLinkageStatus.LINKED
    assert view.revision == 1
    async with sessions() as db:
        assert await db.scalar(select(McpOAuthTokenStateRow.token_revision)) == 1


if __name__ == "__main__":
    pytest_bazel.main()
