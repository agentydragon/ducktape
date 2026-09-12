"""Hermetic, in-process rehearsal of the deployed OAuth MCP fixture: linkage completes and the
echo tool executes with the resulting token, the same shape the acceptance suite exercises
against the real cluster (`//x/agentplane/acceptance:test_mcp`), without needing one."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest_bazel
from sqlalchemy.ext.asyncio import AsyncEngine

from util.net import bind_free_port
from util.testing.asgi import serve_app_sync
from x.agentplane.action_service.catalog import ActionGroup, ActionIdentity, McpExecutorBinding
from x.agentplane.action_service.db import make_sessionmaker
from x.agentplane.action_service.mcp_executor import McpActionGroupExecutor
from x.agentplane.action_service.mcp_linkage import (
    McpLinkageAuthority,
    McpLinkageStart,
    McpLinkageStatus,
    McpOAuthServer,
    McpProvider,
)
from x.agentplane.action_service.models import ExecutionLease, ExecutionRequest, Principal, PrincipalRole
from x.agentplane.action_service.test_fixtures.lifecycle import wait_available
from x.agentplane.action_service.test_fixtures.oauth_mcp_server import CLIENT_ID, PATH, build_app, build_dex_app

REDIRECT_URI = "https://app.example.test/mcp-linkage/callback"


async def test_dex_app_exposes_remote_oauth_metadata() -> None:
    app = build_dex_app(
        base_url="http://fixture.example.test",
        authorization_server="https://dex.example.test/dex",
        jwks_uri="http://dex.example.test/dex/keys",
        audience="agentplane-testing-mcp",
    ).http_app(path=PATH)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture.example.test"
    ) as http:
        response = await http.get("/.well-known/oauth-protected-resource/mcp")

    assert response.status_code == 200, response.text
    assert response.json()["authorization_servers"] == ["https://dex.example.test/dex"]
    assert response.json()["scopes_supported"] == ["openid"]


async def test_full_linkage_cycle_and_tool_call(engine: AsyncEngine, execution_lease: ExecutionLease) -> None:
    sock = bind_free_port()
    host, port = sock.getsockname()
    base_url = f"http://{host}:{port}"
    app = build_app(base_url=base_url, redirect_uri=REDIRECT_URI).http_app(path=PATH)
    with serve_app_sync(app, sock=sock):
        server = McpOAuthServer(
            server_id="example",
            provider=McpProvider.EXAMPLE,
            server_url=f"{base_url}{PATH}",
            client_id=CLIENT_ID,
            redirect_uri=REDIRECT_URI,
        )
        async with httpx.AsyncClient(follow_redirects=False) as http:
            authority = McpLinkageAuthority(make_sessionmaker(engine), {"example": server}, http=http)
            operator = Principal(issuer="test-oauth-fixture", subject="operator", role=PrincipalRole.OPERATOR)

            start = await authority.start("example", McpLinkageStart(), operator)
            authorize = await http.get(start.authorization_url)
            assert authorize.status_code == 302, authorize.text
            query = parse_qs(urlsplit(authorize.headers["location"]).query)
            view = await authority.callback(query["state"][0], query["code"][0])
            assert view.status is McpLinkageStatus.LINKED

            group = ActionGroup(
                title="OAuth fixture",
                description="Self-contained OAuth MCP fixture",
                executor=McpExecutorBinding(
                    kind="mcp",
                    description="OAuth fixture",
                    config={
                        "transport": "streamable-http",
                        "url": server.server_url,
                        "auth": "oauth",
                        "server_id": "example",
                    },
                ),
            )
            executor = McpActionGroupExecutor.from_group_with_linkage("example", group, authority)
            try:
                await executor.start()
                await wait_available(group)
                result = await executor.execute(
                    ExecutionRequest(
                        request_id=uuid4(),
                        action=ActionIdentity(group="example", name="echo"),
                        arguments={"message": "hi"},
                        origin={},
                        correlation={},
                        caller_principal="test-caller",
                    ),
                    execution_lease,
                )
                assert result.result == {"result": "Echo: hi"}
            finally:
                await executor.close()


if __name__ == "__main__":
    pytest_bazel.main()
