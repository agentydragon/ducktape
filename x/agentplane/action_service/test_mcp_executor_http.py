"""Pinned FastMCP/MCP client against a deterministic streamable-HTTP peer on a real socket.

Both JSON and finite SSE replies exercise the production from_group transport selection. The
peer records whole JSON-RPC payloads, and can fail after accepting a call without relying on timing.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import httpx
import pytest
import pytest_bazel
import uvicorn
from cryptography.fernet import Fernet
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from util.net import bind_free_port
from util.testing.asgi import serve_app_sync
from util.testing.mock_oidc import build_mock_oidc_app, generate_rsa_keypair
from x.agentplane.action_service.catalog import (
    ActionCatalog,
    ActionGroup,
    ActionIdentity,
    ActionUnavailableError,
    McpExecutorBinding,
)
from x.agentplane.action_service.db import ExecutionRow, make_sessionmaker
from x.agentplane.action_service.main import Settings, async_main
from x.agentplane.action_service.mcp_executor import McpActionGroupExecutor, _LinkageBearerAuth
from x.agentplane.action_service.mcp_linkage import McpLinkageStatus
from x.agentplane.action_service.models import (
    ActionRequestInput,
    ActionState,
    DecisionInput,
    ExecutionLease,
    ExecutionRequest,
    ExecutionState,
    Principal,
    PrincipalRole,
    Verdict,
)
from x.agentplane.action_service.oauth import OAuthSettings
from x.agentplane.action_service.runtime import running_executor
from x.agentplane.action_service.service import ActionService, ExecutionOutcomeUnknownError
from x.agentplane.action_service.test_fixtures.lifecycle import wait_available, wait_retry


class FakeMcpServer:
    def __init__(self, *, sse: bool) -> None:
        self.sse = sse
        self.requests: list[Request] = []
        self.posts: list[dict[str, Any]] = []
        self.tools: list[dict[str, Any]] = [
            {
                "name": "echo",
                "description": "Echo text over HTTP.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
            }
        ]
        self.list_unavailable = False
        self.call_unavailable = False
        self.tool_error = False

    async def handle(self, request: Request) -> Response:
        self.requests.append(request)
        if request.method == "GET":
            return Response(status_code=405)
        if request.method == "DELETE":
            return Response(status_code=204)
        body = await request.json()
        self.posts.append(body)
        method = body["method"]
        if method == "notifications/initialized":
            return Response(status_code=202)
        if method == "initialize":
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "protocolVersion": body["params"]["protocolVersion"],
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "test-http-peer", "version": "1"},
                    },
                },
                headers={"Mcp-Session-Id": "test-http-session"},
            )
        result: dict[str, Any]
        if method == "tools/list":
            if self.list_unavailable:
                return Response(status_code=503)
            result = {"tools": self.tools}
        elif method == "tools/call":
            if self.call_unavailable:
                # The peer accepted the call; a missing result cannot prove it did not execute.
                return Response(status_code=503)
            result = {
                "content": [{"type": "text", "text": "backend tool error text" if self.tool_error else "hi"}],
                "isError": self.tool_error,
            }
            if not self.tool_error:
                result["structuredContent"] = {
                    "echoed": body["params"]["arguments"]["text"],
                    "api_key": "test-only-backend-secret",
                }
        else:
            raise AssertionError(f"unexpected MCP method: {method}")
        response = {"jsonrpc": "2.0", "id": body["id"], "result": result}
        if self.sse:
            return Response(f"event: message\ndata: {json.dumps(response)}\n\n", media_type="text/event-stream")
        return JSONResponse(response)

    @property
    def calls(self) -> list[dict[str, Any]]:
        return [post for post in self.posts if post["method"] == "tools/call"]


@pytest.fixture(params=[False, True], ids=["json", "sse"])
def fake_server(request: pytest.FixtureRequest) -> FakeMcpServer:
    return FakeMcpServer(sse=request.param)


@pytest.fixture
def http_group(fake_server: FakeMcpServer) -> Iterator[ActionGroup]:
    app = Starlette(routes=[Route("/test-mcp", fake_server.handle, methods=["POST", "GET", "DELETE"])])
    with serve_app_sync(app) as url:
        yield ActionGroup(
            title="HTTP test group",
            description="Credentialless test peer",
            executor=McpExecutorBinding(
                kind="mcp",
                description="HTTP test peer",
                config={"transport": "streamable-http", "url": f"{url}/test-mcp", "auth": "none"},
            ),
        )


@pytest.fixture
def oauth_settings(tmp_path: Path) -> Iterator[OAuthSettings]:
    private_key, public_key = generate_rsa_keypair()
    sock = bind_free_port()
    issuer = f"http://127.0.0.1:{sock.getsockname()[1]}/application/o/test-actions/"
    secret, signing, encryption = (tmp_path / name for name in ("upstream-secret", "jwt-key", "encryption-key"))
    secret.write_text("test-only-upstream-secret")
    signing.write_text(secrets.token_urlsafe(48))
    encryption.write_bytes(Fernet.generate_key())
    idp = build_mock_oidc_app(
        issuer_url=issuer, private_key=private_key, public_key=public_key, authentik_compatible=True
    )
    with serve_app_sync(idp, sock=sock):
        yield OAuthSettings(
            config_url=f"{issuer}.well-known/openid-configuration",
            upstream_client_id="test-actions-client",
            upstream_client_secret_file=secret,
            base_url="https://actions.example.test",
            integration_app_url="https://integration.example.test",
            jwt_signing_key_file=signing,
            encryption_key_file=encryption,
            upstream_issuer=issuer,
            upstream_subject="test-user",
            approving_operator=Principal(issuer="test-http", subject="operator", role=PrincipalRole.OPERATOR),
        )


@pytest.fixture
async def executor(http_group: ActionGroup, fake_server: FakeMcpServer) -> AsyncIterator[McpActionGroupExecutor]:
    executor = McpActionGroupExecutor.from_group("remote", http_group, catalog_refresh_interval=timedelta(hours=1))
    try:
        await executor.start()
        await wait_available(http_group)
        yield executor
    finally:
        await executor.close()


@pytest.fixture
def execution_request() -> ExecutionRequest:
    return ExecutionRequest(
        request_id=uuid4(),
        action=ActionIdentity(group="remote", name="echo"),
        arguments={"text": "hi"},
        origin={},
        correlation={},
        caller_principal="test-http-caller",
    )


async def test_http_session_discovery_call_and_shutdown(
    execution_lease: ExecutionLease,
    http_group: ActionGroup,
    fake_server: FakeMcpServer,
    execution_request: ExecutionRequest,
) -> None:
    executor = McpActionGroupExecutor.from_group("remote", http_group)
    try:
        await executor.start()
        await wait_available(http_group)
        assert http_group.available
        assert set(http_group.actions) == {"echo"}
        assert http_group.actions["echo"].description == "Echo text over HTTP."
        assert http_group.actions["echo"].input_schema == fake_server.tools[0]["inputSchema"]
        result = await executor.execute(execution_request, execution_lease)
        assert result.state is ExecutionState.SUCCEEDED
        assert result.result == {"echoed": "hi", "api_key": "test-only-backend-secret"}
        assert [post["method"] for post in fake_server.posts] == [
            "initialize",
            "notifications/initialized",
            "tools/list",
            "tools/list",
            "tools/call",
        ]
        assert len(fake_server.calls) == 1
        assert fake_server.calls[0]["params"]["name"] == "echo"
        assert fake_server.calls[0]["params"]["arguments"] == {"text": "hi"}
    finally:
        await executor.close()
    assert fake_server.requests[-1].method == "DELETE"
    assert all("authorization" not in request.headers for request in fake_server.requests)
    for request in fake_server.requests[1:]:
        assert request.headers["mcp-session-id"] == "test-http-session"
        assert request.headers["mcp-protocol-version"]


async def test_http_revalidates_live_schema_before_dispatch(
    execution_lease: ExecutionLease,
    executor: McpActionGroupExecutor,
    fake_server: FakeMcpServer,
    execution_request: ExecutionRequest,
) -> None:
    fake_server.tools[0]["inputSchema"]["required"] = ["other"]
    result = await executor.execute(execution_request, execution_lease)
    assert result.state is ExecutionState.FAILED
    assert result.error is not None
    assert result.error["kind"] == "incompatible_action_schema"
    assert fake_server.calls == []


async def test_http_refresh_and_unavailable_catalog(
    executor: McpActionGroupExecutor, http_group: ActionGroup, fake_server: FakeMcpServer
) -> None:
    fake_server.tools = []
    await executor.refresh_catalog()
    assert http_group.available
    assert http_group.actions == {}
    fake_server.list_unavailable = True
    await executor.refresh_catalog()
    assert not http_group.available
    assert http_group.actions == {}


async def test_http_list_failure_refuses_dispatch(
    execution_lease: ExecutionLease,
    executor: McpActionGroupExecutor,
    fake_server: FakeMcpServer,
    execution_request: ExecutionRequest,
) -> None:
    fake_server.list_unavailable = True
    result = await executor.execute(execution_request, execution_lease)
    assert result.state is ExecutionState.FAILED
    assert result.error is not None
    assert result.error["kind"] == "mcp_unavailable"
    assert fake_server.calls == []


async def test_http_tool_error_output_is_a_successful_result(
    execution_lease: ExecutionLease,
    executor: McpActionGroupExecutor,
    fake_server: FakeMcpServer,
    execution_request: ExecutionRequest,
) -> None:
    fake_server.tool_error = True
    result = await executor.execute(execution_request, execution_lease)
    assert result.state is ExecutionState.SUCCEEDED
    assert result.error is None
    assert result.result == {"is_error": True, "content": ["backend tool error text"]}
    assert len(fake_server.calls) == 1


async def test_http_missing_call_result_is_unknown_without_retry(
    execution_lease: ExecutionLease,
    executor: McpActionGroupExecutor,
    fake_server: FakeMcpServer,
    execution_request: ExecutionRequest,
) -> None:
    fake_server.call_unavailable = True
    with pytest.raises(ExecutionOutcomeUnknownError, match="MCP tools/call transport failure"):
        await executor.execute(execution_request, execution_lease)
    assert len(fake_server.calls) == 1


async def test_unlinked_oauth_group_starts_dormant_without_connecting() -> None:
    group = ActionGroup(
        title="OAuth test group",
        description="Unlinked OAuth test peer",
        executor=McpExecutorBinding(
            kind="mcp",
            description="OAuth test peer",
            config={
                "transport": "streamable-http",
                "url": "https://unlinked.invalid/mcp",
                "auth": "oauth",
                "server_id": "github",
            },
        ),
    )
    linkage = AsyncMock()
    linkage_changed = asyncio.Event()
    linkage.subscribe_changes = Mock(return_value=linkage_changed)
    linkage.unsubscribe_changes = Mock()
    linkage.status.side_effect = [
        SimpleNamespace(status=McpLinkageStatus.UNLINKED),
        SimpleNamespace(status=McpLinkageStatus.LINKED),
    ]
    with patch("x.agentplane.action_service.mcp_executor.StreamableHttpTransport") as transport:
        executor = McpActionGroupExecutor.from_group_with_linkage("remote", group, linkage)
        connected = asyncio.Event()
        release = asyncio.Event()

        async def connect() -> None:
            connected.set()
            await release.wait()

        with patch.object(executor, "_connect_client", connect):
            await executor.start()
            try:
                await asyncio.sleep(0)
                assert not group.available
                assert group.actions == {}
                assert transport.call_count == 0
                linkage.status.assert_awaited()
                linkage_changed.set()
                async with asyncio.timeout(1):
                    await connected.wait()
            finally:
                await executor.close()


async def test_oauth_auth_resolves_the_current_token_for_each_request() -> None:
    linkage = AsyncMock()
    linkage.access_token_for_execution.side_effect = ["token-one", "token-two"]
    auth = _LinkageBearerAuth(linkage, "github")

    async def authenticated(request: httpx.Request) -> httpx.Request:
        async for result in auth.async_auth_flow(request):
            return cast(httpx.Request, result)
        raise AssertionError("auth flow did not yield a request")

    first = await authenticated(httpx.Request("GET", "https://test.invalid/mcp"))
    second = await authenticated(httpx.Request("GET", "https://test.invalid/mcp"))
    assert first.headers["Authorization"] == "Bearer token-one"
    assert second.headers["Authorization"] == "Bearer token-two"


@pytest.mark.parametrize(
    "config",
    [
        {"transport": "streamable-http", "url": "file:///tmp/test-mcp"},
        {"transport": "streamable-http", "url": "https://test.invalid/mcp?token=test-only-secret"},
        {"transport": "streamable-http", "url": "http://test-user@test.invalid/mcp"},
        {"transport": "streamable-http", "url": "https://test.invalid/mcp#fragment"},
        {"transport": "streamable-http", "url": "https://test.invalid/mcp", "command": "test-server"},
        {"transport": "streamable-http", "url": "https://test.invalid/mcp", "headers": {}},
        {"transport": "streamable-http", "url": "https://test.invalid/mcp", "auth": "oauth"},
        {"transport": "sse", "url": "https://test.invalid/mcp"},
    ],
)
def test_unsupported_http_config_fails_before_connecting(config: dict[str, Any]) -> None:
    group = ActionGroup(
        title="Invalid HTTP test group",
        description="No connection should be attempted",
        executor=McpExecutorBinding(kind="mcp", description="Invalid test peer", config=config),
    )
    with pytest.raises(ValidationError):
        McpActionGroupExecutor.from_group("remote", group)


@pytest.mark.parametrize("invalid", ["schema", "duplicate"])
async def test_invalid_discovery_clears_stale_actions(
    executor: McpActionGroupExecutor, http_group: ActionGroup, fake_server: FakeMcpServer, invalid: str
) -> None:
    assert "echo" in http_group.actions
    if invalid == "schema":
        fake_server.tools[0]["inputSchema"] = {"type": "test-invalid-type"}
    else:
        fake_server.tools.append(fake_server.tools[0])
    await executor.refresh_catalog()
    assert not http_group.available
    assert http_group.actions == {}
    assert fake_server.calls == []


async def test_invalid_live_schema_refuses_before_call(
    executor: McpActionGroupExecutor,
    fake_server: FakeMcpServer,
    execution_request: ExecutionRequest,
    execution_lease: ExecutionLease,
) -> None:
    fake_server.tools[0]["inputSchema"] = {"type": "test-invalid-type"}
    result = await executor.execute(execution_request, execution_lease)
    assert result.error == {"kind": "mcp_invalid_schema", "message": "backend tool schema is invalid"}
    assert fake_server.calls == []


@pytest.mark.parametrize("failure", ["unavailable", "invalid_schema"])
async def test_runtime_serves_unavailable_group_and_recovers_without_restart(
    http_group: ActionGroup, fake_server: FakeMcpServer, failure: str
) -> None:
    if failure == "unavailable":
        fake_server.list_unavailable = True
    else:
        fake_server.tools[0]["inputSchema"] = {"type": "test-invalid-type"}
    async with running_executor(ActionCatalog(groups={"remote": http_group})):
        assert not http_group.available
        await wait_retry(http_group)
        assert http_group.health is not None
        assert http_group.actions == {}
        assert str(http_group.executor.config["url"]) not in http_group.health.model_dump_json()
        assert fake_server.calls == []
        fake_server.list_unavailable = False
        fake_server.tools[0]["inputSchema"] = {"type": "object"}
        await wait_available(http_group)
        assert set(http_group.actions) == {"echo"}


async def test_main_oauth_serves_during_backend_outage_and_recovers(
    db_url: str, http_group: ActionGroup, fake_server: FakeMcpServer, oauth_settings: OAuthSettings
) -> None:
    """Real production startup, OAuth persistence, and MCP transport; no backend-readiness startup gate."""
    fake_server.list_unavailable = True
    settings = Settings(
        database_url=db_url, action_groups={"remote": http_group}, oauth=oauth_settings, _cli_parse_args=False
    )

    async def serve(server: uvicorn.Server) -> None:
        app = cast(FastAPI, server.config.app)
        service = cast(ActionService, app.state.action_service)
        await wait_retry(http_group)
        assert not http_group.available
        assert fake_server.calls == []
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=oauth_settings.base_url) as client:
            assert (await client.get("/healthz")).status_code == 200
            assert (await client.get("/readyz")).status_code == 200
            metadata = await client.get("/.well-known/oauth-authorization-server")
            assert metadata.status_code == 200
            registration = await client.post(
                metadata.json()["registration_endpoint"],
                json={
                    "client_name": "Test outage client",
                    "redirect_uris": ["https://client.example.test/callback"],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                    "scope": "openid email profile offline_access",
                },
            )
            assert registration.status_code == 201
            assert registration.json()["client_id"]
            fake_server.list_unavailable = False
            await wait_available(http_group)
            caller = Principal(issuer="test-http", subject="sandbox", role=PrincipalRole.CALLER)
            pending = await service.submit(
                ActionRequestInput(
                    idempotency_key="after-outage",
                    action=ActionIdentity(group="remote", name="echo"),
                    arguments={"text": "recovered"},
                ),
                caller,
            )
            await service.decide(
                pending.id,
                DecisionInput(verdict=Verdict.ALLOW, expected_version=pending.version, idempotency_key="allow"),
                oauth_settings.approving_operator,
            )
            async with asyncio.timeout(10):
                while (final := await service.get(pending.id, caller)).state is not ActionState.SUCCEEDED:
                    pass  # Database reads yield until the durable result is published.
            assert final.execution is not None
            assert final.execution.result == {"echoed": "recovered", "api_key": "[redacted]"}
            assert len(fake_server.calls) == 1
            assert (await client.get("/.well-known/oauth-authorization-server")).json() == metadata.json()

    with (
        patch("x.agentplane.action_service.main.k8s_config.load_incluster_config"),
        patch.object(uvicorn.Server, "serve", serve),
    ):
        await async_main(settings)


@pytest.mark.parametrize("outcome", ["success", "tool_error", "unknown", "schema_mismatch", "invalid_schema"])
async def test_production_http_composition_one_execution_no_replay(
    db_url: str, engine: AsyncEngine, http_group: ActionGroup, fake_server: FakeMcpServer, outcome: str
) -> None:
    """Real main, HTTP MCP, and PostgreSQL; only Kubernetes setup and the serving loop are replaced."""
    caller = Principal(issuer="test-http", subject="sandbox", role=PrincipalRole.CALLER)
    operator = Principal(issuer="test-http", subject="operator", role=PrincipalRole.OPERATOR)
    settings = Settings(database_url=db_url, action_groups={"remote": http_group}, _cli_parse_args=False)

    async def serve(server: uvicorn.Server) -> None:
        app = cast(FastAPI, server.config.app)
        service = cast(ActionService, app.state.action_service)
        catalog = cast(ActionCatalog, app.state.action_catalog)
        await wait_available(http_group)
        assert catalog.action_view("remote", "echo").input_schema == fake_server.tools[0]["inputSchema"]
        assert str(http_group.executor.config["url"]) not in "".join(
            view.model_dump_json() for view in catalog.group_views()
        )
        body = ActionRequestInput(
            idempotency_key="http-once", action=ActionIdentity(group="remote", name="echo"), arguments={"text": "hi"}
        )
        pending = await service.submit(body, caller)
        assert pending.state is ActionState.DECISION_PENDING
        assert fake_server.calls == []
        fake_server.tool_error = outcome == "tool_error"
        fake_server.call_unavailable = outcome == "unknown"
        if outcome == "schema_mismatch":
            fake_server.tools[0]["inputSchema"]["required"] = ["other"]
        if outcome == "invalid_schema":
            fake_server.tools[0]["inputSchema"] = {"type": "test-invalid-type"}
        decision = DecisionInput(verdict=Verdict.ALLOW, expected_version=pending.version, idempotency_key="allow-once")
        await service.decide(pending.id, decision, operator)
        expected = {
            "success": ActionState.SUCCEEDED,
            "tool_error": ActionState.SUCCEEDED,
            "unknown": ActionState.EXECUTION_UNKNOWN,
            "schema_mismatch": ActionState.FAILED,
            "invalid_schema": ActionState.FAILED,
        }[outcome]
        async with asyncio.timeout(10):
            while (final := await service.get(pending.id, caller)).state is not expected:
                pass  # Each database read yields; wait for durable completion, not an elapsed delay.
        assert final.execution is not None
        assert final.execution.result == {
            "success": {"echoed": "hi", "api_key": "[redacted]"},
            "tool_error": {"is_error": True, "content": ["backend tool error text"]},
        }.get(outcome)
        if outcome not in {"success", "tool_error"}:
            assert final.execution.error is not None
            assert (
                final.execution.error["kind"]
                == {
                    "unknown": "execution_outcome_unknown",
                    "schema_mismatch": "incompatible_action_schema",
                    "invalid_schema": "mcp_invalid_schema",
                }[outcome]
            )
        for principal in (caller, operator):
            view = await service.get(pending.id, principal)
            assert "test-only" not in view.model_dump_json()
            assert str(http_group.executor.config["url"]) not in view.model_dump_json()
        if outcome == "invalid_schema":
            # Execution-time detection cleared the offered catalog; new submissions see the outage
            # until the backend publishes a valid schema again.
            with pytest.raises(ActionUnavailableError):
                await service.submit(body, caller)
            fake_server.tools[0]["inputSchema"] = {"type": "object"}
            await wait_available(http_group)
        assert (await service.submit(body, caller)).execution == final.execution
        assert (await service.decide(pending.id, decision, operator)).execution == final.execution
        # Duplicate dispatch and restart recovery must both respect the durable single-Execution claim.
        await service._dispatch_once(pending.id)
        await service.close()
        await service.start()
        assert (await service.get(pending.id, caller)).execution == final.execution
        assert len(fake_server.calls) == (0 if outcome in {"schema_mismatch", "invalid_schema"} else 1)
        async with make_sessionmaker(engine)() as session:
            assert await session.scalar(select(func.count()).select_from(ExecutionRow)) == 1
        if fake_server.calls:
            assert fake_server.calls[0]["params"]["arguments"] == {"text": "hi"}

    with (
        patch("x.agentplane.action_service.main.k8s_config.load_incluster_config"),
        patch.object(uvicorn.Server, "serve", serve),
    ):
        await async_main(settings)
    assert not any(task.get_name().startswith("mcp-executor-refresh-") for task in asyncio.all_tasks())


async def test_static_credential_mount_recovers(http_group: ActionGroup, tmp_path: Path) -> None:
    token_file = tmp_path / "bearer"
    http_group.executor.config.update(auth="static_bearer", bearer_file=str(token_file))
    async with running_executor(ActionCatalog(groups={"remote": http_group})):
        await wait_retry(http_group)
        assert http_group.health is not None
        assert not http_group.available
        token_file.write_text("test-only-mounted-token")
        await wait_available(http_group)
        assert set(http_group.actions) == {"echo"}


async def test_established_http_failure_reconnects_fresh_session(
    executor: McpActionGroupExecutor,
    http_group: ActionGroup,
    fake_server: FakeMcpServer,
    execution_request: ExecutionRequest,
    execution_lease: ExecutionLease,
) -> None:
    old = executor._connection
    fake_server.list_unavailable = True
    result = await executor.execute(execution_request, execution_lease)
    assert result.state is ExecutionState.FAILED
    assert fake_server.calls == []
    assert not http_group.available
    fake_server.list_unavailable = False
    await wait_available(http_group)
    assert executor._connection is not old
    assert sum(post["method"] == "initialize" for post in fake_server.posts) >= 2
    assert (await executor.execute(execution_request, execution_lease)).state is ExecutionState.SUCCEEDED
    assert len(fake_server.calls) == 1


if __name__ == "__main__":
    pytest_bazel.main()
