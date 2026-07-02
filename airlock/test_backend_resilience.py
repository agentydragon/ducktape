"""Integration tests: airlock resilience when backends are unavailable.

Covers:
1. Airlock starts cleanly when a backend is initially unreachable
2. /api/backends reports degraded/connected status correctly
3. A degraded backend reconnects automatically when it becomes available
4. An approved action returns an isError result when its backend is unavailable
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from textwrap import dedent
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import httpx
import pytest
import pytest_bazel
from fastmcp.mcp_config import RemoteMCPServer
from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.applications import Starlette
from starlette.routing import Mount

from airlock.app import create_app
from airlock.config import Settings
from airlock.conftest import EchoBackend, GateClient, agent_transport, operator_transport, serve_app
from airlock.models import ActionStatus
from airlock.oauth.provider import OAuthConfig
from mcp_infra.prefix import MCPMountPrefix
from util.net import pick_free_port

TEST_NS = MCPMountPrefix("test")


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


def _make_settings(
    *, backends: dict, port: int, db_url: str, predicate_file: Path, reconnect_interval_s: float = 30.0
) -> Settings:
    """Build a Settings object for test apps."""
    return Settings(
        backends=backends,
        public_base_url=f"http://127.0.0.1:{port}",
        db_url=db_url,
        predicate_path=predicate_file,
        oidc_issuer="https://unused.example.com",
        oidc_client_id="test",
        reconnect_interval_s=reconnect_interval_s,
        oauth=OAuthConfig(providers=[]),
        port=port,
    )


@pytest.mark.usefixtures("_mock_k8s_store")
async def test_airlock_starts_with_backend_down(
    rsa_key_pair, agent_jwt: str, predicate_file: Path, db_url: str, operator_headers: dict[str, str]
):
    """Airlock starts and serves /mcp even when a backend URL is unreachable."""
    dead_port = pick_free_port()
    gate_port = pick_free_port()
    auth = JWTVerifier(public_key=rsa_key_pair.public_key)

    settings = _make_settings(
        backends={TEST_NS: RemoteMCPServer(url=f"http://127.0.0.1:{dead_port}/mcp")},
        port=gate_port,
        db_url=db_url,
        predicate_file=predicate_file,
    )
    app = create_app(settings, auth=auth, include_static=False)

    async with (
        serve_app(app, port=gate_port),
        httpx.AsyncClient(base_url=f"http://127.0.0.1:{gate_port}", headers=operator_headers) as http,
    ):
        r = await http.get("/healthz")
        assert r.status_code == 200

        # Backend is degraded in the status API.
        r = await http.get("/api/backends")
        assert r.status_code == 200
        backends = r.json()
        assert len(backends) == 1
        assert backends[0]["name"] == str(TEST_NS)
        assert backends[0]["connection_status"]["state"] == "degraded"
        assert "error" in backends[0]["connection_status"]

        # No tools from the dead backend appear via MCP.
        async with GateClient(agent_transport(f"http://127.0.0.1:{gate_port}", agent_jwt)) as agent:
            tools = await agent.list_tools()
            tool_names = {t.name for t in tools}
            # Only built-in gate tools (list_actions, approve_action, etc.) — no test_* tools.
            assert not any(name.startswith("test_") for name in tool_names)


@pytest.mark.usefixtures("_mock_k8s_store")
async def test_api_backends_connected_when_backend_up(
    rsa_key_pair, predicate_file: Path, db_url: str, echo_backend: EchoBackend, operator_headers: dict[str, str]
):
    """GET /api/backends reports connected when backend is reachable."""
    echo_port = pick_free_port()
    gate_port = pick_free_port()
    auth = JWTVerifier(public_key=rsa_key_pair.public_key)

    echo_mcp_app = echo_backend.server.http_app(path="/")
    echo_app = Starlette(routes=[Mount("/mcp", app=echo_mcp_app)], lifespan=echo_mcp_app.lifespan)
    settings = _make_settings(
        backends={TEST_NS: RemoteMCPServer(url=f"http://127.0.0.1:{echo_port}/mcp")},
        port=gate_port,
        db_url=db_url,
        predicate_file=predicate_file,
    )
    app = create_app(settings, auth=auth, include_static=False)

    async with (
        serve_app(echo_app, port=echo_port),
        serve_app(app, port=gate_port),
        httpx.AsyncClient(base_url=f"http://127.0.0.1:{gate_port}", headers=operator_headers) as http,
    ):
        r = await http.get("/api/backends")
        assert r.status_code == 200
        backends = r.json()
        assert len(backends) == 1
        assert backends[0]["connection_status"]["state"] == "connected"


@pytest.mark.usefixtures("_mock_k8s_store")
async def test_backend_reconnects_when_available(
    rsa_key_pair,
    agent_jwt: str,
    predicate_file: Path,
    db_url: str,
    echo_backend: EchoBackend,
    operator_headers: dict[str, str],
):
    """After airlock starts with a dead backend, it reconnects once the backend comes up."""
    echo_port = pick_free_port()
    gate_port = pick_free_port()
    auth = JWTVerifier(public_key=rsa_key_pair.public_key)

    settings = _make_settings(
        backends={TEST_NS: RemoteMCPServer(url=f"http://127.0.0.1:{echo_port}/mcp")},
        port=gate_port,
        db_url=db_url,
        predicate_file=predicate_file,
        reconnect_interval_s=0.1,
    )
    app = create_app(settings, auth=auth, include_static=False)

    echo_mcp_app = echo_backend.server.http_app(path="/")
    echo_starlette = Starlette(routes=[Mount("/mcp", app=echo_mcp_app)], lifespan=echo_mcp_app.lifespan)

    async with serve_app(app, port=gate_port):
        # Initially degraded — no tools from test namespace.
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gate_port}", headers=operator_headers) as http:
            r = await http.get("/api/backends")
            assert r.json()[0]["connection_status"]["state"] == "degraded"

        # Bring backend up.
        async with serve_app(echo_starlette, port=echo_port):
            # Poll until airlock detects the backend (up to 5s).
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gate_port}", headers=operator_headers) as http:
                with anyio.fail_after(5.0):
                    while True:
                        r = await http.get("/api/backends")
                        if r.json()[0]["connection_status"]["state"] == "connected":
                            break
                        await asyncio.sleep(0.1)

            # Tools from the backend are now available.
            async with GateClient(agent_transport(f"http://127.0.0.1:{gate_port}", agent_jwt)) as agent:
                tools = await agent.list_tools()
                assert any(t.name.startswith("test_") for t in tools)


@pytest.mark.usefixtures("_mock_k8s_store")
async def test_approved_action_errors_when_backend_unavailable(
    rsa_key_pair,
    agent_jwt: str,
    operator_jwt: str,
    predicate_file: Path,
    db_url: str,
    echo_backend: EchoBackend,
    operator_headers: dict[str, str],
):
    """When a backend goes down after tools are registered, approved actions complete
    with isError=true rather than getting stuck in EXECUTING."""
    echo_port = pick_free_port()
    gate_port = pick_free_port()
    auth = JWTVerifier(public_key=rsa_key_pair.public_key)

    # Use a long reconnect interval so the reconnect loop doesn't interfere.
    settings = _make_settings(
        backends={TEST_NS: RemoteMCPServer(url=f"http://127.0.0.1:{echo_port}/mcp")},
        port=gate_port,
        db_url=db_url,
        predicate_file=predicate_file,
        reconnect_interval_s=3600.0,
    )

    echo_mcp_app = echo_backend.server.http_app(path="/")
    echo_starlette = Starlette(routes=[Mount("/mcp", app=echo_mcp_app)], lifespan=echo_mcp_app.lifespan)

    # Phase 1: Both servers up — queue an action (stays PENDING).
    async with serve_app(echo_starlette, port=echo_port):
        app = create_app(settings, auth=auth, include_static=False)
        async with (
            serve_app(app, port=gate_port),
            GateClient(agent_transport(f"http://127.0.0.1:{gate_port}", agent_jwt)) as agent,
        ):
            action = await agent.call_gate_tool(
                "test_echo",
                {
                    "input": {"text": "hello"},
                    "justification": "test",
                    "session_key": "a0000000-0000-0000-0000-000000000001",
                },
            )
            assert action.state.status == ActionStatus.PENDING
            created_key = action.key
        # gate stopped
    # echo stopped — both servers are now down

    # Phase 2: Restart gate only (echo remains down) → backend is degraded.
    # Approving the pending action must yield isError rather than hanging.
    app2 = create_app(settings, auth=auth, include_static=False)
    async with serve_app(app2, port=gate_port):
        async with GateClient(operator_transport(f"http://127.0.0.1:{gate_port}", operator_jwt)) as operator:
            await operator.approve(created_key)

        # Poll until the action reaches DONE (background pipeline runs asynchronously).
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gate_port}", headers=operator_headers) as http:
            with anyio.fail_after(5.0):
                while True:
                    r = await http.get(f"/api/actions/{created_key.session_key}/{created_key.action_seq}")
                    data = r.json()
                    if data["state"]["status"] == "done":
                        break
                    await asyncio.sleep(0.05)

        assert data["state"]["status"] == "done"
        # Backend was degraded at approval time — outcome must signal failure.
        assert data["state"]["outcome"]["isError"] is True


if __name__ == "__main__":
    pytest_bazel.main()
