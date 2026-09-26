"""Operator-approved MCP tool-call API tests."""

from __future__ import annotations

import time
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock
from uuid import UUID

import pytest
import pytest_bazel
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastmcp import FastMCP
from mcp import types as mcp_types
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from starlette.websockets import WebSocketDisconnect

from haku.console.conftest import operator_id, write_config
from haku.console.database_migrate import apply_migrations
from haku.console.database_schema import Agent, CredentialBinding, McpToolCall, McpToolCallPrincipal, StaticCredential
from haku.console.identity import operator_auth
from haku.console.identity.agent import (
    # TestClient drives the app over httpx, imported inside starlette; gazelle cannot see it.
    # gazelle:include_dep @pypi//httpx
    AgentStatus,
    ClientRegistrationKind,
    CredentialBindingStatus,
    CredentialKind,
    EnrollmentPhase,
)
from haku.console.identity.authorization import fingerprint_static_token
from haku.console.identity.operator_identity import OperatorStatus
from haku.console.mcp.approval import (
    DegradedReflection,
    McpServerDispatcher,
    PostgresToolCallLedger,
    ToolCallRecord,
    _mcp_result_to_json,
)
from haku.console.mcp.execution import EXECUTION_CONTEXT_DEPENDENCY, McpExecutionContext, OperatorMcpExecutionCaller
from haku.console.mcp.reflection_cache import ReflectedCatalog
from haku.console.mcp.tool_call_service import ToolCallApplicationService, backend_auth_for_operator
from haku.console.mcp_config import (
    InProcessBackend,
    InProcessCredentialKind,
    InProcessServerRegistration,
    McpServerEntry,
    NoCredential,
    const_in_process_server,
)
from haku.console.notifications import console_events
from haku.console.tool_call_actor import AgentActor, OperatorActor, RuntimeActor
from haku.console.tool_calls import (
    AgentToolCallCaller,
    OperatorToolCallCaller,
    SubmitToolCallRequest,
    ToolCallPayloadField,
    ToolCallStatus,
)


def _build_test_mcp_server() -> FastMCP:
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


def _build_execution_context_mcp_server() -> FastMCP:
    server = FastMCP("haku-execution-context-test")

    @server.tool()
    async def caller_id(execution: McpExecutionContext = EXECUTION_CONTEXT_DEPENDENCY) -> str:
        caller = execution.caller
        return str(caller.operator_id if isinstance(caller, OperatorMcpExecutionCaller) else caller.principal.agent_id)

    return server


# The Postgres testcontainer + per-test database fixtures (`db_url`, `migrated_db_url`, `make_client`)
# live in conftest.py. `make_client` wires the app to a fresh migrated database automatically, so
# tests only pass the overrides they exercise.

# A static agent `haku` (bearer `tool-token`, acting as operator subject `op-haku`), referenced from
# a config file's `static_agents`.
_AGENT_TOKEN = "tool-token"
_STATIC_AGENTS = {
    "haku": {
        "agent_id": "30000000-0000-4000-8000-000000000001",
        "display_name": "Haku",
        "token": _AGENT_TOKEN,
        "operator_subject": "op-haku",
        "access_profile_id": "no_auto_approval",
    }
}


async def _enum_values(engine: AsyncEngine) -> dict[str, tuple[str, ...]]:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    """
                SELECT type.typname, enum.enumlabel
                FROM pg_type AS type
                JOIN pg_enum AS enum ON enum.enumtypid = type.oid
                ORDER BY type.typname, enum.enumsortorder
                """
                )
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
    return {
        "mcp": {"servers": {server["id"].replace("-", "_"): server for server in servers}},
        "static_agents": _STATIC_AGENTS,
        "auto_approval_policies": [{"id": "no_auto_approval", "type": "never"}],
        "access_profiles": [{"id": "no_auto_approval", "auto_approval_policy": "no_auto_approval"}],
        "default_access_profile_id": "no_auto_approval",
    }


def _in_process_server(server_id: str, credential: dict[str, Any]) -> dict[str, Any]:
    return {"id": server_id, "backend": {"kind": "in_process", "credential": credential}}


def _test_servers(*server_ids: str) -> dict[str, InProcessServerRegistration]:
    return {server_id: const_in_process_server(_build_test_mcp_server()) for server_id in server_ids}


@pytest.fixture
def console_app(tmp_path: Path) -> dict[str, Any]:
    """`make_client` arguments for the standard two-server console (credential-free `grocy-sf` +
    `smoke`, both the test server) most tests use."""
    servers = [_in_process_server(server_id, {"kind": "none"}) for server_id in ("grocy-sf", "smoke")]
    return {
        "config_file": write_config(tmp_path / "haku_console.yaml", _config(servers)),
        "in_process_servers": _test_servers("grocy-sf", "smoke"),
    }


@pytest.fixture
def operator_client(make_operator_client: Callable[..., Any], console_app: dict[str, Any]) -> Generator[TestClient]:
    """An operator-session client against the standard `console_app` — the setup the majority of
    operator-facing tests need. Tests with a bespoke config call `make_operator_client`
    (or `make_client`) directly instead."""
    with make_operator_client(**console_app) as client:
        yield client


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
    client: TestClient, request: SubmitToolCallRequest, *, actor: RuntimeActor | None = None
) -> dict[str, Any]:
    app = cast(FastAPI, client.app)

    async def submit() -> Any:
        return await app.state.tool_call_service.submit_and_wait(
            req=request, actor=actor if actor is not None else app.state.test_operator_actor
        )

    assert client.portal is not None
    record = client.portal.call(submit)
    return cast(dict[str, Any], record.model_dump(mode="json"))


def _withdraw(client: TestClient, tool_call_id: str, reason: str | None, *, actor: AgentActor) -> dict[str, Any]:
    app = cast(FastAPI, client.app)

    async def withdraw() -> Any:
        return await app.state.tool_call_service.withdraw(tool_call_id=tool_call_id, reason=reason, actor=actor)

    assert client.portal is not None
    record = client.portal.call(withdraw)
    return cast(dict[str, Any], record.model_dump(mode="json"))


def _static_agent_actor(client: TestClient, bearer: str) -> AgentActor:
    app = cast(FastAPI, client.app)

    async def resolve() -> AgentActor:
        sessions = cast(async_sessionmaker[AsyncSession], app.state.db_sessions)
        async with sessions() as session:
            result = await session.execute(
                select(
                    CredentialBinding.binding_id,
                    CredentialBinding.agent_id,
                    Agent.owner_operator_id,
                    Agent.access_profile_id,
                )
                .join(StaticCredential, StaticCredential.binding_id == CredentialBinding.binding_id)
                .join(Agent, Agent.agent_id == CredentialBinding.agent_id)
                .where(StaticCredential.credential_fingerprint == fingerprint_static_token(bearer))
            )
            binding_id, agent_id, operator_id, access_profile_id = result.one()
        return AgentActor(
            agent_id=agent_id, operator_id=operator_id, binding_id=binding_id, access_profile_id=access_profile_id
        )

    assert client.portal is not None
    return cast(AgentActor, client.portal.call(resolve))


def _record_execution_operator_ids(monkeypatch: pytest.MonkeyPatch) -> list[UUID]:
    operator_ids: list[UUID] = []

    async def recording_service_auth(*, server: McpServerEntry, operator_id: UUID) -> str | None:
        operator_ids.append(operator_id)
        return await backend_auth_for_operator(server=server, operator_id=operator_id)

    monkeypatch.setattr("haku.console.mcp.tool_call_service.backend_auth_for_operator", recording_service_auth)
    return operator_ids


def test_operator_mutations_reject_untrusted_origin(operator_client: TestClient) -> None:
    response = operator_client.request(
        "POST",
        "/api/tool-calls/not-a-call/decision",
        headers={"Origin": "https://haku-ui.test"},
        json={"decision": "approve"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "operator mutations require the console's exact Origin"


async def test_mcp_result_serialization_uses_mcp_wire_shape() -> None:
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
        # Always serialized (mcp_types.CallToolResult.result_type); older peers ignore it.
        "resultType": "complete",
    }


async def test_submit_mints_tool_call_id(operator_client: TestClient, migrated_db_url: str) -> None:
    first = _submit(operator_client)
    second = _submit(operator_client)
    assert first["tool_call_id"].startswith("tc_")
    assert first["caller"] == {"kind": "operator"}
    assert first["status"] == "pending_approval"
    assert "approval_id" not in first
    assert second["tool_call_id"] != first["tool_call_id"]


async def test_rest_submission_route_is_retired(operator_client: TestClient) -> None:
    response = operator_client.post(
        "/api/tool-calls",
        headers={"Authorization": "Bearer tool-token"},
        json={"server_id": "smoke", "tool_name": "echo", "arguments": {}, "wait_for_ms": 0},
    )
    assert response.status_code == 405


async def test_list_tool_calls_pages_by_cursor(operator_client: TestClient) -> None:
    """The history view's paging: `next_cursor` walks the ledger newest-first without repeating or
    skipping a row, and runs out exactly at its end."""
    submitted = [
        _submit_request(
            operator_client,
            SubmitToolCallRequest(server_id="smoke", tool_name="echo", arguments={"text": str(i)}, wait_for_ms=0),
        )["tool_call_id"]
        for i in range(5)
    ]

    walked: list[str] = []
    cursor: str | None = None
    for _page in range(3):
        params: dict[str, Any] = {"limit": 2, "newest_first": True}
        if cursor is not None:
            params["cursor"] = cursor
        body = operator_client.get("/api/tool-calls", params=params).json()
        walked.extend(row["tool_call_id"] for row in body["tool_calls"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    # Newest first, every call exactly once, and the short final page ends the walk.
    assert walked == list(reversed(submitted))
    assert cursor is None
    assert operator_client.get("/api/tool-calls", params={"cursor": "not-a-cursor"}).status_code == 422


def _agent_stock_add(amount: int = 1) -> SubmitToolCallRequest:
    return SubmitToolCallRequest(
        server_id="grocy-sf",
        tool_name="stock_add",
        rationale="box is physically present",
        arguments={"items": [{"product_id": 123, "amount": amount}]},
        wait_for_ms=0,
    )


async def test_agent_withdrawal_clears_the_operator_queue_but_keeps_the_audit_row(
    make_client: Callable[..., Any], make_operator_client: Callable[..., Any], console_app: dict[str, Any]
) -> None:
    with (
        make_client(**console_app) as client,
        make_operator_client(**console_app, operator_external_user_key="op-haku") as operator,
    ):
        agent = _static_agent_actor(client, _AGENT_TOKEN)
        pending = _submit_request(client, _agent_stock_add(), actor=agent)
        assert [c["tool_call_id"] for c in operator.get("/api/approvals/pending").json()["approvals"]] == [
            pending["tool_call_id"]
        ]

        withdrawn = _withdraw(client, pending["tool_call_id"], "superseded by a corrected call", actor=agent)

        assert operator.get("/api/approvals/pending").json()["approvals"] == []
        history = operator.get("/api/tool-calls").json()["tool_calls"]
        decision = operator.post(f"/api/tool-calls/{pending['tool_call_id']}/decision", json={"decision": "approve"})

    assert withdrawn["status"] == "withdrawn"
    assert withdrawn["withdrawal_reason"] == "superseded by a corrected call"
    # Out of the queue, still in the ledger: withdrawal is an audit fact, not a delete.
    assert [(c["tool_call_id"], c["status"]) for c in history] == [(pending["tool_call_id"], "withdrawn")]
    # Deciding a call the agent already retracted is a conflict, not a silent re-approval.
    assert decision.status_code == 409
    assert "not pending approval" in decision.json()["detail"]


async def test_websocket_receives_agent_withdrawal_invalidation(
    make_client: Callable[..., Any], make_operator_client: Callable[..., Any], console_app: dict[str, Any]
) -> None:
    with (
        make_client(**console_app) as client,
        make_operator_client(**console_app, operator_external_user_key="op-haku") as operator,
    ):
        agent = _static_agent_actor(client, _AGENT_TOKEN)
        with operator.websocket_connect("/api/events/ws", headers={"Origin": "https://haku.test"}) as ws:
            assert ws.receive_json() == {"event_type": "hello"}
            pending = _submit_request(client, _agent_stock_add(), actor=agent)
            assert ws.receive_json() == {"event_type": "tool_calls_changed", "tool_call_id": pending["tool_call_id"]}

            _withdraw(client, pending["tool_call_id"], "no longer needed", actor=agent)
            event = ws.receive_json()

    # The operator's open drawer is woken by the retraction, so the card does not linger.
    assert event == {"event_type": "tool_calls_changed", "tool_call_id": pending["tool_call_id"]}


async def _join_executions(service: ToolCallApplicationService) -> None:
    await service.join_executions()


def _drain_executions(client: TestClient) -> None:
    """Run decide()'s dispatched background executions to completion on the app loop, so a sync
    TestClient can observe the terminal row — approving now returns RUNNING and executes in the
    background."""
    portal = client.portal
    assert portal is not None
    service = cast(ToolCallApplicationService, cast(FastAPI, client.app).state.tool_call_service)
    portal.call(_join_executions, service)


def test_approval_executes_tool_and_records_terminal_result(operator_client: TestClient) -> None:
    submitted = _submit(operator_client)
    resp = operator_client.post(f"/api/tool-calls/{submitted['tool_call_id']}/decision", json={"decision": "approve"})
    assert resp.status_code == 200, resp.text
    # decide records the approval and dispatches execution: the response is RUNNING, no result yet.
    decided = resp.json()["tool_call"]
    assert decided["status"] == "running"
    assert decided["approved_at"] is not None
    assert decided["approval_policy_id"] is None
    assert decided["result"] is None

    _drain_executions(operator_client)
    finished = operator_client.get(f"/api/tool-calls/{submitted['tool_call_id']}").json()
    assert finished["status"] == "ok"
    assert finished["result"]["content"][0]["text"] == "stock_add:123:1"


async def test_approval_resolves_credentials_for_the_canonical_operator_id(
    *, make_operator_client, console_app: dict[str, Any], migrated_db_url: str, migrated_sessions, monkeypatch
) -> None:
    execution_operator_ids = _record_execution_operator_ids(monkeypatch)
    with make_operator_client(**console_app, operator_external_user_key="credential-free-sub") as client:
        submitted = _submit(client)
        approved = client.post(f"/api/tool-calls/{submitted['tool_call_id']}/decision", json={"decision": "approve"})
        # Drain before the client (and its lifespan aclose) tears down, so execution runs to completion.
        _drain_executions(client)

    assert approved.status_code == 200, approved.text
    assert approved.json()["tool_call"]["status"] == "running"
    assert execution_operator_ids == [await operator_id(migrated_sessions, "credential-free-sub")]


async def test_routing_executes_each_agent_as_its_own_operator(
    *,
    make_client,
    tmp_path: Path,
    migrated_db_url: str,
    migrated_sessions: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two static agents bound to two operators: each agent's auto-approved call on an
    operator-linked server executes with *its* operator's token, with no crosstalk."""
    # `haku` (bearer tool-token → op-haku) comes from the base config; add a second agent `ops-bot`.
    tokens = {
        await operator_id(migrated_sessions, "op-haku"): "grocy-token-haku",
        await operator_id(migrated_sessions, "op-ops"): "grocy-token-ops",
    }
    built_with: list[str] = []

    def build(token: str | None) -> FastMCP:
        # Auto-approval builds the server once without a credential to read its schema.
        if token is not None:
            built_with.append(token)
        return _build_test_mcp_server()

    async def operator_token(*, server: McpServerEntry, operator_id: UUID) -> str:
        del server
        return tokens[operator_id]

    monkeypatch.setattr("haku.console.mcp.tool_call_service.backend_auth_for_operator", operator_token)

    config = _config([_in_process_server("grocy-sf", {"kind": "none"})])
    config["auto_approval_policies"] = [
        {"id": "manual_review", "type": "never"},
        {"id": "grocy_reads", "type": "exact_tools", "tools": {"grocy-sf": ["products_list"]}},
    ]
    config["static_agents"] = {
        "haku": {**_STATIC_AGENTS["haku"], "access_profile_id": "grocy-reader"},
        "ops": {
            "agent_id": "30000000-0000-4000-8000-000000000002",
            "display_name": "Ops Bot",
            "token": "ops-token",
            "operator_subject": "op-ops",
            "access_profile_id": "grocy-reader",
        },
    }
    config["access_profiles"] = [
        {"id": "manual-review", "auto_approval_policy": "manual_review"},
        {"id": "grocy-reader", "auto_approval_policy": "grocy_reads"},
    ]
    config["default_access_profile_id"] = "manual-review"
    with make_client(
        config_file=write_config(tmp_path / "routing.yaml", config),
        in_process_servers={
            "grocy-sf": InProcessServerRegistration(builder=build, credential_kind=InProcessCredentialKind.NONE)
        },
    ) as client:
        # products_list is an unconditionally auto-approved grocy read, so each call runs immediately.
        call_ids: list[str] = []
        for bearer in ("tool-token", "ops-token"):
            record = _submit_request(
                client,
                # The served upstream's products_list mirrors grocy-sf and requires full detail.
                SubmitToolCallRequest(
                    server_id="grocy-sf", tool_name="products_list", arguments={"detail": "full"}, wait_for_ms=0
                ),
                actor=_static_agent_actor(client, bearer),
            )
            assert record["status"] == "ok", record
            call_ids.append(record["tool_call_id"])

        for bearer, expected_call_id in zip(("tool-token", "ops-token"), call_ids, strict=True):
            actor = _static_agent_actor(client, bearer)

            async def list_calls(actor: RuntimeActor = actor) -> list[ToolCallRecord]:
                return cast(list[ToolCallRecord], await client.app.state.tool_call_service.list_tool_calls(actor=actor))

            assert client.portal is not None
            listed = client.portal.call(list_calls)
            assert [call.tool_call_id for call in listed] == [expected_call_id]
            assert client.get("/api/tool-calls", headers={"Authorization": f"Bearer {bearer}"}).status_code == 401

    # haku's call executed with op-haku's token; ops-bot's with op-ops's — each routed to its operator.
    assert built_with == ["grocy-token-haku", "grocy-token-ops"]


async def test_two_operator_two_agent_http_authorization_matrix(
    make_client, make_operator_client, tmp_path: Path
) -> None:
    agent_specs = (
        ("haku", "tool-token", "op-haku"),
        ("haku-sibling", "haku-sibling-token", "op-haku"),
        ("ops", "ops-token", "op-ops"),
        ("ops-sibling", "ops-sibling-token", "op-ops"),
    )
    config = _config([_in_process_server("grocy-sf", {"kind": "none"})])
    config["static_agents"] = {
        name.replace("-", "_"): {
            "agent_id": f"30000000-0000-4000-8000-{index:012d}",
            "display_name": name.replace("-", " ").title(),
            "token": token,
            "operator_subject": operator_key,
            "access_profile_id": "no_auto_approval",
        }
        for index, (name, token, operator_key) in enumerate(agent_specs, start=10)
    }
    app = {
        "config_file": write_config(tmp_path / "two_operator_agents.yaml", config),
        "in_process_servers": _test_servers("grocy-sf"),
    }

    with (
        make_client(**app) as agents,
        make_operator_client(
            **app, operator_external_user_key="op-haku", operator_username="owner@example.com"
        ) as operator_a,
        make_operator_client(
            **app, operator_external_user_key="op-ops", operator_username="ops@example.com"
        ) as operator_b,
    ):
        call_ids: dict[str, str] = {}
        for amount, (name, bearer, _) in enumerate(agent_specs, start=1):
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

        for name, bearer, _ in agent_specs:
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

        # Decision ownership is checked before credential lookup, transition, or execution.
        for operator, foreign_call_id in ((operator_a, call_ids["ops"]), (operator_b, call_ids["haku"])):
            response = operator.post(f"/api/tool-calls/{foreign_call_id}/decision", json={"decision": "approve"})
            assert response.status_code == 404

        approved = operator_a.post(f"/api/tool-calls/{call_ids['haku']}/decision", json={"decision": "approve"})
        denied = operator_b.post(
            f"/api/tool-calls/{call_ids['ops']}/decision", json={"decision": "deny", "decision_note": "no"}
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["tool_call"]["status"] == "running"
        assert denied.status_code == 200, denied.text
        assert denied.json()["tool_call"]["status"] == "denied"


async def test_approval_denial_is_terminal_and_does_not_execute(operator_client: TestClient) -> None:
    submitted = _submit(operator_client)
    resp = operator_client.post(
        f"/api/tool-calls/{submitted['tool_call_id']}/decision", json={"decision": "deny", "decision_note": "not today"}
    )
    assert resp.status_code == 200
    tool_call = resp.json()["tool_call"]
    assert tool_call["status"] == "denied"
    assert tool_call["result"] is None
    assert tool_call["decision_note"] == "not today"
    assert tool_call["decision_operator_id"] is not None


async def test_approval_note_round_trips_on_approval(operator_client: TestClient) -> None:
    submitted = _submit(operator_client)
    resp = operator_client.post(
        f"/api/tool-calls/{submitted['tool_call_id']}/decision",
        json={"decision": "approve", "decision_note": "reviewed and approved"},
    )
    assert resp.status_code == 200
    tool_call = resp.json()["tool_call"]
    assert tool_call["status"] == "running"
    assert tool_call["decision_note"] == "reviewed and approved"
    assert tool_call["decision_operator_id"] is not None


async def test_all_v1_tool_calls_require_console_approval(operator_client: TestClient) -> None:
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


async def test_operator_tenants_cannot_read_or_decide_each_others_calls(
    make_operator_client, console_app: dict[str, Any]
) -> None:
    with (
        make_operator_client(
            **console_app, operator_external_user_key="operator-a", operator_username="a@example.com"
        ) as operator_a,
        make_operator_client(
            **console_app, operator_external_user_key="operator-b", operator_username="b@example.com"
        ) as operator_b,
    ):
        submitted = _submit(operator_a)
        call_id = submitted["tool_call_id"]

        assert [row["tool_call_id"] for row in operator_a.get("/api/tool-calls").json()["tool_calls"]] == [call_id]
        assert operator_b.get("/api/tool-calls").json()["tool_calls"] == []
        assert operator_b.get(f"/api/tool-calls/{call_id}").status_code == 404
        assert operator_b.get("/api/approvals/pending").json()["approvals"] == []

        for decision in ("deny", "approve"):
            response = operator_b.post(f"/api/tool-calls/{call_id}/decision", json={"decision": decision})
            assert response.status_code == 404

        approved = operator_a.post(f"/api/tool-calls/{call_id}/decision", json={"decision": "approve"})
        assert approved.status_code == 200, approved.text
        assert approved.json()["tool_call"]["status"] == "running"


async def test_list_newest_first_keeps_the_most_recent(operator_client: TestClient) -> None:
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


async def test_ledger_get_and_list_load_principal_projection_in_one_query(
    *,
    make_client,
    make_operator_client,
    console_app: dict[str, Any],
    migrated_db_url: str,
    migrated_sessions,
    migrated_engine: AsyncEngine,
) -> None:
    with (
        make_client(**console_app) as agent,
        make_operator_client(**console_app, operator_external_user_key="op-haku") as operator,
    ):
        agent_record = _submit_request(
            agent,
            SubmitToolCallRequest(server_id="smoke", tool_name="echo", arguments={"text": "agent"}, wait_for_ms=0),
            actor=_static_agent_actor(agent, "tool-token"),
        )
        agent_call_id = cast(str, agent_record["tool_call_id"])
        operator_call_id = cast(str, _submit(operator)["tool_call_id"])

    ledger_engine = migrated_engine
    ledger = PostgresToolCallLedger(migrated_sessions)
    actor = OperatorActor(operator_id=await operator_id(migrated_sessions, "op-haku"))
    statements: list[str] = []

    def record_tool_call_query(
        _connection: object, _cursor: object, statement: str, _parameters: object, _context: object, _executemany: bool
    ) -> None:
        if "mcp_tool_call" in statement.casefold():
            statements.append(statement)

    event.listen(ledger_engine.sync_engine, "before_cursor_execute", record_tool_call_query)
    try:
        listed = await ledger.list_tool_calls(actor=actor)
        assert len(statements) == 1, statements

        by_id = {record.tool_call_id: record for record in listed}
        assert set(by_id) == {agent_call_id, operator_call_id}
        assert by_id[agent_call_id].caller == AgentToolCallCaller(
            agent_id=UUID("30000000-0000-4000-8000-000000000001"), display_name="Haku"
        )
        assert by_id[operator_call_id].caller == OperatorToolCallCaller()

        statements.clear()
        fetched = await ledger.get(agent_call_id, actor=actor)
        assert len(statements) == 1, statements
        assert fetched == by_id[agent_call_id]

        statements.clear()
        summaries = await ledger.list_tool_calls(actor=actor, fields=())
        assert len(summaries) == 2
        assert len(statements) == 1, statements
        summary_sql = statements[0].casefold()
        assert "arguments_json" not in summary_sql
        assert "rationale" not in summary_sql
        assert "result_json" not in summary_sql

        statements.clear()
        resolved = await ledger.get(agent_call_id, actor=actor, fields=(ToolCallPayloadField.RESULT,))
        assert resolved.tool_call_id == agent_call_id
        assert len(statements) == 1, statements
        result_sql = statements[0].casefold()
        assert "result_json" in result_sql
        assert "arguments_json" not in result_sql
        assert "rationale" not in result_sql
    finally:
        event.remove(ledger_engine.sync_engine, "before_cursor_execute", record_tool_call_query)


async def test_websocket_receives_pending_approval_invalidation(operator_client: TestClient) -> None:
    with operator_client.websocket_connect("/api/events/ws", headers={"Origin": "https://haku.test"}) as ws:
        assert ws.receive_json() == {"event_type": "hello"}
        submitted = _submit(operator_client)
        event = ws.receive_json()
    assert event == {"event_type": "tool_calls_changed", "tool_call_id": submitted["tool_call_id"]}
    assert operator_client.get("/api/approvals/events").status_code == 404


async def test_two_operator_websockets_only_receive_their_interleaved_tool_calls(
    make_operator_client, console_app: dict[str, Any]
) -> None:
    with (
        make_operator_client(
            **console_app, operator_external_user_key="websocket-operator-a", operator_username="a@example.com"
        ) as operator_a,
        make_operator_client(
            **console_app, operator_external_user_key="websocket-operator-b", operator_username="b@example.com"
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


async def test_websocket_reports_an_expired_session_apart_from_a_rejected_one(
    make_operator_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Expiry gets its own close code so the shell re-authenticates instead of showing the live
    channel as merely offline and retrying a handshake that can only be refused."""
    deadline = int(time.time()) + 300
    with (
        make_operator_client(operator_session_expires_at=deadline) as client,
        client.websocket_connect("/api/events/ws", headers={"Origin": "https://haku.test"}) as ws,
    ):
        assert ws.receive_json() == {"event_type": "hello"}
        monkeypatch.setattr(operator_auth.time, "time", lambda: deadline + 1)
        # Any client frame wakes the socket's revalidation ahead of its idle tick.
        ws.send_text("ping")
        with pytest.raises(WebSocketDisconnect) as disconnected:
            ws.receive_json()

    assert disconnected.value.code == console_events.OPERATOR_SESSION_EXPIRED_CLOSE_CODE


async def test_websocket_rejects_cross_origin(make_operator_client) -> None:
    with (
        make_operator_client() as client,
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect("/api/events/ws", headers={"Origin": "https://haku-ui.test"}),
    ):
        pass
    assert exc_info.value.code == 1008


async def test_audit_log_is_tenant_scoped_and_redacts_secrets(
    make_client, make_operator_client, console_app: dict[str, Any]
) -> None:
    with (
        make_client(**console_app) as agent,
        make_operator_client(**console_app, operator_external_user_key="operator-sub") as operator,
        make_operator_client(**console_app, operator_external_user_key="op-haku") as haku_operator,
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
    assert "tool-token" not in dumped


async def test_postgres_store_runs_alembic_and_persists_typed_ledger(
    operator_client: TestClient, migrated_engine: AsyncEngine, migrated_sessions, migrated_db_url: str
) -> None:
    submitted = _submit_request(
        operator_client,
        SubmitToolCallRequest(server_id="smoke", tool_name="echo", arguments={"text": "world"}, wait_for_ms=0),
    )
    approved = operator_client.post(
        f"/api/tool-calls/{submitted['tool_call_id']}/decision", json={"decision": "approve"}
    ).json()["tool_call"]
    assert approved["status"] == "running"

    _drain_executions(operator_client)
    finished = operator_client.get(f"/api/tool-calls/{submitted['tool_call_id']}").json()
    assert finished["status"] == "ok"
    assert finished["result"]["content"][0]["text"] == "echo:world"

    engine = migrated_engine
    async with engine.connect() as conn:
        tables = {
            row["table_name"]
            for row in (
                await conn.execute(
                    text(
                        """
                            SELECT table_name
                            FROM information_schema.tables
                            WHERE table_schema = 'public'
                            """
                    )
                )
            )
            .mappings()
            .all()
        }
        columns = {
            row["column_name"]
            for row in (
                await conn.execute(
                    text(
                        """
                            SELECT column_name
                            FROM information_schema.columns
                            WHERE table_name = 'mcp_tool_calls'
                            """
                    )
                )
            )
            .mappings()
            .all()
        }
        principal_columns = {
            row["column_name"]
            for row in (
                await conn.execute(
                    text(
                        """
                            SELECT column_name
                            FROM information_schema.columns
                            WHERE table_name = 'mcp_tool_call_principals'
                            """
                    )
                )
            )
            .mappings()
            .all()
        }
    async with migrated_sessions() as session:
        persisted_call = await session.get(McpToolCall, submitted["tool_call_id"])
        persisted_principal = await session.get(McpToolCallPrincipal, submitted["tool_call_id"])
        assert persisted_call is not None
        assert persisted_principal is not None
        assert persisted_principal.operator_id == await operator_id(migrated_sessions, "operator-sub")
        assert persisted_call.server_id == "smoke"
        assert persisted_call.tool_name == "echo"
        assert persisted_call.status is ToolCallStatus.OK
        assert persisted_call.arguments_json == {"text": "world"}
        assert persisted_call.result_json is not None
        assert persisted_call.result_json["content"][0]["text"] == "echo:world"

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
    } <= tables
    assert {
        "mcp_agent_operator",
        "mcp_tool_calls_legacy_unowned",
        "mcp_tool_call_events",
        "mcp_tool_call_events_legacy_unowned",
        "mcp_operator_oauth_associations",
        "mcp_operator_oauth_flows",
        "provider_connections",
        "provider_connection_flows",
        "oauth_connection_results",
    }.isdisjoint(tables)
    assert columns == {column.name for column in McpToolCall.__table__.columns}
    assert principal_columns == {column.name for column in McpToolCallPrincipal.__table__.columns}


async def test_fresh_baseline_enum_values_match_domain_enums(db_url: str) -> None:
    apply_migrations(db_url)
    engine = create_async_engine(db_url)
    try:
        baseline_values = await _enum_values(engine)
    finally:
        await engine.dispose()

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


# --- In-process MCP servers (McpServerDispatcher in-process registration) ---
# Unit tests only: no postgres/network fixtures, exercising McpServerDispatcher
# directly (over a fresh `_build_test_mcp_server()` instance, in-memory — no HTTP)
# rather than through the FastAPI app.


def test_server_entry_allows_in_process_backend() -> None:
    McpServerEntry(
        id="google", backend=InProcessBackend(credential=NoCredential())
    )  # ok: resolved via the in-process registry at runtime, not this model


async def test_executor_dispatches_to_registered_in_process_server() -> None:
    builder = Mock(return_value=_build_test_mcp_server())
    registration = InProcessServerRegistration(builder=builder, credential_kind=InProcessCredentialKind.NONE)
    executor = McpServerDispatcher({"google": registration}, catalog_cache_ttl_seconds=0.0)
    server = McpServerEntry(id="google", backend=InProcessBackend(credential=NoCredential()))
    context = McpExecutionContext(
        caller=OperatorMcpExecutionCaller(operator_id=UUID(int=42)),
        tool_call_id="tc_test",
        approving_operator_id=None,
        approval_policy_id=None,
    )
    result = await executor.execute(
        server, "echo", {"text": "hi"}, auth_token="operator-token", execution_context=context
    )
    assert result["content"][0]["text"] == "echo:hi"
    builder.assert_called_once_with("operator-token")


async def test_executor_injects_trusted_context_into_a_stable_in_process_server() -> None:
    server_instance = _build_execution_context_mcp_server()
    registration = InProcessServerRegistration(
        builder=lambda _token: server_instance, credential_kind=InProcessCredentialKind.NONE
    )
    executor = McpServerDispatcher({"internal": registration}, catalog_cache_ttl_seconds=0.0)
    server = McpServerEntry(id="internal", backend=InProcessBackend(credential=NoCredential()))
    operator_id = UUID(int=42)

    result = await executor.execute(
        server,
        "caller_id",
        {},
        auth_token=None,
        execution_context=McpExecutionContext(
            caller=OperatorMcpExecutionCaller(operator_id=operator_id),
            tool_call_id="tc_test",
            approving_operator_id=None,
            approval_policy_id=None,
        ),
    )

    assert result["content"][0]["text"] == str(operator_id)


async def test_executor_raises_when_in_process_backend_is_not_registered() -> None:
    executor = McpServerDispatcher({}, catalog_cache_ttl_seconds=0.0)
    server = McpServerEntry(id="google", backend=InProcessBackend(credential=NoCredential()))
    with pytest.raises(RuntimeError, match="no in-process registration"):
        await executor.execute(
            server,
            "echo",
            {},
            auth_token=None,
            execution_context=McpExecutionContext(
                caller=OperatorMcpExecutionCaller(operator_id=UUID(int=42)),
                tool_call_id=None,
                approving_operator_id=None,
                approval_policy_id=None,
            ),
        )


async def test_dispatcher_reflects_in_process_server_tools() -> None:
    builder = Mock(return_value=_build_test_mcp_server())
    registration = InProcessServerRegistration(builder=builder, credential_kind=InProcessCredentialKind.NONE)
    dispatcher = McpServerDispatcher({"google": registration}, catalog_cache_ttl_seconds=0.0)
    server = McpServerEntry(id="google", backend=InProcessBackend(credential=NoCredential()))
    metadata = await dispatcher.metadata(server)
    assert isinstance(metadata, ReflectedCatalog)
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
    builder.assert_called_once_with(None)


async def test_dispatcher_reuses_a_reflected_catalog_within_the_ttl() -> None:
    builder = Mock(return_value=_build_test_mcp_server())
    registration = InProcessServerRegistration(builder=builder, credential_kind=InProcessCredentialKind.NONE)
    dispatcher = McpServerDispatcher({"google": registration}, catalog_cache_ttl_seconds=3600.0)
    server = McpServerEntry(id="google", backend=InProcessBackend(credential=NoCredential()))

    first = await dispatcher.metadata(server)
    second = await dispatcher.metadata(server)

    assert isinstance(first, ReflectedCatalog)
    assert isinstance(second, ReflectedCatalog)
    assert {tool.name for tool in second.tools} == {tool.name for tool in first.tools}
    builder.assert_called_once_with(None)


async def test_dispatcher_does_not_cache_a_degraded_reflection() -> None:
    """A server that failed must be retried on the next listing, not held degraded for the TTL."""
    dispatcher = McpServerDispatcher({}, catalog_cache_ttl_seconds=3600.0)
    server = McpServerEntry(id="google", backend=InProcessBackend(credential=NoCredential()))

    assert isinstance(await dispatcher.metadata(server), DegradedReflection)

    registered = McpServerDispatcher(
        {
            "google": InProcessServerRegistration(
                builder=Mock(return_value=_build_test_mcp_server()), credential_kind=InProcessCredentialKind.NONE
            )
        },
        catalog_cache_ttl_seconds=3600.0,
    )
    assert isinstance(await registered.metadata(server), ReflectedCatalog)


async def test_dispatcher_degrades_when_in_process_backend_is_not_registered() -> None:
    dispatcher = McpServerDispatcher({}, catalog_cache_ttl_seconds=0.0)
    server = McpServerEntry(id="google", backend=InProcessBackend(credential=NoCredential()))
    metadata = await dispatcher.metadata(server)
    assert isinstance(metadata, DegradedReflection)


if __name__ == "__main__":
    pytest_bazel.main()
