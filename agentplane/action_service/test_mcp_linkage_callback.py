"""Backend OAuth linkage callbacks against migrated PostgreSQL."""

from collections.abc import AsyncIterator
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import httpx
import httpx2
import pytest
import pytest_bazel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.action_service.api import create_app
from agentplane.action_service.auth import DisabledOperatorAuthenticator
from agentplane.action_service.catalog import ActionCatalog
from agentplane.action_service.db import ActionStore, McpOAuthTokenStateRow, make_sessionmaker
from agentplane.action_service.mcp_linkage import (
    McpLinkageAuthority,
    McpLinkageConflictError,
    McpLinkageStart,
    McpLinkageStatus,
    McpLinkageView,
)
from agentplane.action_service.mcp_settings import McpClientMetadataSettings, McpOAuthServer
from agentplane.action_service.models import OperatorPrincipal
from agentplane.action_service.service import ActionService
from agentplane.action_service.testing.callers import admitted_callers
from agentplane.action_service.updates import ActionUpdates
from agentplane.workload_auth.principal import WorkloadPrincipalResolver


@pytest.fixture
def cimd_advertised() -> bool:
    """Whether the authorization server behind the CIMD-using MCP servers advertises CIMD support."""
    return True


@pytest.fixture
async def linkage(engine: AsyncEngine, cimd_advertised: bool) -> AsyncIterator[McpLinkageAuthority]:
    cimd_client_id = "https://test-actions.example/oauth/client-metadata.json"

    def provider(request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST":
            assert request.url.path == "/token"
            # GitHub answers form-encoded without this header, and reports a bad code as 200.
            assert request.headers["accept"] == "application/json"
            form = parse_qs(request.content.decode())
            if form.get("grant_type") == ["refresh_token"]:
                assert form["client_id"] == [cimd_client_id]
                assert "client_secret" not in form
                return httpx2.Response(200, json={"access_token": "test-refreshed-access-token", "expires_in": 3600})
            if form.get("code") == ["test-cimd-code"]:
                assert form["client_id"] == [cimd_client_id]
                assert "client_secret" not in form
                # Expires inside the authority's refresh skew, so the next token lookup refreshes it.
                return httpx2.Response(
                    200,
                    json={
                        "access_token": "test-cimd-access-token",
                        "refresh_token": "test-cimd-refresh-token",
                        "expires_in": 30,
                    },
                )
            if form.get("code") == ["test-bad-code"]:
                return httpx2.Response(200, json={"error": "bad_verification_code"})
            return httpx2.Response(200, json={"access_token": "test-access-token", "expires_in": 3600})
        if request.url.host in {"test-cimd.example", "test-cimd-other.example"} and request.url.path == (
            "/.well-known/oauth-authorization-server"
        ):
            return httpx2.Response(
                200,
                json={
                    "issuer": "https://test-idp.example",
                    "authorization_endpoint": "https://test-idp.example/authorize",
                    "token_endpoint": "https://test-idp.example/token",
                    "client_id_metadata_document_supported": cimd_advertised,
                },
            )
        return httpx2.Response(404)

    server = McpOAuthServer(
        server_id="test-kubernetes",
        server_url="https://test-kubernetes.example/mcp",
        authorization_endpoint="https://test-idp.example/authorize",
        token_endpoint="https://test-idp.example/token",
        client_id="test-client",
        redirect_uri="https://test-actions.example/mcp-linkage/callback",
    )
    cimd_server = McpOAuthServer(
        server_id="test-cimd",
        server_url="https://test-cimd.example/mcp",
        use_shared_cimd=True,
        redirect_uri="https://test-actions.example/mcp-linkage/callback",
    )
    other_cimd_server = McpOAuthServer(
        server_id="test-cimd-other",
        server_url="https://test-cimd-other.example/mcp",
        use_shared_cimd=True,
        redirect_uri="https://test-actions.example/other-mcp-linkage/callback",
    )
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(provider)) as http:
        yield McpLinkageAuthority(
            make_sessionmaker(engine),
            {
                server.server_id: server,
                cimd_server.server_id: cimd_server,
                other_cimd_server.server_id: other_cimd_server,
            },
            client_metadata=McpClientMetadataSettings(url=cimd_client_id, client_name="Test Agentplane application"),
            http=http,
        )


@pytest.fixture
async def callback_client(
    linkage: McpLinkageAuthority, engine: AsyncEngine, db_url: str
) -> AsyncIterator[httpx.AsyncClient]:
    catalog = ActionCatalog(groups={})
    actions = ActionService(ActionStore(make_sessionmaker(engine)), catalog, {})
    app = create_app(
        actions,
        Mock(spec=WorkloadPrincipalResolver),
        DisabledOperatorAuthenticator(),
        catalog,
        callers=admitted_callers(),
        updates=ActionUpdates(db_url),
        direct_wait_seconds=30,
        max_wait_seconds=30,
        mcp_linkage=linkage,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test-actions") as http:
        yield http


async def test_first_link_relink_and_link_after_disconnect(
    linkage: McpLinkageAuthority, callback_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    operator = OperatorPrincipal(issuer="test-issuer", subject="test-operator")
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


async def test_rejected_code_is_reported_and_leaves_the_server_unlinked(
    linkage: McpLinkageAuthority, callback_client: httpx.AsyncClient
) -> None:
    operator = OperatorPrincipal(issuer="test-issuer", subject="test-operator")
    started = await linkage.start("test-kubernetes", McpLinkageStart(), operator)
    state = parse_qs(urlsplit(started.authorization_url).query)["state"][0]
    response = await callback_client.get("/v1/mcp-linkage/callback", params={"state": state, "code": "test-bad-code"})
    assert response.status_code == 502, response.text
    assert response.json()["detail"] == "the OAuth provider refused the token request: bad_verification_code"
    assert (await linkage.status("test-kubernetes")).status is McpLinkageStatus.UNLINKED


async def test_cimd_document_is_public_and_matches_the_configured_client(callback_client: httpx.AsyncClient) -> None:
    response = await callback_client.get("/oauth/client-metadata.json")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=3600"
    assert response.json() == {
        "client_id": "https://test-actions.example/oauth/client-metadata.json",
        "client_name": "Test Agentplane application",
        "redirect_uris": [
            "https://test-actions.example/mcp-linkage/callback",
            "https://test-actions.example/other-mcp-linkage/callback",
        ],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }
    assert (await callback_client.get("/oauth/client-metadata/test-cimd.json")).status_code == 404


async def test_shared_cimd_is_used_for_authorization_and_token_exchange(
    linkage: McpLinkageAuthority, callback_client: httpx.AsyncClient
) -> None:
    started = await linkage.start(
        "test-cimd", McpLinkageStart(), OperatorPrincipal(issuer="test-issuer", subject="test-operator")
    )
    query = parse_qs(urlsplit(started.authorization_url).query)
    assert query["client_id"] == ["https://test-actions.example/oauth/client-metadata.json"]
    other_started = await linkage.start(
        "test-cimd-other", McpLinkageStart(), OperatorPrincipal(issuer="test-issuer", subject="test-operator")
    )
    other_query = parse_qs(urlsplit(other_started.authorization_url).query)
    assert other_query["client_id"] == query["client_id"]
    response = await callback_client.get(
        "/v1/mcp-linkage/callback", params={"state": query["state"][0], "code": "test-cimd-code"}
    )
    assert response.status_code == 200, response.text
    assert await linkage.access_token_for_execution("test-cimd") == "test-refreshed-access-token"


@pytest.mark.parametrize("cimd_advertised", [False])
async def test_cimd_link_requires_discovery_support(linkage: McpLinkageAuthority) -> None:
    with pytest.raises(McpLinkageConflictError, match="does not advertise Client ID Metadata Document support"):
        await linkage.start(
            "test-cimd", McpLinkageStart(), OperatorPrincipal(issuer="test-issuer", subject="test-operator")
        )


if __name__ == "__main__":
    pytest_bazel.main()
