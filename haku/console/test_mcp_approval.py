"""Operator-approved MCP tool-call API tests."""

from __future__ import annotations

import datetime
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import pytest
import pytest_bazel
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastmcp import FastMCP
from mcp import types as mcp_types
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.websockets import WebSocketDisconnect

from haku.console.agents.authorization import fingerprint_static_token
from haku.console.agents.models import (
    AgentStatus,
    ClientRegistrationKind,
    CredentialBindingStatus,
    CredentialKind,
    EnrollmentPhase,
)
from haku.console.conftest import csrf_token, operator_id, write_config
from haku.console.database_migrate import apply_migrations
from haku.console.database_schema import (
    Agent,
    CredentialBinding,
    McpOperatorOAuthAssociation,
    McpToolCall,
    McpToolCallPrincipal,
    StaticCredential,
)
from haku.console.mcp_approval import (
    AliveServerMetadata,
    McpMetadataProvider,
    McpToolExecutor,
    PostgresToolCallLedger,
    _execution_auth,
    _mcp_result_to_json,
)
from haku.console.mcp_config import McpServerEntry, const_in_process_server
from haku.console.mcp_operator_oauth import PostgresMcpOperatorOAuthStore
from haku.console.operator_identity import OperatorStatus
from haku.console.tool_call_actor import AgentActor, OperatorActor, ToolCallActor
from haku.console.tool_call_service import backend_auth_for_operator
from haku.console.tool_calls import AgentToolCallCaller, OperatorToolCallCaller, SubmitToolCallRequest, ToolCallStatus
from haku.console.tools.gmail import build_mcp as build_gmail_mcp
from util.net import pick_free_port
from util.testing.asgi import serve_app_sync, serve_fastmcp


def _test_mcp_server() -> FastMCP:
    server = FastMCP("haku-console-test")

    @server.tool()
    async def stock_add(items: list[dict[str, Any]]) -> str:
        """Add stock items to the test inventory."""
        item = items[0]
        return f"stock_add:{item['product_id']}:{item['amount']}"

    @server.tool()
    async def echo(text: str) -> str:
        """Echo a test string."""
        return f"echo:{text}"

    @server.tool()
    async def products_list(detail: str = "brief") -> list[dict[str, Any]]:
        """List products, mirroring grocy-sf's products_list. The reference lookup asks for
        `detail="full"`; Grocy returns numeric columns as strings, so mirror that here."""
        assert detail == "full", f"reference lookup should request full detail, got {detail!r}"
        return [
            {
                "id": 1,
                "name": "Milk",
                "location_id": "2",
                "qu_id_stock": "3",
                "qu_id_purchase": "3",
                "qu_id_consume": "3",
                "min_stock_amount": "1.0",
                "default_best_before_days": "7",
                "due_type": "1",
                "parent_product_id": "0",
                "product_group_id": "4",
                "description": "Whole milk",
                "calories": None,
            }
        ]

    @server.tool()
    async def locations_list() -> list[dict[str, Any]]:
        """List locations, mirroring grocy-sf's locations_list(detail="brief")."""
        return [{"id": 2, "name": "Fridge"}]

    @server.tool()
    async def quantity_units_list() -> list[dict[str, Any]]:
        """List quantity units, mirroring grocy-sf's quantity_units_list(detail="brief")."""
        return [{"id": 3, "name": "Liter"}]

    @server.tool()
    async def product_groups_list() -> list[dict[str, Any]]:
        """List product groups, mirroring grocy-sf's product_groups_list(detail="brief")."""
        return [{"id": 4, "name": "Dairy"}]

    @server.tool()
    async def shopping_lists_list() -> list[dict[str, Any]]:
        """List shopping lists, mirroring grocy-sf's shopping_lists_list(detail="brief")."""
        return [{"id": 5, "name": "Weekly"}]

    @server.tool()
    async def shopping_list_get(shopping_list: int | str) -> dict[str, Any]:
        """Fetch one shopping list's items, mirroring grocy-sf's shopping_list_get. The reference
        lookup calls this once per list (by id) to flatten every item for the shopping-list
        edit/remove previews; a dict return is inlined into structured content (items at the top
        level, not under "result")."""
        assert shopping_list == 5, f"reference lookup should pass the list id, got {shopping_list!r}"
        return {
            "name": "Weekly",
            "description": None,
            "items": [
                {
                    "item_id": 11,
                    "product_name": "Milk",
                    "product_id": 1,
                    "amount": 2.0,
                    "qu_name": "Liter",
                    "note": None,
                    "done": False,
                },
                {
                    "item_id": 12,
                    "product_name": None,
                    "product_id": None,
                    "amount": 1.0,
                    "qu_name": None,
                    "note": "paper towels?",
                    "done": False,
                },
            ],
        }

    return server


@contextmanager
def _serve_remote_oauth(*, static_client_id: str | None = None) -> Generator[str]:
    """A fake OAuth server. With `static_client_id` set, the metadata omits
    `registration_endpoint` and no `/auth/register` route is mounted at all — mirroring
    Authentik (fronted by kubernetes-mcp-server), which has no DCR endpoint — so the test
    fails loudly if the client under test attempts dynamic registration anyway.
    """
    port = pick_free_port()
    base_url = f"http://127.0.0.1:{port}"
    expected_client_id = static_client_id or "dynamic-client"

    async def mcp(request: Request) -> JSONResponse:
        return JSONResponse(
            {"detail": "auth required"},
            status_code=401,
            headers={
                "WWW-Authenticate": f'Bearer resource_metadata="{base_url}/.well-known/oauth-protected-resource/mcp"'
            },
        )

    async def protected_resource(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "resource": f"{base_url}/mcp",
                "authorization_servers": [f"{base_url}/auth"],
                "scopes_supported": ["openid", "profile", "offline_access"],
            }
        )

    async def oauth_metadata(request: Request) -> JSONResponse:
        metadata = {
            "issuer": f"{base_url}/auth",
            "authorization_endpoint": f"{base_url}/auth/authorize",
            "token_endpoint": f"{base_url}/auth/token",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
        }
        if static_client_id is None:
            metadata["registration_endpoint"] = f"{base_url}/auth/register"
        return JSONResponse(metadata)

    async def register(request: Request) -> JSONResponse:
        body = await request.json()
        return JSONResponse(
            {
                **body,
                "client_id": "dynamic-client",
                "client_secret": None,
                "client_id_issued_at": 1,
                "token_endpoint_auth_method": "none",
            },
            status_code=201,
        )

    async def token(request: Request) -> JSONResponse:
        form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
        if form["grant_type"] == "authorization_code":
            assert form["code"] == "operator-code"
            assert form["client_id"] == expected_client_id
            assert form["code_verifier"]
            return JSONResponse(
                {
                    "access_token": "operator-access-token",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "refresh_token": "operator-refresh-token",
                    "scope": form.get("scope", "openid profile offline_access"),
                }
            )
        assert form["grant_type"] == "refresh_token"
        assert form["refresh_token"] == "operator-refresh-token"
        return JSONResponse(
            {
                "access_token": "operator-refreshed-token",
                "token_type": "Bearer",
                "expires_in": 3600,
                "refresh_token": "operator-refresh-token",
            }
        )

    routes = [
        Route("/mcp", mcp),
        Route("/.well-known/oauth-protected-resource/mcp", protected_resource),
        Route("/.well-known/oauth-protected-resource", protected_resource),
        Route("/.well-known/oauth-authorization-server/auth", oauth_metadata),
        Route("/auth/token", token, methods=["POST"]),
    ]
    if static_client_id is None:
        routes.append(Route("/auth/register", register, methods=["POST"]))
    app = Starlette(routes=routes)
    with serve_app_sync(app, port=port):
        yield base_url


@pytest.fixture
def remote_oauth_url() -> Generator[str]:
    with _serve_remote_oauth() as url:
        yield url


@pytest.fixture
def preregistered_remote_oauth_url() -> Generator[str]:
    with _serve_remote_oauth(static_client_id="preregistered-client") as url:
        yield url


# The Postgres testcontainer + per-test database fixtures (`db_url`, `migrated_db_url`, `make_client`)
# live in conftest.py. `make_client` wires the app to a fresh migrated database automatically, so
# tests only pass the overrides they exercise.

# A static agent `haku` (bearer `tool-token`, acting as operator subject `op-haku`), referenced from
# a config file's `static_agents` and resolved from these env vars — like the deploy.
_AGENT_TOKEN = "tool-token"
_AGENT_TOKEN_ENV = "HAKU_CONSOLE_TEST_AGENT_TOKEN"
_AGENT_OPERATOR_ENV = "HAKU_CONSOLE_TEST_AGENT_OPERATOR"
_STATIC_AGENTS = [
    {
        "agent_id": "30000000-0000-4000-8000-000000000001",
        "display_name": "Haku",
        "token_env_var": _AGENT_TOKEN_ENV,
        "operator_subject_env": _AGENT_OPERATOR_ENV,
    }
]


@pytest.fixture(autouse=True)
def _static_agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_AGENT_TOKEN_ENV, _AGENT_TOKEN)
    monkeypatch.setenv(_AGENT_OPERATOR_ENV, "op-haku")


@pytest.fixture
def mcp_server_url(monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    monkeypatch.setenv("HAKU_CONSOLE_MCP_CREDENTIAL_HAKU_CONSOLE_GROCY_SF_TOKEN", "test-token")
    with serve_fastmcp(_test_mcp_server()) as url:
        yield url


def _enum_values(engine: Engine) -> dict[str, tuple[str, ...]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT type.typname, enum.enumlabel
                FROM pg_type AS type
                JOIN pg_enum AS enum ON enum.enumtypid = type.oid
                ORDER BY type.typname, enum.enumsortorder
                """
            )
        ).all()
    return {
        type_name: tuple(label for row_type_name, label in rows if row_type_name == type_name)
        for type_name in {row_type_name for row_type_name, _ in rows}
    }


def _config(servers: list[dict[str, Any]]) -> dict[str, Any]:
    """A console config dict for the given MCP servers, always carrying the `haku` static agent — a
    console with no /mcp credential doesn't run (create_app raises), and the deploy always has it. So
    the test app exercises the same required static MCP credential as the deployment."""
    return {"mcp": {"servers": servers}, "static_agents": _STATIC_AGENTS}


def _config_file(tmp_path: Path, mcp_server_url: str) -> Path:
    servers = [
        {"id": "grocy-sf", "server_url": mcp_server_url, "bearer_token_secret": "haku-console-grocy-sf-token"},
        {"id": "smoke", "server_url": mcp_server_url},
    ]
    return write_config(tmp_path / "haku_console.yaml", _config(servers))


@pytest.fixture
def console_config(tmp_path: Path, mcp_server_url: str) -> Path:
    """The standard two-server console config (`grocy-sf` static-bearer + `smoke`) most tests use."""
    return _config_file(tmp_path, mcp_server_url)


@pytest.fixture
def operator_client(make_operator_client: Callable[..., Any], console_config: Path) -> Generator[TestClient]:
    """An operator-session client against the standard `console_config`, CSRF configured — the setup
    the majority of operator-facing tests need. Tests with a bespoke config call `make_operator_client`
    (or `make_client`) directly instead."""
    with make_operator_client(config_file=console_config) as client:
        yield client


@pytest.fixture
def operator_oauth_config_file(tmp_path: Path, remote_oauth_url: str) -> Path:
    servers = [{"id": "grocy-sf", "server_url": f"{remote_oauth_url}/mcp", "operator_oauth": {}}]
    return write_config(tmp_path / "haku_console_operator_oauth.yaml", _config(servers))


@pytest.fixture
def gmail_config_file(tmp_path: Path) -> Path:
    return write_config(tmp_path / "haku_console_gmail.yaml", _config([{"id": "gmail"}]))


@pytest.fixture
def preregistered_operator_oauth_config_file(tmp_path: Path, preregistered_remote_oauth_url: str) -> Path:
    servers = [
        {
            "id": "grocy-sf",
            "server_url": f"{preregistered_remote_oauth_url}/mcp",
            "operator_oauth": {"static_client_id": "preregistered-client"},
        }
    ]
    return write_config(tmp_path / "haku_console_operator_oauth_static.yaml", _config(servers))


def _submit(client: TestClient, *, amount: int = 1) -> dict[str, Any]:
    """Submit directly at the application boundary; agent admission is tested through `/mcp`."""
    app = cast(FastAPI, client.app)

    async def submit() -> Any:
        return await app.state.tool_call_service.submit_and_wait(
            req=SubmitToolCallRequest(
                server_id="grocy-sf",
                tool_name="stock_add",
                title="Add Thrive box items to Grocy",
                rationale="box is physically present",
                arguments={"items": [{"product_id": 123, "amount": amount}]},
                wait_for_ms=0,
            ),
            actor=app.state.test_operator_actor,
        )

    assert client.portal is not None
    record = client.portal.call(submit)
    return cast(dict[str, Any], record.model_dump(mode="json"))


def _submit_request(
    client: TestClient, request: SubmitToolCallRequest, *, actor: ToolCallActor | None = None
) -> dict[str, Any]:
    app = cast(FastAPI, client.app)

    async def submit() -> Any:
        return await app.state.tool_call_service.submit_and_wait(
            req=request, actor=actor if actor is not None else app.state.test_operator_actor
        )

    assert client.portal is not None
    record = client.portal.call(submit)
    return cast(dict[str, Any], record.model_dump(mode="json"))


def _static_agent_actor(client: TestClient, bearer: str) -> AgentActor:
    app = cast(FastAPI, client.app)
    engine = create_engine(app.state.settings.database_url.get_secret_value())
    try:
        with Session(engine) as session:
            binding_id, agent_id, operator_id = session.execute(
                select(CredentialBinding.binding_id, CredentialBinding.agent_id, Agent.owner_operator_id)
                .join(StaticCredential, StaticCredential.binding_id == CredentialBinding.binding_id)
                .join(Agent, Agent.agent_id == CredentialBinding.agent_id)
                .where(StaticCredential.credential_fingerprint == fingerprint_static_token(bearer))
            ).one()
        return AgentActor(agent_id=agent_id, operator_id=operator_id, binding_id=binding_id)
    finally:
        engine.dispose()


class RecordingExecutor:
    def __init__(self) -> None:
        self.auth_tokens: list[str | None] = []

    async def execute(
        self, server: Any, tool_name: str, arguments: dict[str, Any], auth_token: str | None
    ) -> dict[str, Any]:
        self.auth_tokens.append(auth_token)
        return {"content": [{"type": "text", "text": f"{server.id}:{tool_name}"}], "isError": False}


def _record_execution_operator_ids(monkeypatch: pytest.MonkeyPatch) -> list[UUID]:
    operator_ids: list[UUID] = []

    async def recording_execution_auth(
        server: McpServerEntry,
        operator_id: UUID,
        oauth_store: PostgresMcpOperatorOAuthStore,
        provider_store: Any = None,
        authentik_store: Any = None,
    ) -> str | None:
        operator_ids.append(operator_id)
        return await _execution_auth(server, operator_id, oauth_store, provider_store, authentik_store)

    async def recording_service_auth(
        *,
        server: McpServerEntry,
        operator_id: UUID,
        oauth_store: PostgresMcpOperatorOAuthStore,
        provider_store: Any = None,
        authentik_store: Any = None,
    ) -> str | None:
        operator_ids.append(operator_id)
        return await backend_auth_for_operator(
            server=server,
            operator_id=operator_id,
            oauth_store=oauth_store,
            provider_store=provider_store,
            authentik_store=authentik_store,
        )

    monkeypatch.setattr("haku.console.mcp_approval._execution_auth", recording_execution_auth)
    monkeypatch.setattr("haku.console.tool_call_service.backend_auth_for_operator", recording_service_auth)
    return operator_ids


class RecordingMetadataProvider:
    def __init__(self) -> None:
        self.auth_tokens: list[str | None] = []

    async def metadata(self, server: Any, auth_token: str | None) -> AliveServerMetadata:
        self.auth_tokens.append(auth_token)
        return AliveServerMetadata(server_id=server.id, title=server.id)


class EchoingExecutor:
    async def execute(
        self, server: Any, tool_name: str, arguments: dict[str, Any], auth_token: str | None
    ) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": f"{server.id}:{tool_name}"}], "isError": False}


@pytest.fixture
def gmail_client() -> Mock:
    return Mock()


@pytest.fixture
def echoing_executor() -> EchoingExecutor:
    return EchoingExecutor()


@pytest.fixture
def recording_executor() -> RecordingExecutor:
    return RecordingExecutor()


def test_reflection_lists_connected_servers_without_leaking_credentials(
    make_operator_client, console_config: Path
) -> None:
    with make_operator_client(config_file=console_config) as client:
        resp = client.get("/api/capabilities/mcp-servers")
    assert resp.status_code == 200
    body = resp.json()
    server = body["servers"][0]
    assert server["server_id"] == "grocy-sf"
    assert server["status"] == "alive"
    tools = {tool["name"]: tool for tool in server["tools"]}
    assert set(tools) == {
        "echo",
        "stock_add",
        "products_list",
        "locations_list",
        "quantity_units_list",
        "product_groups_list",
        "shopping_lists_list",
        "shopping_list_get",
    }
    assert "status" not in tools["stock_add"]
    assert tools["stock_add"]["input_schema"]["type"] == "object"
    assert "status" not in tools["echo"]
    assert tools["echo"]["input_schema"]["type"] == "object"
    assert "bearer_token_secret" not in str(body)


def test_operator_oauth_association_drives_tool_reflection(
    make_operator_client, operator_oauth_config_file: Path
) -> None:
    metadata_provider = RecordingMetadataProvider()
    with (
        make_operator_client(
            config_file=operator_oauth_config_file, tool_call_metadata_provider=metadata_provider
        ) as client,
        client.websocket_connect("/api/events/ws", headers={"Origin": "https://haku.test"}) as events,
    ):
        assert events.receive_json() == {"event_type": "hello"}
        before = client.get("/api/capabilities/mcp-servers").json()
        started = client.post("/api/mcp/operator-auth/grocy-sf/connect", headers={"X-CSRF-Token": csrf_token(client)})
        state = parse_qs(urlparse(started.json()["authorization_url"]).query)["state"][0]
        callback = client.get("/api/mcp/operator-auth/callback", params={"state": state, "code": "operator-code"})
        association_event = events.receive_json()
        after = client.get("/api/capabilities/mcp-servers").json()
        disconnected = client.delete("/api/mcp/operator-auth/grocy-sf", headers={"X-CSRF-Token": csrf_token(client)})
        disassociation_event = events.receive_json()

    assert before["servers"][0]["status"] == "degraded"
    assert "Connect your grocy-sf MCP account" in before["servers"][0]["degraded_reason"]
    assert callback.status_code == 200, callback.text
    assert "BroadcastChannel" not in callback.text
    assert association_event == {
        "event_type": "mcp_operator_auth_changed",
        "server_id": "grocy-sf",
        "status": "connected",
    }
    assert disconnected.status_code == 200, disconnected.text
    assert disassociation_event == {
        "event_type": "mcp_operator_auth_changed",
        "server_id": "grocy-sf",
        "status": "disconnected",
    }
    assert after["servers"][0]["status"] == "alive"
    assert metadata_provider.auth_tokens == ["operator-access-token"]
    assert "bearer_token_secret" not in str(after)


def test_operator_oauth_static_client_id_skips_dynamic_registration(
    make_operator_client, preregistered_operator_oauth_config_file: Path
) -> None:
    """Mirrors kubectl-passthrough-mcp: fronted by Authentik, which has no DCR endpoint —
    dynamic registration would 401, so a configured static_client_id must skip it entirely.
    """
    with make_operator_client(config_file=preregistered_operator_oauth_config_file) as client:
        started = client.post("/api/mcp/operator-auth/grocy-sf/connect", headers={"X-CSRF-Token": csrf_token(client)})
        assert started.status_code == 200, started.text
        auth_query = parse_qs(urlparse(started.json()["authorization_url"]).query)
        assert auth_query["client_id"] == ["preregistered-client"]

        callback = client.get(
            "/api/mcp/operator-auth/callback", params={"state": auth_query["state"][0], "code": "operator-code"}
        )
        after = client.get("/api/mcp/operator-auth").json()

    assert callback.status_code == 200, callback.text
    assert after["associations"][0]["status"] == "connected"


def test_operator_oauth_callback_is_bound_to_flow_operator(
    make_operator_client, operator_oauth_config_file: Path
) -> None:
    with (
        make_operator_client(
            config_file=operator_oauth_config_file,
            operator_external_user_key="operator-a",
            operator_username="a@example.com",
        ) as operator_a,
        make_operator_client(
            config_file=operator_oauth_config_file,
            operator_external_user_key="operator-b",
            operator_username="b@example.com",
        ) as operator_b,
    ):
        started = operator_a.post(
            "/api/mcp/operator-auth/grocy-sf/connect", headers={"X-CSRF-Token": csrf_token(operator_a)}
        )
        state = parse_qs(urlparse(started.json()["authorization_url"]).query)["state"][0]

        wrong_operator = operator_b.get(
            "/api/mcp/operator-auth/callback", params={"state": state, "code": "operator-code"}
        )
        completed = operator_a.get("/api/mcp/operator-auth/callback", params={"state": state, "code": "operator-code"})
        a_status = operator_a.get("/api/mcp/operator-auth").json()["associations"][0]
        b_status = operator_b.get("/api/mcp/operator-auth").json()["associations"][0]

    assert wrong_operator.status_code == 403
    assert "different operator" in wrong_operator.text
    # A mismatched session does not consume the flow: its owner can still complete it.
    assert completed.status_code == 200, completed.text
    assert a_status["status"] == "connected"
    assert b_status["status"] == "unconnected"


def test_reflection_marks_unreachable_servers_degraded(
    make_operator_client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HAKU_CONSOLE_MCP_CREDENTIAL_HAKU_CONSOLE_GROCY_SF_TOKEN", "test-token")
    with make_operator_client(config_file=_config_file(tmp_path, "http://127.0.0.1:1/mcp")) as client:
        resp = client.get("/api/capabilities/mcp-servers")
    assert resp.status_code == 200
    server = resp.json()["servers"][0]
    assert server["server_id"] == "grocy-sf"
    assert server["status"] == "degraded"
    assert server["tools"] == []
    assert server["degraded_reason"]


def test_mcp_result_serialization_uses_mcp_wire_shape() -> None:
    result = mcp_types.CallToolResult(
        content=[
            mcp_types.TextContent(type="text", text="ok"),
            mcp_types.ImageContent(type="image", mimeType="image/png", data="ZmFrZQ=="),
        ],
        structuredContent={"changed": True},
        isError=False,
    )

    assert _mcp_result_to_json(result) == {
        "content": [{"type": "text", "text": "ok"}, {"type": "image", "data": "ZmFrZQ==", "mimeType": "image/png"}],
        "structuredContent": {"changed": True},
        "isError": False,
    }


def test_submit_mints_tool_call_id(operator_client: TestClient, migrated_db_url: str) -> None:
    first = _submit(operator_client)
    second = _submit(operator_client)
    assert first["tool_call_id"].startswith("tc_")
    assert first["caller"] == {"kind": "operator"}
    assert first["status"] == "pending_approval"
    assert "approval_id" not in first
    assert second["tool_call_id"] != first["tool_call_id"]


def test_rest_submission_route_is_retired(operator_client: TestClient) -> None:
    response = operator_client.post(
        "/api/tool-calls",
        headers={"Authorization": "Bearer tool-token"},
        json={"server_id": "smoke", "tool_name": "echo", "arguments": {}, "wait_for_ms": 0},
    )
    assert response.status_code == 405


def test_haku_gmail_labels_list_auto_approves_executes_and_records_policy(
    make_client, make_operator_client, gmail_config_file: Path, gmail_client: Mock, echoing_executor: EchoingExecutor
) -> None:
    with (
        make_client(
            config_file=gmail_config_file,
            gmail_client=gmail_client,
            in_process_servers={"gmail": const_in_process_server(build_gmail_mcp(gmail_client))},
            tool_call_executor=echoing_executor,
        ) as client,
        make_operator_client(config_file=gmail_config_file, operator_external_user_key="op-haku") as operator,
    ):
        record = _submit_request(
            client,
            SubmitToolCallRequest(server_id="gmail", tool_name="labels_list", arguments={}, wait_for_ms=0),
            actor=_static_agent_actor(client, "tool-token"),
        )
        pending = operator.get("/api/approvals/pending").json()

    assert record["status"] == "ok"
    assert record["approval_policy_id"] == "unconditional_v1"
    assert record["auto_approval_evaluation"] == "approved: gmail/labels_list is allowlisted read-only/safe"
    assert record["approved_at"] is not None
    assert record["result"]["content"][0]["text"] == "gmail:labels_list"
    assert pending["approvals"] == []


def test_operator_gmail_labels_list_stays_pending(
    make_operator_client, gmail_config_file: Path, gmail_client: Mock, echoing_executor: EchoingExecutor
) -> None:
    with make_operator_client(
        config_file=gmail_config_file,
        gmail_client=gmail_client,
        tool_call_executor=echoing_executor,
        # Matching a configured agent id must not turn an operator into an auto-approved agent.
        operator_username="haku",
    ) as client:
        record = _submit_request(
            client, SubmitToolCallRequest(server_id="gmail", tool_name="labels_list", arguments={}, wait_for_ms=0)
        )

    assert record["status"] == "pending_approval"
    assert record["approval_policy_id"] is None
    assert record["auto_approval_evaluation"] is None


def test_haku_gmail_nonmatching_policy_evaluation_is_recorded(
    make_client, make_operator_client, gmail_config_file: Path, gmail_client: Mock
) -> None:
    with (
        make_client(
            config_file=gmail_config_file,
            gmail_client=gmail_client,
            in_process_servers={"gmail": const_in_process_server(build_gmail_mcp(gmail_client))},
        ) as client,
        make_operator_client(config_file=gmail_config_file, operator_external_user_key="op-haku") as operator,
    ):
        record = _submit_request(
            client,
            SubmitToolCallRequest(
                server_id="gmail",
                tool_name="threads_modify_labels",
                arguments={"thread_ids": ["t1"], "add": ["INBOX"]},
                wait_for_ms=0,
            ),
            actor=_static_agent_actor(client, "tool-token"),
        )
        pending = operator.get("/api/approvals/pending").json()["approvals"]

    assert record["status"] == "pending_approval"
    assert record["approval_policy_id"] is None
    assert record["auto_approval_evaluation"] == "manual: at least one label name is outside 'haku/'"
    assert pending[0]["auto_approval_evaluation"] == record["auto_approval_evaluation"]


def test_approval_executes_tool_and_records_terminal_result(operator_client: TestClient) -> None:
    submitted = _submit(operator_client)
    resp = operator_client.post(
        f"/api/tool-calls/{submitted['tool_call_id']}/decision",
        headers={"X-CSRF-Token": csrf_token(operator_client)},
        json={"decision": "approve"},
    )
    fetched = operator_client.get(f"/api/tool-calls/{submitted['tool_call_id']}").json()
    assert resp.status_code == 200, resp.text
    decided = resp.json()["tool_call"]
    assert decided["status"] == "ok"
    assert decided["approved_at"] is not None
    assert decided["approval_policy_id"] is None
    assert decided["result"]["content"][0]["text"] == "stock_add:123:1"
    assert fetched == decided


def test_configured_credential_approval_passes_canonical_operator_id(
    make_operator_client,
    console_config: Path,
    migrated_db_url: str,
    recording_executor: RecordingExecutor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_operator_ids = _record_execution_operator_ids(monkeypatch)
    with make_operator_client(
        config_file=console_config,
        operator_external_user_key="configured-credential-sub",
        tool_call_executor=recording_executor,
    ) as client:
        submitted = _submit(client)
        approved = client.post(
            f"/api/tool-calls/{submitted['tool_call_id']}/decision",
            headers={"X-CSRF-Token": csrf_token(client)},
            json={"decision": "approve"},
        )

    assert approved.status_code == 200, approved.text
    assert execution_operator_ids == [operator_id(migrated_db_url, "configured-credential-sub")]
    assert recording_executor.auth_tokens == ["test-token"]


def test_operator_oauth_association_drives_approved_tool_execution(
    make_operator_client,
    operator_oauth_config_file: Path,
    recording_executor: RecordingExecutor,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_operator_ids = _record_execution_operator_ids(monkeypatch)
    with make_operator_client(
        config_file=operator_oauth_config_file,
        operator_external_user_key="operator-oauth-sub",
        tool_call_executor=recording_executor,
    ) as client:
        before = client.get("/api/mcp/operator-auth").json()
        assert before["associations"] == [
            {"server_id": "grocy-sf", "status": "unconnected", "username": "operator@example.com"}
        ]

        started = client.post("/api/mcp/operator-auth/grocy-sf/connect", headers={"X-CSRF-Token": csrf_token(client)})
        assert started.status_code == 200, started.text
        authorization_url = started.json()["authorization_url"]
        parsed_authorization = urlparse(authorization_url)
        auth_query = parse_qs(parsed_authorization.query)
        assert parsed_authorization.path == "/auth/authorize"
        assert auth_query["client_id"] == ["dynamic-client"]
        assert auth_query["redirect_uri"] == ["https://haku.test/api/mcp/operator-auth/callback"]
        assert auth_query["code_challenge_method"] == ["S256"]

        callback = client.get(
            "/api/mcp/operator-auth/callback", params={"state": auth_query["state"][0], "code": "operator-code"}
        )
        assert callback.status_code == 200, callback.text
        after = client.get("/api/mcp/operator-auth").json()
        assert after["associations"][0]["status"] == "connected"
        assert after["associations"][0]["connected_at"]

        reconnect = client.post("/api/mcp/operator-auth/grocy-sf/connect", headers={"X-CSRF-Token": csrf_token(client)})
        removed_start = client.post(
            "/api/mcp/operator-auth/grocy-sf/start", headers={"X-CSRF-Token": csrf_token(client)}
        )
        assert reconnect.status_code == 409
        assert reconnect.json()["detail"] == "MCP server grocy-sf is already connected; disconnect it first"
        assert removed_start.status_code == 404

        submitted = _submit(client)
        approved = client.post(
            f"/api/tool-calls/{submitted['tool_call_id']}/decision",
            headers={"X-CSRF-Token": csrf_token(client)},
            json={"decision": "approve"},
        )

    assert approved.status_code == 200, approved.text
    assert approved.json()["tool_call"]["status"] == "ok"
    assert execution_operator_ids == [operator_id(migrated_db_url, "operator-oauth-sub")]
    assert recording_executor.auth_tokens == ["operator-access-token"]


def test_operator_oauth_approval_requires_existing_association(
    make_operator_client, operator_oauth_config_file: Path, recording_executor: RecordingExecutor
) -> None:
    with make_operator_client(config_file=operator_oauth_config_file, tool_call_executor=recording_executor) as client:
        submitted = _submit(client)
        resp = client.post(
            f"/api/tool-calls/{submitted['tool_call_id']}/decision",
            headers={"X-CSRF-Token": csrf_token(client)},
            json={"decision": "approve"},
        )
        fetched = client.get(f"/api/tool-calls/{submitted['tool_call_id']}").json()

    assert resp.status_code == 409
    assert "Connect your grocy-sf MCP account" in resp.json()["detail"]
    assert fetched["status"] == "pending_approval"
    assert recording_executor.auth_tokens == []


def _seed_association(db_url: str, *, operator_external_user_key: str, access_token: str) -> None:
    """Insert a connected operator_oauth association for grocy-sf (bypassing the DCR/PKCE flow)."""
    engine = create_engine(db_url)
    now = datetime.datetime.now(datetime.UTC)
    try:
        with sessionmaker(engine)() as session, session.begin():
            session.add(
                McpOperatorOAuthAssociation(
                    server_id="grocy-sf",
                    operator_id=operator_id(db_url, operator_external_user_key),
                    created_at=now,
                    updated_at=now,
                    client_id="test-client",
                    token_endpoint="http://unused.test/token",
                    access_token=access_token,
                    token_type="Bearer",
                    token_expires_at=now + datetime.timedelta(hours=1),
                )
            )
    finally:
        engine.dispose()


def test_routing_executes_each_agent_as_its_own_operator(
    make_client,
    tmp_path: Path,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    recording_executor: RecordingExecutor,
) -> None:
    """Two static agents bound to two operators: each agent's auto-approved operator_oauth call
    executes with *its* operator's token, with no crosstalk."""
    # `haku` (bearer tool-token → op-haku) comes from the autouse env; add a second agent `ops-bot`.
    monkeypatch.setenv("HAKU_CONSOLE_TEST_AGENT2_TOKEN", "ops-token")
    monkeypatch.setenv("HAKU_CONSOLE_TEST_AGENT2_OPERATOR", "op-ops")
    _seed_association(migrated_db_url, operator_external_user_key="op-haku", access_token="grocy-token-haku")
    _seed_association(migrated_db_url, operator_external_user_key="op-ops", access_token="grocy-token-ops")

    config = _config([{"id": "grocy-sf", "server_url": "http://unused.test/mcp", "operator_oauth": {}}])
    config["static_agents"] = [
        *_STATIC_AGENTS,
        {
            "agent_id": "30000000-0000-4000-8000-000000000002",
            "display_name": "Ops Bot",
            "token_env_var": "HAKU_CONSOLE_TEST_AGENT2_TOKEN",
            "operator_subject_env": "HAKU_CONSOLE_TEST_AGENT2_OPERATOR",
        },
    ]
    with make_client(
        config_file=write_config(tmp_path / "routing.yaml", config), tool_call_executor=recording_executor
    ) as client:
        # products_list is an unconditionally auto-approved grocy read, so each call runs immediately.
        call_ids: list[str] = []
        for bearer in ("tool-token", "ops-token"):
            record = _submit_request(
                client,
                SubmitToolCallRequest(server_id="grocy-sf", tool_name="products_list", arguments={}, wait_for_ms=0),
                actor=_static_agent_actor(client, bearer),
            )
            assert record["status"] == "ok", record
            call_ids.append(record["tool_call_id"])

        for bearer, expected_call_id in zip(("tool-token", "ops-token"), call_ids, strict=True):
            actor = _static_agent_actor(client, bearer)
            listed = client.app.state.tool_call_service.list_tool_calls(actor=actor)
            assert [call.tool_call_id for call in listed] == [expected_call_id]
            assert client.get("/api/tool-calls", headers={"Authorization": f"Bearer {bearer}"}).status_code == 401

    # haku's call executed with op-haku's token; ops-bot's with op-ops's — each routed to its operator.
    assert recording_executor.auth_tokens == ["grocy-token-haku", "grocy-token-ops"]


def test_two_operator_two_agent_http_authorization_matrix(
    make_client, make_operator_client, tmp_path: Path, mcp_server_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_specs = (
        ("haku", "tool-token", "op-haku", _AGENT_TOKEN_ENV, _AGENT_OPERATOR_ENV),
        (
            "haku-sibling",
            "haku-sibling-token",
            "op-haku",
            "HAKU_CONSOLE_TEST_HAKU_SIBLING_TOKEN",
            "HAKU_CONSOLE_TEST_HAKU_SIBLING_OPERATOR",
        ),
        ("ops", "ops-token", "op-ops", "HAKU_CONSOLE_TEST_OPS_TOKEN", "HAKU_CONSOLE_TEST_OPS_OPERATOR"),
        (
            "ops-sibling",
            "ops-sibling-token",
            "op-ops",
            "HAKU_CONSOLE_TEST_OPS_SIBLING_TOKEN",
            "HAKU_CONSOLE_TEST_OPS_SIBLING_OPERATOR",
        ),
    )
    for _, token, operator_key, token_env, operator_env in agent_specs:
        monkeypatch.setenv(token_env, token)
        monkeypatch.setenv(operator_env, operator_key)
    config = _config(
        [{"id": "grocy-sf", "server_url": mcp_server_url, "bearer_token_secret": "haku-console-grocy-sf-token"}]
    )
    config["static_agents"] = [
        {
            "agent_id": f"30000000-0000-4000-8000-{index:012d}",
            "display_name": name.replace("-", " ").title(),
            "token_env_var": token_env,
            "operator_subject_env": operator_env,
        }
        for index, (name, _, _, token_env, operator_env) in enumerate(agent_specs, start=10)
    ]
    config_file = write_config(tmp_path / "two_operator_agents.yaml", config)

    with (
        make_client(config_file=config_file) as agents,
        make_operator_client(
            config_file=config_file, operator_external_user_key="op-haku", operator_username="owner@example.com"
        ) as operator_a,
        make_operator_client(
            config_file=config_file, operator_external_user_key="op-ops", operator_username="ops@example.com"
        ) as operator_b,
    ):
        call_ids: dict[str, str] = {}
        for amount, (name, bearer, _, _, _) in enumerate(agent_specs, start=1):
            record = _submit_request(
                agents,
                SubmitToolCallRequest(
                    server_id="grocy-sf",
                    tool_name="stock_add",
                    arguments={"items": [{"product_id": 123, "amount": amount}]},
                    wait_for_ms=0,
                ),
                actor=_static_agent_actor(agents, bearer),
            )
            call_ids[name] = record["tool_call_id"]

        call_ids["operator-a"] = _submit(operator_a, amount=5)["tool_call_id"]
        call_ids["operator-b"] = _submit(operator_b, amount=6)["tool_call_id"]

        for name, bearer, _, _, _ in agent_specs:
            headers = {"Authorization": f"Bearer {bearer}"}
            assert agents.get("/api/tool-calls", headers=headers).status_code == 401
            assert agents.get(f"/api/tool-calls/{call_ids[name]}", headers=headers).status_code == 401
            assert agents.get("/api/approvals/pending", headers=headers).status_code == 401
            assert (
                agents.post(
                    f"/api/tool-calls/{call_ids[name]}/decision", headers=headers, json={"decision": "deny"}
                ).status_code
                == 401
            )

        operator_expected = {
            "a": {call_ids["haku"], call_ids["haku-sibling"], call_ids["operator-a"]},
            "b": {call_ids["ops"], call_ids["ops-sibling"], call_ids["operator-b"]},
        }
        for operator, own_ids, foreign_ids in (
            (operator_a, operator_expected["a"], operator_expected["b"]),
            (operator_b, operator_expected["b"], operator_expected["a"]),
        ):
            listed_ids = {call["tool_call_id"] for call in operator.get("/api/tool-calls").json()["tool_calls"]}
            pending_ids = {call["tool_call_id"] for call in operator.get("/api/approvals/pending").json()["approvals"]}
            assert listed_ids == pending_ids == own_ids
            assert all(operator.get(f"/api/tool-calls/{call_id}").status_code == 200 for call_id in own_ids)
            assert all(operator.get(f"/api/tool-calls/{call_id}").status_code == 404 for call_id in foreign_ids)

        # Decision ownership is checked before OAuth lookup, transition, or execution.
        for operator, foreign_call_id in ((operator_a, call_ids["ops"]), (operator_b, call_ids["haku"])):
            response = operator.post(
                f"/api/tool-calls/{foreign_call_id}/decision",
                headers={"X-CSRF-Token": csrf_token(operator)},
                json={"decision": "approve"},
            )
            assert response.status_code == 404

        approved = operator_a.post(
            f"/api/tool-calls/{call_ids['haku']}/decision",
            headers={"X-CSRF-Token": csrf_token(operator_a)},
            json={"decision": "approve"},
        )
        denied = operator_b.post(
            f"/api/tool-calls/{call_ids['ops']}/decision",
            headers={"X-CSRF-Token": csrf_token(operator_b)},
            json={"decision": "deny", "reason": "no"},
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["tool_call"]["status"] == "ok"
        assert denied.status_code == 200, denied.text
        assert denied.json()["tool_call"]["status"] == "denied"


def test_approval_denial_is_terminal_and_does_not_execute(operator_client: TestClient) -> None:
    submitted = _submit(operator_client)
    resp = operator_client.post(
        f"/api/tool-calls/{submitted['tool_call_id']}/decision",
        headers={"X-CSRF-Token": csrf_token(operator_client)},
        json={"decision": "deny", "reason": "not today"},
    )
    assert resp.status_code == 200
    tool_call = resp.json()["tool_call"]
    assert tool_call["status"] == "denied"
    assert tool_call["result"] is None
    assert tool_call["denial_reason"] == "not today"


def test_all_v1_tool_calls_require_console_approval(operator_client: TestClient) -> None:
    body = _submit_request(
        operator_client,
        SubmitToolCallRequest(server_id="smoke", tool_name="echo", arguments={"text": "world"}, wait_for_ms=1000),
    )
    pending = operator_client.get("/api/approvals/pending").json()
    listed = operator_client.get(
        "/api/tool-calls", params={"status": "pending_approval", "since": "1970-01-01T00:00:00+00:00"}
    ).json()
    assert body["status"] == "pending_approval"
    assert body["result"] is None
    assert pending["approvals"] == [body]
    assert listed["tool_calls"][0]["tool_call_id"] == body["tool_call_id"]


def test_unknown_oauth_server_maps_to_http_not_found(operator_client: TestClient) -> None:
    connected = operator_client.post(
        "/api/mcp/operator-auth/missing/connect", headers={"X-CSRF-Token": csrf_token(operator_client)}
    )

    assert connected.status_code == 404
    assert connected.json()["detail"] == "unknown MCP server: missing"


def test_operator_tenants_cannot_read_or_decide_each_others_calls(make_operator_client, console_config: Path) -> None:
    with (
        make_operator_client(
            config_file=console_config, operator_external_user_key="operator-a", operator_username="a@example.com"
        ) as operator_a,
        make_operator_client(
            config_file=console_config, operator_external_user_key="operator-b", operator_username="b@example.com"
        ) as operator_b,
    ):
        submitted = _submit(operator_a)
        call_id = submitted["tool_call_id"]

        assert [row["tool_call_id"] for row in operator_a.get("/api/tool-calls").json()["tool_calls"]] == [call_id]
        assert operator_b.get("/api/tool-calls").json()["tool_calls"] == []
        assert operator_b.get(f"/api/tool-calls/{call_id}").status_code == 404
        assert operator_b.get("/api/approvals/pending").json()["approvals"] == []

        b_csrf = csrf_token(operator_b)
        for decision in ("deny", "approve"):
            response = operator_b.post(
                f"/api/tool-calls/{call_id}/decision", headers={"X-CSRF-Token": b_csrf}, json={"decision": decision}
            )
            assert response.status_code == 404

        approved = operator_a.post(
            f"/api/tool-calls/{call_id}/decision",
            headers={"X-CSRF-Token": csrf_token(operator_a)},
            json={"decision": "approve"},
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["tool_call"]["status"] == "ok"


def test_list_newest_first_keeps_the_most_recent(operator_client: TestClient) -> None:
    first = _submit(operator_client, amount=1)
    second = _submit(operator_client, amount=2)
    third = _submit(operator_client, amount=3)
    newest = operator_client.get("/api/tool-calls", params={"newest_first": "true"}).json()
    newest_two = operator_client.get("/api/tool-calls", params={"newest_first": "true", "limit": 2}).json()
    oldest = operator_client.get("/api/tool-calls").json()

    ids = [first["tool_call_id"], second["tool_call_id"], third["tool_call_id"]]
    assert [r["tool_call_id"] for r in newest["tool_calls"]] == list(reversed(ids))
    # `limit` under newest_first keeps the most recent calls, not the oldest.
    assert [r["tool_call_id"] for r in newest_two["tool_calls"]] == [third["tool_call_id"], second["tool_call_id"]]
    # The default (no newest_first) stays oldest-first for the pending-approval queue.
    assert [r["tool_call_id"] for r in oldest["tool_calls"]] == ids


def test_ledger_get_and_list_load_principal_projection_in_one_query(
    make_client, make_operator_client, console_config: Path, migrated_db_url: str
) -> None:
    with (
        make_client(config_file=console_config) as agent,
        make_operator_client(config_file=console_config, operator_external_user_key="op-haku") as operator,
    ):
        agent_record = _submit_request(
            agent,
            SubmitToolCallRequest(server_id="smoke", tool_name="echo", arguments={"text": "agent"}, wait_for_ms=0),
            actor=_static_agent_actor(agent, "tool-token"),
        )
        agent_call_id = cast(str, agent_record["tool_call_id"])
        operator_call_id = cast(str, _submit(operator)["tool_call_id"])

    ledger_engine = create_engine(migrated_db_url, pool_pre_ping=True)
    ledger = PostgresToolCallLedger(sessionmaker(ledger_engine, expire_on_commit=False))
    actor = OperatorActor(operator_id=operator_id(migrated_db_url, "op-haku"))
    statements: list[str] = []

    def record_tool_call_query(
        _connection: object, _cursor: object, statement: str, _parameters: object, _context: object, _executemany: bool
    ) -> None:
        if "mcp_tool_call" in statement.casefold():
            statements.append(statement)

    event.listen(ledger_engine, "before_cursor_execute", record_tool_call_query)
    try:
        listed = ledger.list_tool_calls(actor=actor)
        assert len(statements) == 1, statements

        by_id = {record.tool_call_id: record for record in listed}
        assert set(by_id) == {agent_call_id, operator_call_id}
        assert by_id[agent_call_id].caller == AgentToolCallCaller(
            agent_id=UUID("30000000-0000-4000-8000-000000000001"), display_name="Haku"
        )
        assert by_id[operator_call_id].caller == OperatorToolCallCaller()

        statements.clear()
        fetched = ledger.get(agent_call_id, actor=actor)
        assert len(statements) == 1, statements
        assert fetched == by_id[agent_call_id]
    finally:
        event.remove(ledger_engine, "before_cursor_execute", record_tool_call_query)
        ledger_engine.dispose()


def test_websocket_receives_pending_approval_invalidation(operator_client: TestClient) -> None:
    with operator_client.websocket_connect("/api/events/ws", headers={"Origin": "https://haku.test"}) as ws:
        assert ws.receive_json() == {"event_type": "hello"}
        submitted = _submit(operator_client)
        event = ws.receive_json()
    assert event == {"event_type": "tool_calls_changed", "tool_call_id": submitted["tool_call_id"]}
    assert operator_client.get("/api/approvals/events").status_code == 404


def test_two_operator_websockets_only_receive_their_interleaved_tool_calls(
    make_operator_client, console_config: Path
) -> None:
    with (
        make_operator_client(
            config_file=console_config,
            operator_external_user_key="websocket-operator-a",
            operator_username="a@example.com",
        ) as operator_a,
        make_operator_client(
            config_file=console_config,
            operator_external_user_key="websocket-operator-b",
            operator_username="b@example.com",
        ) as operator_b,
        operator_a.websocket_connect("/api/events/ws", headers={"Origin": "https://haku.test"}) as events_a,
        operator_b.websocket_connect("/api/events/ws", headers={"Origin": "https://haku.test"}) as events_b,
    ):
        assert events_a.receive_json() == {"event_type": "hello"}
        assert events_b.receive_json() == {"event_type": "hello"}
        submitted = [
            ("a", _submit(operator_a, amount=1)["tool_call_id"]),
            ("b", _submit(operator_b, amount=2)["tool_call_id"]),
            ("a", _submit(operator_a, amount=3)["tool_call_id"]),
            ("b", _submit(operator_b, amount=4)["tool_call_id"]),
        ]
        # Each submit publishes one Operator-routed invalidation. Each socket must see only its own
        # two call ids even when the durable writes interleave tenants.
        received_a = [events_a.receive_json() for _ in range(2)]
        received_b = [events_b.receive_json() for _ in range(2)]

    expected_a = {call_id for owner, call_id in submitted if owner == "a"}
    expected_b = {call_id for owner, call_id in submitted if owner == "b"}
    assert {event["tool_call_id"] for event in received_a} == expected_a
    assert {event["tool_call_id"] for event in received_b} == expected_b
    assert expected_a.isdisjoint({event["tool_call_id"] for event in received_b})
    assert expected_b.isdisjoint({event["tool_call_id"] for event in received_a})


def test_websocket_rejects_cross_origin(make_operator_client) -> None:
    with (
        make_operator_client() as client,
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect("/api/events/ws", headers={"Origin": "https://haku-ui.test"}),
    ):
        pass
    assert exc_info.value.code == 1008


def test_audit_log_is_tenant_scoped_and_redacts_secrets(
    make_client, make_operator_client, console_config: Path
) -> None:
    with (
        make_client(config_file=console_config) as agent,
        make_operator_client(config_file=console_config, operator_external_user_key="operator-sub") as operator,
        make_operator_client(config_file=console_config, operator_external_user_key="op-haku") as haku_operator,
    ):
        operator_call = _submit_request(
            operator,
            SubmitToolCallRequest(server_id="smoke", tool_name="echo", arguments={"text": "one"}, wait_for_ms=0),
        )
        haku_call = _submit_request(
            agent,
            SubmitToolCallRequest(server_id="smoke", tool_name="echo", arguments={"text": "two"}, wait_for_ms=0),
            actor=_static_agent_actor(agent, "tool-token"),
        )
        operator_body = operator.get("/api/tool-calls").json()
        haku_body = haku_operator.get("/api/tool-calls").json()
        future = operator.get("/api/tool-calls", params={"since": "2999-01-01T00:00:00+00:00"}).json()

    assert [row["tool_call_id"] for row in operator_body["tool_calls"]] == [operator_call["tool_call_id"]]
    assert [row["tool_call_id"] for row in haku_body["tool_calls"]] == [haku_call["tool_call_id"]]
    assert future["tool_calls"] == []
    dumped = str([operator_body, haku_body])
    assert "haku-console-grocy-sf-token" not in dumped
    assert "tool-token" not in dumped


def test_postgres_store_runs_alembic_and_persists_typed_ledger(operator_client: TestClient, db_url: str) -> None:
    submitted = _submit_request(
        operator_client,
        SubmitToolCallRequest(server_id="smoke", tool_name="echo", arguments={"text": "world"}, wait_for_ms=0),
    )
    approved = operator_client.post(
        f"/api/tool-calls/{submitted['tool_call_id']}/decision",
        headers={"X-CSRF-Token": csrf_token(operator_client)},
        json={"decision": "approve"},
    ).json()["tool_call"]

    assert approved["status"] == "ok"
    assert approved["result"]["content"][0]["text"] == "echo:world"

    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            tables = {
                row["table_name"]
                for row in conn.execute(
                    text(
                        """
                        SELECT table_name
                        FROM information_schema.tables
                        WHERE table_schema = 'public'
                        """
                    )
                )
                .mappings()
                .all()
            }
            columns = {
                row["column_name"]
                for row in conn.execute(
                    text(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_name = 'mcp_tool_calls'
                        """
                    )
                )
                .mappings()
                .all()
            }
            principal_columns = {
                row["column_name"]
                for row in conn.execute(
                    text(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_name = 'mcp_tool_call_principals'
                        """
                    )
                )
                .mappings()
                .all()
            }
        with sessionmaker(engine)() as session:
            persisted_call = session.get(McpToolCall, submitted["tool_call_id"])
            persisted_principal = session.get(McpToolCallPrincipal, submitted["tool_call_id"])
            assert persisted_call is not None
            assert persisted_principal is not None
            assert persisted_principal.operator_id == operator_id(db_url, "operator-sub")
            assert persisted_call.server_id == "smoke"
            assert persisted_call.tool_name == "echo"
            assert persisted_call.status is ToolCallStatus.OK
            assert persisted_call.arguments_json == {"text": "world"}
            assert persisted_call.result_json is not None
            assert persisted_call.result_json["content"][0]["text"] == "echo:world"
    finally:
        engine.dispose()

    assert {
        "operators",
        "identity_anchors",
        "oidc_identities",
        "client_software",
        "enrollment_interactions",
        "enrollment_correlation_reservations",
        "agents",
        "agent_name_reservations",
        "credential_bindings",
        "authorization_grants",
        "static_credentials",
        "mcp_tool_call_principals",
        "mcp_operator_oauth_associations",
        "mcp_operator_oauth_flows",
        "provider_connections",
        "provider_connection_flows",
    } <= tables
    assert {
        "mcp_agent_operator",
        "mcp_tool_calls_legacy_unowned",
        "mcp_tool_call_events",
        "mcp_tool_call_events_legacy_unowned",
    }.isdisjoint(tables)
    assert columns == {column.name for column in McpToolCall.__table__.columns}
    assert principal_columns == {column.name for column in McpToolCallPrincipal.__table__.columns}


def test_fresh_baseline_enum_values_match_domain_enums(db_url: str) -> None:
    apply_migrations(db_url)
    engine = create_engine(db_url)
    try:
        baseline_values = _enum_values(engine)
    finally:
        engine.dispose()

    current_values = {
        "agent_status": tuple(status.value for status in AgentStatus),
        "client_registration_kind": tuple(kind.value for kind in ClientRegistrationKind),
        "credential_binding_status": tuple(status.value for status in CredentialBindingStatus),
        "credential_kind": tuple(kind.value for kind in CredentialKind),
        "enrollment_phase": tuple(phase.value for phase in EnrollmentPhase),
        "operator_status": tuple(status.value for status in OperatorStatus),
        "tool_call_status": tuple(status.value for status in ToolCallStatus),
    }
    assert baseline_values == current_values


# --- In-process MCP servers (McpToolExecutor/McpMetadataProvider in-process registration) ---
# Unit tests only: no postgres/network fixtures, exercising McpToolExecutor/McpMetadataProvider
# directly (over the module-level `_test_mcp_server()` FastMCP fixture, in-memory — no HTTP)
# rather than through the FastAPI app.


def test_server_entry_allows_missing_server_url() -> None:
    McpServerEntry(id="google")  # ok: resolved via the in-process registry at runtime, not this model


async def test_executor_dispatches_to_registered_in_process_server() -> None:
    executor = McpToolExecutor({"google": const_in_process_server(_test_mcp_server())})
    server = McpServerEntry(id="google")
    result = await executor.execute(server, "echo", {"text": "hi"}, auth_token=None)
    assert result["content"][0]["text"] == "echo:hi"


async def test_executor_raises_when_no_server_url_and_not_registered() -> None:
    executor = McpToolExecutor({})
    server = McpServerEntry(id="google")
    with pytest.raises(RuntimeError, match="no server_url and no in-process registration"):
        await executor.execute(server, "echo", {}, auth_token=None)


async def test_metadata_provider_reflects_in_process_server_tools() -> None:
    metadata_provider = McpMetadataProvider({"google": const_in_process_server(_test_mcp_server())})
    server = McpServerEntry(id="google")
    metadata = await metadata_provider.metadata(server, auth_token=None)
    assert isinstance(metadata, AliveServerMetadata)
    assert {tool.name for tool in metadata.tools} == {
        "stock_add",
        "echo",
        "products_list",
        "locations_list",
        "quantity_units_list",
        "product_groups_list",
        "shopping_lists_list",
        "shopping_list_get",
    }


async def test_metadata_provider_degrades_when_no_server_url_and_not_registered() -> None:
    metadata_provider = McpMetadataProvider({})
    server = McpServerEntry(id="google")
    metadata = await metadata_provider.metadata(server, auth_token=None)
    assert metadata.status == "degraded"


if __name__ == "__main__":
    pytest_bazel.main()
