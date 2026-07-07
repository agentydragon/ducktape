"""Operator-approved MCP tool-call API tests."""

from __future__ import annotations

import re
from collections.abc import Generator
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_bazel
from fastapi.testclient import TestClient
from mcp import types as mcp_types
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from haku.console.mcp_approval import McpServerEntry, ServerMetadata, ToolMetadata, _mcp_result_to_json
from third_party.containers.rlocations import POSTGRES_18, RYUK
from util.oci import load_oci_image
from util.testing.postgres import force_drop_database_sync


class FakeToolExecutor:
    async def execute(self, server: McpServerEntry, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"fake_mcp": True, "server": server.id, "tool": tool_name, "arguments": arguments}


class FakeMetadataProvider:
    async def metadata(self, server: McpServerEntry) -> ServerMetadata:
        return ServerMetadata(
            server_id=server.id,
            title=server.id,
            tools=[
                ToolMetadata(
                    name="stock_add",
                    description="Fake stock add tool used by tests.",
                    input_schema={"type": "object", "additionalProperties": True},
                    schema_source="mcp",
                ),
                ToolMetadata(
                    name="echo",
                    description="Fake echo tool used by tests.",
                    input_schema={"type": "object", "additionalProperties": True},
                    schema_source="mcp",
                ),
            ],
            schema_source="mcp",
        )


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


def _test_app_overrides(db_url: str) -> dict[str, Any]:
    return {
        "database_url": SecretStr(db_url),
        "tool_call_executor": FakeToolExecutor(),
        "tool_call_metadata_provider": FakeMetadataProvider(),
    }


def _config_file(tmp_path: Path) -> Path:
    path = tmp_path / "haku_console.yaml"
    path.write_text(
        """
mcp:
  servers:
    - id: grocy-sf
      server_url: https://grocy-sf.example.test/mcp
      bearer_token_secret: haku-console-grocy-sf-token
    - id: smoke
      server_url: https://smoke.example.test/mcp
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


def test_reflection_lists_connected_servers_without_leaking_credentials(
    make_client, tmp_path: Path, db_url: str
) -> None:
    with make_client(config_file=_config_file(tmp_path), **_test_app_overrides(db_url)) as client:
        resp = client.get("/api/capabilities/mcp-servers")
    assert resp.status_code == 200
    body = resp.json()
    server = body["servers"][0]
    assert server == {
        "server_id": "grocy-sf",
        "title": "grocy-sf",
        "tools": [
            {
                "name": "stock_add",
                "description": "Fake stock add tool used by tests.",
                "input_schema": {"type": "object", "additionalProperties": True},
                "schema_source": "mcp",
                "degraded_reason": None,
            },
            {
                "name": "echo",
                "description": "Fake echo tool used by tests.",
                "input_schema": {"type": "object", "additionalProperties": True},
                "schema_source": "mcp",
                "degraded_reason": None,
            },
        ],
        "schema_source": "mcp",
        "degraded_reason": None,
    }
    assert "bearer_token_secret" not in str(body)


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


def test_submit_mints_tool_call_id(make_client, tmp_path: Path, db_url: str) -> None:
    with make_client(config_file=_config_file(tmp_path), **_test_app_overrides(db_url)) as client:
        first = _submit(client)
        second = _submit(client)
    assert first["tool_call_id"].startswith("tc_")
    assert first["status"] == "pending_approval"
    assert "approval_id" not in first
    assert second["tool_call_id"] != first["tool_call_id"]


def test_approval_executes_tool_and_records_terminal_result(make_client, tmp_path: Path, db_url: str) -> None:
    with make_client(
        config_file=_config_file(tmp_path), csrf_secret=SecretStr("csrf"), **_test_app_overrides(db_url)
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
    assert decided["result"]["fake_mcp"] is True
    assert decided["result"]["tool"] == "stock_add"
    assert fetched == decided


def test_approval_denial_is_terminal_and_does_not_execute(make_client, tmp_path: Path, db_url: str) -> None:
    with make_client(
        config_file=_config_file(tmp_path), csrf_secret=SecretStr("csrf"), **_test_app_overrides(db_url)
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


def test_all_v1_tool_calls_require_console_approval(make_client, tmp_path: Path, db_url: str) -> None:
    with make_client(config_file=_config_file(tmp_path), **_test_app_overrides(db_url)) as client:
        resp = client.post(
            "/api/tool-calls",
            json={"server_id": "smoke", "tool_name": "echo", "arguments": {"hello": "world"}, "wait_for_ms": 1000},
        )
        pending = client.get("/api/approvals/pending").json()
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending_approval"
    assert body["result"] is None
    assert pending["approvals"][0]["tool_call_id"] == body["tool_call_id"]


def test_websocket_receives_pending_approval_event(make_client, tmp_path: Path, db_url: str) -> None:
    with (
        make_client(config_file=_config_file(tmp_path), **_test_app_overrides(db_url)) as client,
        client.websocket_connect("/api/approvals/ws") as ws,
    ):
        assert ws.receive_json() == {"type": "hello"}
        submitted = _submit(client)
        event = ws.receive_json()
    assert event["event_type"] == "tool_call_submitted"
    assert event["tool_call_id"] == submitted["tool_call_id"]
    assert event["status"] == "pending_approval"


def test_full_audit_log_listing_and_secret_redaction(make_client, tmp_path: Path, db_url: str) -> None:
    with make_client(
        config_file=_config_file(tmp_path), agent_api_token=SecretStr("tool-token"), **_test_app_overrides(db_url)
    ) as client:
        operator_call = client.post(
            "/api/tool-calls",
            headers={"X-authentik-username": "operator@example.com"},
            json={"server_id": "smoke", "tool_name": "echo", "arguments": {"x": 1}, "wait_for_ms": 0},
        ).json()
        haku_call = client.post(
            "/api/tool-calls",
            headers={"Authorization": "Bearer tool-token"},
            json={"server_id": "smoke", "tool_name": "echo", "arguments": {"x": 2}, "wait_for_ms": 0},
        ).json()
        body = client.get("/api/tool-calls").json()
    ids = {r["tool_call_id"] for r in body["tool_calls"]}
    assert {operator_call["tool_call_id"], haku_call["tool_call_id"]} <= ids
    dumped = str(body)
    assert "haku-console-grocy-sf-token" not in dumped
    assert "tool-token" not in dumped


def test_postgres_store_runs_alembic_and_persists_typed_ledger(make_client, tmp_path: Path, db_url: str) -> None:
    with make_client(
        config_file=_config_file(tmp_path), csrf_secret=SecretStr("csrf"), **_test_app_overrides(db_url)
    ) as client:
        submitted = client.post(
            "/api/tool-calls",
            json={"server_id": "smoke", "tool_name": "echo", "arguments": {"hello": "world"}, "wait_for_ms": 0},
        ).json()
        approved = client.post(
            f"/api/tool-calls/{submitted['tool_call_id']}/decision",
            headers={"X-CSRF-Token": _csrf(client)},
            json={"decision": "approve"},
        ).json()["tool_call"]

    assert approved["status"] == "ok"
    assert approved["result"]["arguments"] == {"hello": "world"}

    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
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

    assert version == "0001"
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
    assert row["arguments_json"] == {"hello": "world"}
    assert row["result_json"]["fake_mcp"] is True


if __name__ == "__main__":
    pytest_bazel.main()
