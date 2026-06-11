"""Integration test: boots create_app() and verifies REST + MCP work together.

This catches the bug where the MCP sub-app lifespan (storage init) didn't fire
at startup, causing /api/actions to return 500.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_bazel
from fastmcp import FastMCP
from fastmcp.mcp_config import RemoteMCPServer
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair
from starlette.applications import Starlette
from starlette.routing import Mount

from airlock.app import create_app
from airlock.config import Settings
from airlock.conftest import GateClient, agent_transport, serve_app
from airlock.oauth.provider import OAuthConfig
from mcp_infra.prefix import MCPMountPrefix
from util.net import pick_free_port


@pytest.fixture
def rsa_key_pair() -> RSAKeyPair:
    return RSAKeyPair.generate()


@pytest.fixture
def predicate_file(tmp_path: Path) -> Path:
    p = tmp_path / "predicate.py"
    p.write_text(
        dedent("""
        from airlock.predicates import NeedsHumanDecision

        def decide(server_namespace, tool_name, arguments):
            return NeedsHumanDecision()
    """).lstrip()
    )
    return p


@pytest.fixture
def _mock_k8s_store():
    """Patch K8sTokenStore.from_incluster since tests don't run in a k8s pod."""
    mock_store = MagicMock()
    mock_store.list_secrets = AsyncMock(return_value=[])
    mock_store.delete_orphaned_secrets = AsyncMock()
    with patch("airlock.app.K8sTokenStore.from_incluster", new_callable=AsyncMock, return_value=mock_store):
        yield


@pytest.mark.usefixtures("_mock_k8s_store")
async def test_rest_api_works_after_startup(rsa_key_pair: RSAKeyPair, predicate_file: Path, db_url: str):
    """GET /api/actions returns 200 immediately — no MCP client needed."""
    port = pick_free_port()
    settings = Settings(
        backends={},
        public_base_url=f"http://127.0.0.1:{port}",
        db_url=db_url,
        predicate_path=predicate_file,
        oidc_issuer="https://unused.example.com",
        oidc_client_id="test",
        oauth=OAuthConfig(providers=[]),
        port=port,
    )
    auth = JWTVerifier(public_key=rsa_key_pair.public_key)
    app = create_app(settings, auth=auth, include_static=False)
    async with serve_app(app, port=port), httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
        r = await http.get("/healthz")
        assert r.status_code == 200

        # This is the exact request that was returning 500 before the fix.
        r = await http.get("/api/actions")
        assert r.status_code == 200
        assert r.json() == []


@pytest.mark.usefixtures("_mock_k8s_store")
async def test_mcp_action_visible_via_rest(rsa_key_pair: RSAKeyPair, predicate_file: Path, db_url: str):
    """An action created via MCP appears in GET /api/actions."""
    port = pick_free_port()
    echo = FastMCP("echo")

    @echo.tool()
    async def echo_tool(text: str) -> str:
        return f"echoed: {text}"

    echo_port = pick_free_port()
    echo_app = echo.http_app(path="/")
    echo_starlette = Starlette(routes=[Mount("/mcp", app=echo_app)], lifespan=echo_app.lifespan)

    settings = Settings(
        backends={MCPMountPrefix("test"): RemoteMCPServer(url=f"http://127.0.0.1:{echo_port}/mcp")},
        public_base_url=f"http://127.0.0.1:{port}",
        db_url=db_url,
        predicate_path=predicate_file,
        oidc_issuer="https://unused.example.com",
        oidc_client_id="test",
        oauth=OAuthConfig(providers=[]),
        port=port,
    )
    auth = JWTVerifier(public_key=rsa_key_pair.public_key)
    agent_jwt = rsa_key_pair.create_token(subject="agent", scopes=["propose", "read"])

    app = create_app(settings, auth=auth, include_static=False)
    async with serve_app(echo_starlette, port=echo_port), serve_app(app, port=port):
        # Create an action via MCP (tool is namespace_toolname = test_echo_tool).
        async with GateClient(agent_transport(f"http://127.0.0.1:{port}", agent_jwt)) as client:
            action = await client.call_gate_tool(
                "test_echo_tool",
                {
                    "input": {"text": "hello"},
                    "justification": "test",
                    "session_key": "a0000000-0000-0000-0000-000000000001",
                },
            )
            assert action.state.status.value == "pending"

        # Verify action is visible via REST.
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
            r = await http.get("/api/actions")
            assert r.status_code == 200
            actions = r.json()
            assert len(actions) == 1
            assert actions[0]["call"]["tool_name"] == "echo_tool"


if __name__ == "__main__":
    pytest_bazel.main()
