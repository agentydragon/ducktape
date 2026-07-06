"""Operator-approved MCP tool-call API tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest_bazel
from fastapi.testclient import TestClient
from pydantic import SecretStr

from haku.console.mcp_approval import McpServerEntry, ServerMetadata, ToolMetadata


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


def _test_app_overrides() -> dict[str, Any]:
    return {"tool_call_executor": FakeToolExecutor(), "tool_call_metadata_provider": FakeMetadataProvider()}


def _catalog(tmp_path: Path) -> Path:
    path = tmp_path / "mcp_servers.yaml"
    path.write_text(
        """
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


def _submit(client: TestClient, *, client_request_id: str = "req-1", amount: int = 1) -> dict[str, Any]:
    resp = client.post(
        "/api/tool-calls",
        json={
            "server_id": "grocy-sf",
            "tool_name": "stock_add",
            "client_request_id": client_request_id,
            "state_request_id": "2026-07-thrive-box-grocy-stock-add",
            "title": "Add Thrive box items to Grocy",
            "rationale": "box is physically present",
            "arguments": {"items": [{"product_id": 123, "amount": amount}]},
        },
    )
    assert resp.status_code == 200, resp.text
    return cast(dict[str, Any], resp.json())


def test_reflection_lists_connected_servers_without_leaking_credentials(make_client, tmp_path: Path) -> None:
    with make_client(mcp_approval_catalog_path=_catalog(tmp_path), **_test_app_overrides()) as client:
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


def test_submit_mints_tool_call_id_and_idempotent_replay(make_client, tmp_path: Path) -> None:
    with make_client(mcp_approval_catalog_path=_catalog(tmp_path), **_test_app_overrides()) as client:
        first = _submit(client)
        replay = _submit(client)
        by_client_id = client.get("/api/tool-calls/by-client-request/req-1").json()
        conflict = client.post(
            "/api/tool-calls",
            json={
                "server_id": "grocy-sf",
                "tool_name": "stock_add",
                "client_request_id": "req-1",
                "arguments": {"items": [{"product_id": 123, "amount": 2}]},
            },
        )
    assert first["tool_call_id"].startswith("tc_")
    assert first["status"] == "pending_approval"
    assert "approval_id" not in first
    assert replay["tool_call_id"] == first["tool_call_id"]
    assert by_client_id["tool_call_id"] == first["tool_call_id"]
    assert conflict.status_code == 409


def test_approval_executes_tool_and_records_terminal_result(make_client, tmp_path: Path) -> None:
    with make_client(
        mcp_approval_catalog_path=_catalog(tmp_path), csrf_secret=SecretStr("csrf"), **_test_app_overrides()
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


def test_approval_denial_is_terminal_and_does_not_execute(make_client, tmp_path: Path) -> None:
    with make_client(
        mcp_approval_catalog_path=_catalog(tmp_path), csrf_secret=SecretStr("csrf"), **_test_app_overrides()
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
    assert tool_call["decision_reason"] == "not today"
    assert tool_call["result"] is None


def test_all_v1_tool_calls_require_console_approval(make_client, tmp_path: Path) -> None:
    with make_client(mcp_approval_catalog_path=_catalog(tmp_path), **_test_app_overrides()) as client:
        resp = client.post(
            "/api/tool-calls",
            json={
                "server_id": "smoke",
                "tool_name": "echo",
                "client_request_id": "smoke",
                "arguments": {"hello": "world"},
                "wait_for_ms": 1000,
            },
        )
        pending = client.get("/api/approvals/pending").json()
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending_approval"
    assert body["result"] is None
    assert pending["approvals"][0]["tool_call_id"] == body["tool_call_id"]


def test_websocket_receives_pending_approval_event(make_client, tmp_path: Path) -> None:
    with (
        make_client(mcp_approval_catalog_path=_catalog(tmp_path), **_test_app_overrides()) as client,
        client.websocket_connect("/api/approvals/ws") as ws,
    ):
        assert ws.receive_json() == {"type": "hello"}
        submitted = _submit(client, client_request_id="ws-req")
        event = ws.receive_json()
    assert event["event_type"] == "tool_call_submitted"
    assert event["tool_call_id"] == submitted["tool_call_id"]
    assert event["status"] == "pending_approval"


def test_full_audit_log_listing_and_secret_redaction(make_client, tmp_path: Path) -> None:
    with make_client(
        mcp_approval_catalog_path=_catalog(tmp_path), agent_api_token=SecretStr("tool-token"), **_test_app_overrides()
    ) as client:
        operator_call = client.post(
            "/api/tool-calls",
            headers={"X-authentik-username": "operator@example.com"},
            json={"server_id": "smoke", "tool_name": "echo", "client_request_id": "operator", "arguments": {"x": 1}},
        ).json()
        haku_call = client.post(
            "/api/tool-calls",
            headers={"Authorization": "Bearer tool-token"},
            json={"server_id": "smoke", "tool_name": "echo", "client_request_id": "haku", "arguments": {"x": 2}},
        ).json()
        body = client.get("/api/tool-calls").json()
    ids = {r["tool_call_id"] for r in body["tool_calls"]}
    assert {operator_call["tool_call_id"], haku_call["tool_call_id"]} <= ids
    dumped = str(body)
    assert "haku-console-grocy-sf-token" not in dumped
    assert "tool-token" not in dumped


if __name__ == "__main__":
    pytest_bazel.main()
