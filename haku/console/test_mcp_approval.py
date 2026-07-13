"""Operator-approved MCP tool-call API tests."""

from __future__ import annotations

import datetime
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_bazel
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient
from fastmcp import FastMCP
from mcp import types as mcp_types
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import sessionmaker
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.websockets import WebSocketDisconnect

from haku.console.conftest import csrf_token, write_config
from haku.console.database_schema import McpOperatorOAuthAssociation, metadata
from haku.console.mcp_approval import AliveServerMetadata, McpMetadataProvider, McpToolExecutor, _mcp_result_to_json
from haku.console.mcp_config import McpServerEntry
from haku.console.tool_calls import ToolCallEventType, ToolCallStatus
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

    return server


@contextmanager
def _remote_oauth_url(*, static_client_id: str | None = None) -> Generator[str]:
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


# The Postgres testcontainer + per-test database fixtures (`db_url`, `migrated_db_url`, `make_client`)
# live in conftest.py. `make_client` wires the app to a fresh migrated database automatically, so
# tests only pass the overrides they exercise.

# A static agent `haku` (bearer `tool-token`, acting as operator subject `op-haku`), referenced from
# a config file's `static_agents` and resolved from these env vars — like the deploy.
_AGENT_TOKEN = "tool-token"
_AGENT_TOKEN_ENV = "HAKU_CONSOLE_TEST_AGENT_TOKEN"
_AGENT_OPERATOR_ENV = "HAKU_CONSOLE_TEST_AGENT_OPERATOR"
_STATIC_AGENTS = [{"agent": "haku", "token_env_var": _AGENT_TOKEN_ENV, "operator_subject_env": _AGENT_OPERATOR_ENV}]


@pytest.fixture(autouse=True)
def _static_agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_AGENT_TOKEN_ENV, _AGENT_TOKEN)
    monkeypatch.setenv(_AGENT_OPERATOR_ENV, "op-haku")


@pytest.fixture
def mcp_server_url(monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    monkeypatch.setenv("HAKU_CONSOLE_MCP_CREDENTIAL_HAKU_CONSOLE_GROCY_SF_TOKEN", "test-token")
    with serve_fastmcp(_test_mcp_server()) as url:
        yield url


def _alembic_config(conn: Connection) -> AlembicConfig:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    cfg.attributes["connection"] = conn
    cfg.attributes["target_metadata"] = metadata
    return cfg


def _upgrade(engine: Engine, revision: str) -> None:
    with engine.begin() as conn:
        alembic_command.upgrade(_alembic_config(conn), revision)


def _downgrade(engine: Engine, revision: str) -> None:
    with engine.begin() as conn:
        alembic_command.downgrade(_alembic_config(conn), revision)


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
    an `/api/tool-calls` caller presenting its bearer authenticates as the agent; absent a
    bearer/operator the endpoint 401s (under app-owned auth) rather than silently assuming operator."""
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
    with make_operator_client(config_file=console_config, csrf_secret=SecretStr("csrf")) as client:
        yield client


def _operator_oauth_config_file(tmp_path: Path, oauth_base_url: str) -> Path:
    servers = [{"id": "grocy-sf", "server_url": f"{oauth_base_url}/mcp", "operator_oauth": {}}]
    return write_config(tmp_path / "haku_console_operator_oauth.yaml", _config(servers))


def _gmail_config_file(tmp_path: Path) -> Path:
    return write_config(tmp_path / "haku_console_gmail.yaml", _config([{"id": "gmail"}]))


def _operator_oauth_static_client_config_file(tmp_path: Path, oauth_base_url: str, client_id: str) -> Path:
    servers = [
        {"id": "grocy-sf", "server_url": f"{oauth_base_url}/mcp", "operator_oauth": {"static_client_id": client_id}}
    ]
    return write_config(tmp_path / "haku_console_operator_oauth_static.yaml", _config(servers))


def _submit(client: TestClient, *, amount: int = 1) -> dict[str, Any]:
    # An operator-originated submit; the client must carry an operator session (the `operator_client`
    # fixture or `make_operator_client`).
    resp = client.post(
        "/api/tool-calls",
        json={
            "server_id": "grocy-sf",
            "tool_name": "stock_add",
            "title": "Add Thrive box items to Grocy",
            "rationale": "box is physically present",
            "arguments": {"items": [{"product_id": 123, "amount": amount}]},
            "wait_for_ms": 0,
        },
    )
    assert resp.status_code == 200, resp.text
    return cast(dict[str, Any], resp.json())


class RecordingExecutor:
    def __init__(self) -> None:
        self.auth_tokens: list[str | None] = []

    async def execute(
        self, server: Any, tool_name: str, arguments: dict[str, Any], auth_token: str | None
    ) -> dict[str, Any]:
        self.auth_tokens.append(auth_token)
        return {"content": [{"type": "text", "text": f"{server.id}:{tool_name}"}], "isError": False}


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
    }
    assert tools["stock_add"]["status"] == "alive"
    assert tools["stock_add"]["input_schema"]["type"] == "object"
    assert tools["echo"]["status"] == "alive"
    assert tools["echo"]["input_schema"]["type"] == "object"
    assert "bearer_token_secret" not in str(body)


def test_operator_oauth_association_drives_tool_reflection(make_operator_client, tmp_path: Path, db_url: str) -> None:
    metadata_provider = RecordingMetadataProvider()
    with (
        _remote_oauth_url() as oauth_url,
        make_operator_client(
            config_file=_operator_oauth_config_file(tmp_path, oauth_url),
            csrf_secret=SecretStr("csrf"),
            public_base_url="https://haku.test",
            tool_call_metadata_provider=metadata_provider,
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
    make_operator_client, tmp_path: Path, db_url: str
) -> None:
    """Mirrors kubectl-passthrough-mcp: fronted by Authentik, which has no DCR endpoint —
    dynamic registration would 401, so a configured static_client_id must skip it entirely.
    """
    with (
        _remote_oauth_url(static_client_id="preregistered-client") as oauth_url,
        make_operator_client(
            config_file=_operator_oauth_static_client_config_file(tmp_path, oauth_url, "preregistered-client"),
            csrf_secret=SecretStr("csrf"),
            public_base_url="https://haku.test",
        ) as client,
    ):
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


def test_operator_oauth_callback_is_bound_to_flow_operator(make_operator_client, tmp_path: Path, db_url: str) -> None:
    with (
        _remote_oauth_url() as oauth_url,
        make_operator_client(
            config_file=_operator_oauth_config_file(tmp_path, oauth_url),
            csrf_secret=SecretStr("csrf"),
            operator_subject="operator-a",
            operator_username="a@example.com",
        ) as operator_a,
        make_operator_client(
            config_file=_operator_oauth_config_file(tmp_path, oauth_url),
            csrf_secret=SecretStr("csrf"),
            operator_subject="operator-b",
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


def test_grocy_sf_reference_resolves_ids_to_names(make_operator_client, console_config: Path) -> None:
    with make_operator_client(config_file=console_config) as client:
        resp = client.get("/api/grocy-sf/reference")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "products": [
            {
                "id": 1,
                "name": "Milk",
                "location_id": 2,
                "qu_id_stock": 3,
                "qu_id_purchase": 3,
                "qu_id_consume": 3,
                "min_stock_amount": 1.0,
                "default_best_before_days": 7,
                "due_type": 1,
                # "0" is Grocy's "unset FK" encoding — parsed to None, not group 0.
                "parent_product_id": None,
                "product_group_id": 4,
                "description": "Whole milk",
                "calories": None,
            }
        ],
        "locations": [{"id": 2, "name": "Fridge"}],
        "quantity_units": [{"id": 3, "name": "Liter"}],
        "product_groups": [{"id": 4, "name": "Dairy"}],
        "shopping_lists": [{"id": 5, "name": "Weekly"}],
    }


def test_reflection_marks_unreachable_servers_degraded(
    make_operator_client, tmp_path: Path, db_url: str, monkeypatch: pytest.MonkeyPatch
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


def test_submit_mints_tool_call_id(operator_client: TestClient) -> None:
    first = _submit(operator_client)
    second = _submit(operator_client)
    assert first["tool_call_id"].startswith("tc_")
    assert first["status"] == "pending_approval"
    assert "approval_id" not in first
    assert second["tool_call_id"] != first["tool_call_id"]


def test_tool_call_rejects_ambiguous_operator_and_agent_credentials(operator_client: TestClient) -> None:
    response = operator_client.post(
        "/api/tool-calls",
        headers={"Authorization": "Bearer tool-token"},
        json={"server_id": "smoke", "tool_name": "echo", "arguments": {}, "wait_for_ms": 0},
    )
    assert response.status_code == 400
    assert "exactly one" in response.json()["detail"]


def test_haku_gmail_labels_list_auto_approves_executes_and_records_policy(
    make_client, make_operator_client, tmp_path: Path, db_url: str
) -> None:
    gmail = Mock()
    config_file = _gmail_config_file(tmp_path)
    with (
        make_client(
            config_file=config_file,
            gmail_client=gmail,
            in_process_servers={"gmail": build_gmail_mcp(gmail)},
            tool_call_executor=EchoingExecutor(),
        ) as client,
        make_operator_client(config_file=config_file, operator_subject="op-haku") as operator,
    ):
        response = client.post(
            "/api/tool-calls",
            headers={"Authorization": "Bearer tool-token"},
            json={"server_id": "gmail", "tool_name": "labels_list", "arguments": {}, "wait_for_ms": 0},
        )
        pending = operator.get("/api/approvals/pending").json()

    assert response.status_code == 200, response.text
    record = response.json()
    assert record["status"] == "ok"
    assert record["approval_policy_id"] == "unconditional_v1"
    assert record["auto_approval_evaluation"] == "approved: gmail/labels_list is allowlisted read-only/safe"
    assert record["approved_at"] is not None
    assert record["result"]["content"][0]["text"] == "gmail:labels_list"
    assert pending["approvals"] == []


def test_operator_gmail_labels_list_stays_pending(make_operator_client, tmp_path: Path, db_url: str) -> None:
    with make_operator_client(
        config_file=_gmail_config_file(tmp_path),
        gmail_client=Mock(),
        tool_call_executor=EchoingExecutor(),
        # Matching a configured agent id must not turn an operator into an auto-approved agent.
        operator_username="haku",
    ) as client:
        response = client.post(
            "/api/tool-calls",
            json={"server_id": "gmail", "tool_name": "labels_list", "arguments": {}, "wait_for_ms": 0},
        )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pending_approval"
    assert response.json()["approval_policy_id"] is None
    assert response.json()["auto_approval_evaluation"] is None


def test_haku_gmail_nonmatching_policy_evaluation_is_recorded(
    make_client, make_operator_client, tmp_path: Path, db_url: str
) -> None:
    gmail = Mock()
    config_file = _gmail_config_file(tmp_path)
    with (
        make_client(
            config_file=config_file, gmail_client=gmail, in_process_servers={"gmail": build_gmail_mcp(gmail)}
        ) as client,
        make_operator_client(config_file=config_file, operator_subject="op-haku") as operator,
    ):
        response = client.post(
            "/api/tool-calls",
            headers={"Authorization": "Bearer tool-token"},
            json={
                "server_id": "gmail",
                "tool_name": "threads_modify_labels",
                "arguments": {"thread_ids": ["t1"], "add": ["INBOX"]},
                "wait_for_ms": 0,
            },
        )
        pending = operator.get("/api/approvals/pending").json()["approvals"]

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pending_approval"
    assert response.json()["approval_policy_id"] is None
    assert response.json()["auto_approval_evaluation"] == "manual: at least one label name is outside 'haku/'"
    assert pending[0]["auto_approval_evaluation"] == response.json()["auto_approval_evaluation"]


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


def test_operator_oauth_association_drives_approved_tool_execution(
    make_operator_client, tmp_path: Path, db_url: str
) -> None:
    executor = RecordingExecutor()
    with (
        _remote_oauth_url() as oauth_url,
        make_operator_client(
            config_file=_operator_oauth_config_file(tmp_path, oauth_url),
            csrf_secret=SecretStr("csrf"),
            public_base_url="https://haku.test",
            tool_call_executor=executor,
        ) as client,
    ):
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
    assert executor.auth_tokens == ["operator-access-token"]


def test_operator_oauth_approval_requires_existing_association(
    make_operator_client, tmp_path: Path, db_url: str
) -> None:
    executor = RecordingExecutor()
    with (
        _remote_oauth_url() as oauth_url,
        make_operator_client(
            config_file=_operator_oauth_config_file(tmp_path, oauth_url),
            csrf_secret=SecretStr("csrf"),
            tool_call_executor=executor,
        ) as client,
    ):
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
    assert executor.auth_tokens == []


def _seed_association(db_url: str, *, operator_subject: str, access_token: str) -> None:
    """Insert a connected operator_oauth association for grocy-sf (bypassing the DCR/PKCE flow)."""
    engine = create_engine(db_url)
    now = datetime.datetime.now(datetime.UTC)
    try:
        with sessionmaker(engine)() as session, session.begin():
            session.add(
                McpOperatorOAuthAssociation(
                    server_id="grocy-sf",
                    operator_subject=operator_subject,
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
    make_client, tmp_path: Path, migrated_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two static agents bound to two operators: each agent's auto-approved operator_oauth call
    executes with *its* operator's token, with no crosstalk."""
    # `haku` (bearer tool-token → op-haku) comes from the autouse env; add a second agent `ops-bot`.
    monkeypatch.setenv("HAKU_CONSOLE_TEST_AGENT2_TOKEN", "ops-token")
    monkeypatch.setenv("HAKU_CONSOLE_TEST_AGENT2_OPERATOR", "op-ops")
    _seed_association(migrated_db_url, operator_subject="op-haku", access_token="grocy-token-haku")
    _seed_association(migrated_db_url, operator_subject="op-ops", access_token="grocy-token-ops")

    config = _config([{"id": "grocy-sf", "server_url": "http://unused.test/mcp", "operator_oauth": {}}])
    config["static_agents"] = [
        *_STATIC_AGENTS,
        {
            "agent": "ops-bot",
            "token_env_var": "HAKU_CONSOLE_TEST_AGENT2_TOKEN",
            "operator_subject_env": "HAKU_CONSOLE_TEST_AGENT2_OPERATOR",
        },
    ]
    executor = RecordingExecutor()
    with make_client(
        config_file=write_config(tmp_path / "routing.yaml", config), tool_call_executor=executor
    ) as client:
        # products_list is an unconditionally auto-approved grocy read, so each call runs immediately.
        call_ids: list[str] = []
        for bearer in ("tool-token", "ops-token"):
            resp = client.post(
                "/api/tool-calls",
                headers={"Authorization": f"Bearer {bearer}"},
                json={"server_id": "grocy-sf", "tool_name": "products_list", "arguments": {}, "wait_for_ms": 0},
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == "ok", resp.json()
            call_ids.append(resp.json()["tool_call_id"])

        for bearer, expected_call_id in zip(("tool-token", "ops-token"), call_ids, strict=True):
            listed = client.get("/api/tool-calls", headers={"Authorization": f"Bearer {bearer}"}).json()["tool_calls"]
            assert [call["tool_call_id"] for call in listed] == [expected_call_id]

    # haku's call executed with op-haku's token; ops-bot's with op-ops's — each routed to its operator.
    assert executor.auth_tokens == ["grocy-token-haku", "grocy-token-ops"]


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
    resp = operator_client.post(
        "/api/tool-calls",
        json={"server_id": "smoke", "tool_name": "echo", "arguments": {"text": "world"}, "wait_for_ms": 1000},
    )
    pending = operator_client.get("/api/approvals/pending").json()
    listed = operator_client.get(
        "/api/tool-calls", params={"status": "pending_approval", "since": "1970-01-01T00:00:00+00:00"}
    ).json()
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending_approval"
    assert body["result"] is None
    assert pending["approvals"][0]["tool_call_id"] == body["tool_call_id"]
    assert pending["approvals"][0]["title"] is None
    assert listed["tool_calls"][0]["tool_call_id"] == body["tool_call_id"]


def test_operator_tenants_cannot_read_or_decide_each_others_calls(make_operator_client, console_config: Path) -> None:
    with (
        make_operator_client(
            config_file=console_config,
            csrf_secret=SecretStr("csrf"),
            operator_subject="operator-a",
            operator_username="a@example.com",
        ) as operator_a,
        make_operator_client(
            config_file=console_config,
            csrf_secret=SecretStr("csrf"),
            operator_subject="operator-b",
            operator_username="b@example.com",
        ) as operator_b,
    ):
        submitted = _submit(operator_a)
        call_id = submitted["tool_call_id"]

        assert [row["tool_call_id"] for row in operator_a.get("/api/tool-calls").json()["tool_calls"]] == [call_id]
        assert operator_b.get("/api/tool-calls").json()["tool_calls"] == []
        assert operator_b.get(f"/api/tool-calls/{call_id}").status_code == 404
        assert operator_b.get("/api/approvals/pending").json()["approvals"] == []
        assert operator_b.get("/api/approvals/events").json()["events"] == []

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


def test_websocket_receives_pending_approval_event(operator_client: TestClient) -> None:
    with operator_client.websocket_connect("/api/events/ws", headers={"Origin": "https://haku.test"}) as ws:
        assert ws.receive_json() == {"event_type": "hello"}
        submitted = _submit(operator_client)
        event = ws.receive_json()
        events = operator_client.get("/api/approvals/events", params={"after_event_id": 0}).json()
    assert event["event_type"] == "tool_call_submitted"
    assert event["tool_call_id"] == submitted["tool_call_id"]
    assert event["status"] == "pending_approval"
    assert events["events"][0]["event_id"] == event["event_id"]


def test_websocket_rejects_cross_origin(make_operator_client) -> None:
    with (
        make_operator_client(public_base_url="https://haku.test") as client,
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
        make_operator_client(config_file=console_config, operator_subject="operator-sub") as operator,
        make_operator_client(config_file=console_config, operator_subject="op-haku") as haku_operator,
    ):
        operator_call = operator.post(
            "/api/tool-calls",
            json={"server_id": "smoke", "tool_name": "echo", "arguments": {"text": "one"}, "wait_for_ms": 0},
        ).json()
        haku_call = agent.post(
            "/api/tool-calls",
            headers={"Authorization": "Bearer tool-token"},
            json={"server_id": "smoke", "tool_name": "echo", "arguments": {"text": "two"}, "wait_for_ms": 0},
        ).json()
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
    submitted = operator_client.post(
        "/api/tool-calls",
        json={"server_id": "smoke", "tool_name": "echo", "arguments": {"text": "world"}, "wait_for_ms": 0},
    ).json()
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
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
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
            row = cast(
                dict[str, Any],
                conn.execute(
                    text(
                        """
                        SELECT operator_subject, server_id, tool_name, status, arguments_json, result_json
                        FROM mcp_tool_calls
                        WHERE tool_call_id = :tool_call_id
                        """
                    ),
                    {"tool_call_id": submitted["tool_call_id"]},
                )
                .mappings()
                .one(),
            )
    finally:
        engine.dispose()

    assert version == "0007"
    assert {"mcp_operator_oauth_associations", "mcp_operator_oauth_flows", "mcp_agent_operator"} <= tables
    assert {"mcp_tool_calls_legacy_unowned", "mcp_tool_call_events_legacy_unowned"}.isdisjoint(tables)
    assert {
        "tool_call_id",
        "operator_subject",
        "server_id",
        "tool_name",
        "caller_principal",
        "status",
        "created_at",
        "updated_at",
        "arguments_json",
        "rationale",
        "title",
        "result_json",
        "error",
        "denial_reason",
        "approval_policy_id",
        "auto_approval_evaluation",
        "approved_at",
    } == columns
    assert row["operator_subject"] == "operator-sub"
    assert row["server_id"] == "smoke"
    assert row["tool_name"] == "echo"
    assert row["status"] == "ok"
    assert row["arguments_json"] == {"text": "world"}
    assert row["result_json"]["content"][0]["text"] == "echo:world"


def test_0007_deletes_unowned_history_and_is_forward_only(db_url: str) -> None:
    engine = create_engine(db_url)
    try:
        _upgrade(engine, "0006")
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO mcp_tool_calls (
                        tool_call_id, server_id, tool_name, caller_principal, status,
                        created_at, updated_at, arguments_json, rationale
                    ) VALUES (
                        'tc_legacy', 'smoke', 'echo', 'legacy-user', 'pending_approval',
                        now(), now(), '{}'::jsonb, 'legacy call with no persisted owner'
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO mcp_tool_call_events (event_type, tool_call_id, status, created_at)
                    VALUES
                        ('tool_call_submitted', 'tc_legacy', 'pending_approval', now()),
                        ('tool_call_submitted', 'tc_orphan', 'pending_approval', now())
                    """
                )
            )

        _upgrade(engine, "head")
        with engine.connect() as conn:
            active_calls = conn.execute(text("SELECT count(*) FROM mcp_tool_calls")).scalar_one()
            active_events = conn.execute(text("SELECT count(*) FROM mcp_tool_call_events")).scalar_one()
            legacy_tables = set(
                conn.execute(
                    text(
                        """
                        SELECT table_name
                        FROM information_schema.tables
                        WHERE table_schema = 'public'
                          AND table_name IN (
                            'mcp_tool_calls_legacy_unowned',
                            'mcp_tool_call_events_legacy_unowned'
                          )
                        """
                    )
                ).scalars()
            )
            nullable = {
                (row["table_name"], row["is_nullable"])
                for row in conn.execute(
                    text(
                        """
                        SELECT table_name, is_nullable
                        FROM information_schema.columns
                        WHERE column_name = 'operator_subject'
                          AND table_name IN ('mcp_tool_calls', 'mcp_tool_call_events')
                        """
                    )
                ).mappings()
            }
            indexes = set(
                conn.execute(
                    text(
                        """
                        SELECT indexname
                        FROM pg_indexes
                        WHERE schemaname = 'public'
                          AND tablename IN ('mcp_tool_calls', 'mcp_tool_call_events')
                        """
                    )
                ).scalars()
            )

        assert active_calls == 0
        assert active_events == 0
        assert legacy_tables == set()
        assert nullable == {("mcp_tool_calls", "NO"), ("mcp_tool_call_events", "NO")}
        assert {
            "idx_mcp_tool_calls_operator_subject_created_at",
            "idx_mcp_tool_call_events_operator_subject_event_id",
        } <= indexes
        with pytest.raises(RuntimeError, match="forward-only"):
            _downgrade(engine, "0006")
    finally:
        engine.dispose()


def test_historical_enum_migration_reaches_current_head(db_url: str) -> None:
    engine = create_engine(db_url)
    try:
        _upgrade(engine, "0001")
        assert _enum_values(engine) == {
            "tool_call_event_type": ("tool_call_submitted", "approval_pending", "tool_call_updated"),
            "tool_call_status": ("pending_approval", "running", "ok", "error", "denied"),
        }

        _upgrade(engine, "head")
        upgraded_values = _enum_values(engine)
    finally:
        engine.dispose()

    current_values = {
        "tool_call_event_type": tuple(event_type.value for event_type in ToolCallEventType),
        "tool_call_status": tuple(status.value for status in ToolCallStatus),
    }
    assert upgraded_values == current_values


# --- In-process MCP servers (McpToolExecutor/McpMetadataProvider in-process registration) ---
# Unit tests only: no postgres/network fixtures, exercising McpToolExecutor/McpMetadataProvider
# directly (over the module-level `_test_mcp_server()` FastMCP fixture, in-memory — no HTTP)
# rather than through the FastAPI app.


def test_server_entry_allows_missing_server_url() -> None:
    McpServerEntry(id="google")  # ok: resolved via the in-process registry at runtime, not this model


async def test_executor_dispatches_to_registered_in_process_server() -> None:
    executor = McpToolExecutor({"google": _test_mcp_server()})
    server = McpServerEntry(id="google")
    result = await executor.execute(server, "echo", {"text": "hi"}, auth_token=None)
    assert result["content"][0]["text"] == "echo:hi"


async def test_executor_raises_when_no_server_url_and_not_registered() -> None:
    executor = McpToolExecutor({})
    server = McpServerEntry(id="google")
    with pytest.raises(RuntimeError, match="no server_url and no in-process registration"):
        await executor.execute(server, "echo", {}, auth_token=None)


async def test_metadata_provider_reflects_in_process_server_tools() -> None:
    metadata_provider = McpMetadataProvider({"google": _test_mcp_server()})
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
    }


async def test_metadata_provider_degrades_when_no_server_url_and_not_registered() -> None:
    metadata_provider = McpMetadataProvider({})
    server = McpServerEntry(id="google")
    metadata = await metadata_provider.metadata(server, auth_token=None)
    assert metadata.status == "degraded"


if __name__ == "__main__":
    pytest_bazel.main()
