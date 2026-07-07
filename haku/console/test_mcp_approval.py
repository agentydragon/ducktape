"""Operator-approved MCP tool-call API tests."""

from __future__ import annotations

import re
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_bazel
import uvicorn
from fastapi.testclient import TestClient
from fastmcp import FastMCP
from mcp import types as mcp_types
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from testcontainers.postgres import PostgresContainer

from haku.console.mcp_approval import _mcp_result_to_json
from third_party.containers.rlocations import POSTGRES_18, RYUK
from util.net import pick_free_port, wait_for_port
from util.oci import load_oci_image
from util.testing.postgres import force_drop_database_sync


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

    return server


@contextmanager
def _remote_mcp_url(server: FastMCP) -> Generator[str]:
    port = pick_free_port()
    mcp_app = server.http_app(path="/")
    app = Starlette(routes=[Mount("/mcp", app=mcp_app)], lifespan=mcp_app.lifespan)
    uvicorn_server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=uvicorn_server.run, daemon=True)
    thread.start()
    try:
        wait_for_port("127.0.0.1", port, timeout_secs=10)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        uvicorn_server.should_exit = True
        thread.join(timeout=10)
        if thread.is_alive():
            raise RuntimeError("test MCP server did not stop")


@contextmanager
def _remote_oauth_url() -> Generator[str]:
    port = pick_free_port()
    base_url = f"http://127.0.0.1:{port}"

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
        return JSONResponse(
            {
                "issuer": f"{base_url}/auth",
                "authorization_endpoint": f"{base_url}/auth/authorize",
                "token_endpoint": f"{base_url}/auth/token",
                "registration_endpoint": f"{base_url}/auth/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
            }
        )

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
            assert form["client_id"] == "dynamic-client"
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

    app = Starlette(
        routes=[
            Route("/mcp", mcp),
            Route("/.well-known/oauth-protected-resource/mcp", protected_resource),
            Route("/.well-known/oauth-protected-resource", protected_resource),
            Route("/.well-known/oauth-authorization-server/auth", oauth_metadata),
            Route("/auth/register", register, methods=["POST"]),
            Route("/auth/token", token, methods=["POST"]),
        ]
    )
    uvicorn_server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=uvicorn_server.run, daemon=True)
    thread.start()
    try:
        wait_for_port("127.0.0.1", port, timeout_secs=10)
        yield base_url
    finally:
        uvicorn_server.should_exit = True
        thread.join(timeout=10)
        if thread.is_alive():
            raise RuntimeError("test OAuth server did not stop")


@pytest.fixture(scope="session", autouse=True)
def _preload_postgres_images() -> None:
    load_oci_image(RYUK)
    load_oci_image(POSTGRES_18)


@pytest.fixture(scope="session")
def postgres_container() -> Generator[PostgresContainer]:
    container = PostgresContainer(image=POSTGRES_18.tag, username="postgres", password="postgres", dbname="postgres")
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="session")
def postgres_admin_url(postgres_container: PostgresContainer) -> str:
    host = postgres_container.get_container_host_ip()
    port = int(postgres_container.get_exposed_port(5432))
    return f"postgresql+psycopg://postgres:postgres@{host}:{port}/postgres"


@pytest.fixture
def db_url(postgres_admin_url: str, request: pytest.FixtureRequest) -> Generator[str]:
    db_name = re.sub(r"[^a-z0-9_]", "_", request.node.name.lower())[:45].rstrip("_") or "haku_console_test"
    admin_engine = create_engine(postgres_admin_url, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    admin_engine.dispose()

    yield postgres_admin_url.rsplit("/", 1)[0] + f"/{db_name}"

    force_drop_database_sync(postgres_admin_url, db_name)


@pytest.fixture
def mcp_server_url(monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    monkeypatch.setenv("HAKU_CONSOLE_MCP_CREDENTIAL_HAKU_CONSOLE_GROCY_SF_TOKEN", "test-token")
    with _remote_mcp_url(_test_mcp_server()) as url:
        yield url


def _test_app_overrides(db_url: str) -> dict[str, Any]:
    return {"database_url": SecretStr(db_url)}


def _config_file(tmp_path: Path, mcp_server_url: str) -> Path:
    path = tmp_path / "haku_console.yaml"
    path.write_text(
        f"""
mcp:
  servers:
    - id: grocy-sf
      server_url: {mcp_server_url}
      bearer_token_secret: haku-console-grocy-sf-token
    - id: smoke
      server_url: {mcp_server_url}
""",
        encoding="utf-8",
    )
    return path


def _operator_oauth_config_file(tmp_path: Path, oauth_base_url: str) -> Path:
    path = tmp_path / "haku_console_operator_oauth.yaml"
    path.write_text(
        f"""
mcp:
  servers:
    - id: grocy-sf
      server_url: {oauth_base_url}/mcp
      operator_oauth: {{}}
""",
        encoding="utf-8",
    )
    return path


def _csrf(client: TestClient) -> str:
    token = client.get("/api/capabilities/csrf").json()["csrf_token"]
    assert isinstance(token, str)
    return token


def _submit(client: TestClient, *, amount: int = 1) -> dict[str, Any]:
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
        return {
            "content": [{"type": "text", "text": f"{server.id}:{tool_name}:{arguments['items'][0]['amount']}"}],
            "isError": False,
        }


def test_reflection_lists_connected_servers_without_leaking_credentials(
    make_client, tmp_path: Path, db_url: str, mcp_server_url: str
) -> None:
    with make_client(config_file=_config_file(tmp_path, mcp_server_url), **_test_app_overrides(db_url)) as client:
        resp = client.get("/api/capabilities/mcp-servers")
    assert resp.status_code == 200
    body = resp.json()
    server = body["servers"][0]
    assert server["server_id"] == "grocy-sf"
    assert server["status"] == "alive"
    tools = {tool["name"]: tool for tool in server["tools"]}
    assert set(tools) == {"echo", "stock_add"}
    assert tools["stock_add"]["status"] == "alive"
    assert tools["stock_add"]["input_schema"]["type"] == "object"
    assert tools["echo"]["status"] == "alive"
    assert tools["echo"]["input_schema"]["type"] == "object"
    assert "bearer_token_secret" not in str(body)


def test_reflection_marks_unreachable_servers_degraded(
    make_client, tmp_path: Path, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HAKU_CONSOLE_MCP_CREDENTIAL_HAKU_CONSOLE_GROCY_SF_TOKEN", "test-token")
    with make_client(
        config_file=_config_file(tmp_path, "http://127.0.0.1:1/mcp"), **_test_app_overrides(db_url)
    ) as client:
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


def test_submit_mints_tool_call_id(make_client, tmp_path: Path, db_url: str, mcp_server_url: str) -> None:
    with make_client(config_file=_config_file(tmp_path, mcp_server_url), **_test_app_overrides(db_url)) as client:
        first = _submit(client)
        second = _submit(client)
    assert first["tool_call_id"].startswith("tc_")
    assert first["status"] == "pending_approval"
    assert "approval_id" not in first
    assert second["tool_call_id"] != first["tool_call_id"]


def test_approval_executes_tool_and_records_terminal_result(
    make_client, tmp_path: Path, db_url: str, mcp_server_url: str
) -> None:
    with make_client(
        config_file=_config_file(tmp_path, mcp_server_url), csrf_secret=SecretStr("csrf"), **_test_app_overrides(db_url)
    ) as client:
        submitted = _submit(client)
        resp = client.post(
            f"/api/tool-calls/{submitted['tool_call_id']}/decision",
            headers={"X-CSRF-Token": _csrf(client)},
            json={"decision": "approve"},
        )
        fetched = client.get(f"/api/tool-calls/{submitted['tool_call_id']}").json()
    assert resp.status_code == 200, resp.text
    decided = resp.json()["tool_call"]
    assert decided["status"] == "ok"
    assert decided["result"]["content"][0]["text"] == "stock_add:123:1"
    assert fetched == decided


def test_operator_oauth_association_drives_approved_tool_execution(make_client, tmp_path: Path, db_url: str) -> None:
    executor = RecordingExecutor()
    with (
        _remote_oauth_url() as oauth_url,
        make_client(
            config_file=_operator_oauth_config_file(tmp_path, oauth_url),
            csrf_secret=SecretStr("csrf"),
            public_base_url="https://haku.test",
            tool_call_executor=executor,
            **_test_app_overrides(db_url),
        ) as client,
    ):
        before = client.get("/api/mcp/operator-auth", headers={"X-authentik-username": "operator@example.com"}).json()
        assert before["associations"] == [
            {
                "server_id": "grocy-sf",
                "status": "unconnected",
                "operator_principal": "operator@example.com",
                "connected_at": None,
                "token_expires_at": None,
                "scope": None,
            }
        ]

        started = client.post(
            "/api/mcp/operator-auth/grocy-sf/start",
            headers={"X-CSRF-Token": _csrf(client), "X-authentik-username": "operator@example.com"},
        )
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
        after = client.get("/api/mcp/operator-auth", headers={"X-authentik-username": "operator@example.com"}).json()
        assert after["associations"][0]["status"] == "connected"
        assert after["associations"][0]["connected_at"]

        submitted = _submit(client)
        approved = client.post(
            f"/api/tool-calls/{submitted['tool_call_id']}/decision",
            headers={"X-CSRF-Token": _csrf(client), "X-authentik-username": "operator@example.com"},
            json={"decision": "approve"},
        )

    assert approved.status_code == 200, approved.text
    assert approved.json()["tool_call"]["status"] == "ok"
    assert executor.auth_tokens == ["operator-access-token"]


def test_operator_oauth_approval_requires_existing_association(make_client, tmp_path: Path, db_url: str) -> None:
    executor = RecordingExecutor()
    with (
        _remote_oauth_url() as oauth_url,
        make_client(
            config_file=_operator_oauth_config_file(tmp_path, oauth_url),
            csrf_secret=SecretStr("csrf"),
            tool_call_executor=executor,
            **_test_app_overrides(db_url),
        ) as client,
    ):
        submitted = _submit(client)
        resp = client.post(
            f"/api/tool-calls/{submitted['tool_call_id']}/decision",
            headers={"X-CSRF-Token": _csrf(client), "X-authentik-username": "operator@example.com"},
            json={"decision": "approve"},
        )
        fetched = client.get(f"/api/tool-calls/{submitted['tool_call_id']}").json()

    assert resp.status_code == 409
    assert "Connect your grocy-sf MCP account" in resp.json()["detail"]
    assert fetched["status"] == "pending_approval"
    assert executor.auth_tokens == []


def test_approval_denial_is_terminal_and_does_not_execute(
    make_client, tmp_path: Path, db_url: str, mcp_server_url: str
) -> None:
    with make_client(
        config_file=_config_file(tmp_path, mcp_server_url), csrf_secret=SecretStr("csrf"), **_test_app_overrides(db_url)
    ) as client:
        submitted = _submit(client)
        resp = client.post(
            f"/api/tool-calls/{submitted['tool_call_id']}/decision",
            headers={"X-CSRF-Token": _csrf(client)},
            json={"decision": "deny", "reason": "not today"},
        )
    assert resp.status_code == 200
    tool_call = resp.json()["tool_call"]
    assert tool_call["status"] == "denied"
    assert tool_call["result"] is None


def test_all_v1_tool_calls_require_console_approval(
    make_client, tmp_path: Path, db_url: str, mcp_server_url: str
) -> None:
    with make_client(config_file=_config_file(tmp_path, mcp_server_url), **_test_app_overrides(db_url)) as client:
        resp = client.post(
            "/api/tool-calls",
            json={"server_id": "smoke", "tool_name": "echo", "arguments": {"text": "world"}, "wait_for_ms": 1000},
        )
        pending = client.get("/api/approvals/pending").json()
        listed = client.get(
            "/api/tool-calls", params={"status": "pending_approval", "since": "1970-01-01T00:00:00+00:00"}
        ).json()
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending_approval"
    assert body["result"] is None
    assert pending["approvals"][0]["tool_call_id"] == body["tool_call_id"]
    assert pending["approvals"][0]["title"] is None
    assert listed["tool_calls"][0]["tool_call_id"] == body["tool_call_id"]


def test_websocket_receives_pending_approval_event(
    make_client, tmp_path: Path, db_url: str, mcp_server_url: str
) -> None:
    with (
        make_client(config_file=_config_file(tmp_path, mcp_server_url), **_test_app_overrides(db_url)) as client,
        client.websocket_connect("/api/approvals/ws") as ws,
    ):
        assert ws.receive_json() == {"type": "hello"}
        submitted = _submit(client)
        event = ws.receive_json()
        events = client.get("/api/approvals/events", params={"after_event_id": 0}).json()
    assert event["event_type"] == "tool_call_submitted"
    assert event["tool_call_id"] == submitted["tool_call_id"]
    assert event["status"] == "pending_approval"
    assert events["events"][0]["event_id"] == event["event_id"]


def test_full_audit_log_listing_and_secret_redaction(
    make_client, tmp_path: Path, db_url: str, mcp_server_url: str
) -> None:
    with make_client(
        config_file=_config_file(tmp_path, mcp_server_url),
        agent_api_token=SecretStr("tool-token"),
        **_test_app_overrides(db_url),
    ) as client:
        operator_call = client.post(
            "/api/tool-calls",
            headers={"X-authentik-username": "operator@example.com"},
            json={"server_id": "smoke", "tool_name": "echo", "arguments": {"text": "one"}, "wait_for_ms": 0},
        ).json()
        haku_call = client.post(
            "/api/tool-calls",
            headers={"Authorization": "Bearer tool-token"},
            json={"server_id": "smoke", "tool_name": "echo", "arguments": {"text": "two"}, "wait_for_ms": 0},
        ).json()
        body = client.get("/api/tool-calls").json()
        pending = client.get(
            "/api/tool-calls", params=[("status", "pending_approval"), ("since", "1970-01-01T00:00:00+00:00")]
        ).json()
        future = client.get("/api/tool-calls", params={"since": "2999-01-01T00:00:00+00:00"}).json()
    ids = {r["tool_call_id"] for r in body["tool_calls"]}
    assert {operator_call["tool_call_id"], haku_call["tool_call_id"]} <= ids
    pending_ids = {r["tool_call_id"] for r in pending["tool_calls"]}
    assert {operator_call["tool_call_id"], haku_call["tool_call_id"]} <= pending_ids
    assert future["tool_calls"] == []
    dumped = str(body)
    assert "haku-console-grocy-sf-token" not in dumped
    assert "tool-token" not in dumped


def test_postgres_store_runs_alembic_and_persists_typed_ledger(
    make_client, tmp_path: Path, db_url: str, mcp_server_url: str
) -> None:
    with make_client(
        config_file=_config_file(tmp_path, mcp_server_url), csrf_secret=SecretStr("csrf"), **_test_app_overrides(db_url)
    ) as client:
        submitted = client.post(
            "/api/tool-calls",
            json={"server_id": "smoke", "tool_name": "echo", "arguments": {"text": "world"}, "wait_for_ms": 0},
        ).json()
        approved = client.post(
            f"/api/tool-calls/{submitted['tool_call_id']}/decision",
            headers={"X-CSRF-Token": _csrf(client)},
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
                        SELECT server_id, tool_name, status, arguments_json, result_json
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

    assert version == "0002"
    assert {"mcp_operator_oauth_associations", "mcp_operator_oauth_flows"} <= tables
    assert {
        "tool_call_id",
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
    } == columns
    assert row["server_id"] == "smoke"
    assert row["tool_name"] == "echo"
    assert row["status"] == "ok"
    assert row["arguments_json"] == {"text": "world"}
    assert row["result_json"]["content"][0]["text"] == "echo:world"


if __name__ == "__main__":
    pytest_bazel.main()
