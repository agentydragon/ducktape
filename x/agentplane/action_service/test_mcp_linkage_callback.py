"""Backend OAuth linkage callbacks against migrated PostgreSQL."""

from collections.abc import AsyncIterator
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_bazel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from x.agentplane.action_service.api import create_app
from x.agentplane.action_service.auth import DisabledOperatorAuthenticator
from x.agentplane.action_service.catalog import ActionCatalog
from x.agentplane.action_service.db import ActionStore, McpOAuthTokenStateRow, make_sessionmaker
from x.agentplane.action_service.mcp_linkage import (
    McpLinkageAuthority,
    McpLinkageStart,
    McpLinkageStatus,
    McpLinkageView,
    McpOAuthServer,
    McpProvider,
)
from x.agentplane.action_service.models import Principal, PrincipalRole
from x.agentplane.action_service.service import ActionService
from x.agentplane.action_service.updates import ActionUpdates
from x.agentplane.sandbox_auth.http import SandboxPrincipalAuthenticator
from x.agentplane.sandbox_auth.principal import SandboxPrincipalResolver


@pytest.fixture
async def linkage(engine: AsyncEngine) -> AsyncIterator[McpLinkageAuthority]:
    def provider(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert request.url.path == "/token"
            return httpx.Response(200, json={"access_token": "test-access-token", "expires_in": 3600})
        return httpx.Response(404)

    server = McpOAuthServer(
        server_id="test-kubernetes",
        provider=McpProvider.KUBERNETES,
        server_url="https://test-kubernetes.example/mcp",
        authorization_endpoint="https://test-idp.example/authorize",
        token_endpoint="https://test-idp.example/token",
        client_id="test-client",
        redirect_uri="https://test-actions.example/mcp-linkage/callback",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
        yield McpLinkageAuthority(make_sessionmaker(engine), {server.server_id: server}, http=http)


@pytest.fixture
async def callback_client(
    linkage: McpLinkageAuthority, engine: AsyncEngine, db_url: str
) -> AsyncIterator[httpx.AsyncClient]:
    catalog = ActionCatalog(groups={})
    actions = ActionService(ActionStore(make_sessionmaker(engine)), catalog, {})
    app = create_app(
        actions,
        SandboxPrincipalAuthenticator(Mock(spec=SandboxPrincipalResolver)),
        DisabledOperatorAuthenticator(),
        catalog,
        updates=ActionUpdates(db_url),
        mcp_linkage=linkage,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test-actions") as http:
        yield http


async def test_first_link_relink_and_link_after_disconnect(
    linkage: McpLinkageAuthority, callback_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    operator = Principal(issuer="test-issuer", subject="test-operator", role=PrincipalRole.OPERATOR)
    for expected_revision, expected_token_revision in [(1, 1), (2, 2), (4, 1)]:
        if expected_revision == 4:
            disconnected = await linkage.disconnect("test-kubernetes", operator)
            assert disconnected.status is McpLinkageStatus.UNLINKED
        started = await linkage.start("test-kubernetes", McpLinkageStart(), operator)
        state = parse_qs(urlsplit(started.authorization_url).query)["state"][0]
        response = await callback_client.get("/v1/mcp-linkage/callback", params={"state": state, "code": "test-code"})
        assert response.status_code == 200
        view = McpLinkageView.model_validate(response.json())
        assert view.status is McpLinkageStatus.LINKED
        assert view.revision == expected_revision
        assert await linkage.access_token("test-kubernetes", expected_revision) == "test-access-token"
        async with make_sessionmaker(engine)() as db:
            token = (await db.scalars(select(McpOAuthTokenStateRow))).one()
            assert token.token_revision == expected_token_revision
        replay = await callback_client.get("/v1/mcp-linkage/callback", params={"state": state, "code": "test-code"})
        assert replay.status_code == 409
        assert "test-access-token" not in response.text


if __name__ == "__main__":
    pytest_bazel.main()
