"""Tests for haku-console's own MCP server (the connected-server tool proxy)."""

from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import re
import secrets
from collections.abc import AsyncGenerator, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import httpx
import pytest
import pytest_bazel
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from jsonschema import Draft202012Validator
from mcp.types import Icon, Tool, ToolAnnotations
from pydantic import SecretStr, ValidationError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from haku.console import mcp_server as mcp_server_module
from haku.console.app import create_app
from haku.console.config import McpOAuthConfig, OperatorOidcConfig
from haku.console.conftest import console_settings, operator_session_cookie, write_config
from haku.console.mcp_approval import DegradedReflection
from haku.console.mcp_config import ConsoleConfigFile, const_in_process_server
from haku.console.mcp_operator_oauth import (
    McpOperatorAuthConnected,
    McpOperatorAuthStatus,
    McpOperatorAuthStatusResponse,
    McpOperatorAuthUnconnected,
)
from haku.console.operator_identity import VerifiedExternalIdentity
from haku.console.provider_connection import ProviderConnected, ProviderConnectionStatusResponse
from haku.console.tool_call_actor import AgentActor, OperatorActor, ToolCallActor
from haku.console.tool_call_service import ToolCallApplicationService, ToolCallNotFoundError
from haku.console.tool_calls import (
    MCP_TOOL_CALL_META_KEY,
    MCP_TOOL_META_KEY,
    SubmitToolCallRequest,
    ToolCallRecord,
    ToolCallStatus,
)
from haku.console.tools import gmail as gmail_tools, google_calendar as calendar_tools
from haku.console.tools.google_calendar_client import CalendarEvent
from mcp_infra.persistence import PostgresPersistence
from util.net import pick_free_port
from util.testing.asgi import serve_app_sync, serve_fastmcp
from util.testing.mock_oidc import build_mock_oidc_app, generate_rsa_keypair


def _remote_backend(url: str, auth: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "remote_mcp", "url": url, "auth": auth}


def _in_process_backend(credential: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "in_process", "credential": credential}


def _dynamic_remote_oauth() -> dict[str, Any]:
    return {"kind": "remote_server_oauth", "client_registration": {"kind": "dynamic", "client_name": "Haku Console"}}


# The `/mcp` static bearer used across these tests, and the static-agent config that binds it to the
# `haku` agent id (which acts as operator subject "42"). Env-referenced, like the deploy.
_AGENT_TOKEN = "agent-token"
_AGENT_TOKEN_ENV = "HAKU_CONSOLE_TEST_AGENT_TOKEN"
_AGENT_OPERATOR_ENV = "HAKU_CONSOLE_TEST_AGENT_OPERATOR"
_SIBLING_AGENT_TOKEN = "sibling-agent-token"
_SIBLING_AGENT_TOKEN_ENV = "HAKU_CONSOLE_TEST_SIBLING_AGENT_TOKEN"
_SIBLING_AGENT_OPERATOR_ENV = "HAKU_CONSOLE_TEST_SIBLING_AGENT_OPERATOR"
_OTHER_AGENT_TOKEN = "other-agent-token"
_OTHER_AGENT_TOKEN_ENV = "HAKU_CONSOLE_TEST_OTHER_AGENT_TOKEN"
_OTHER_AGENT_OPERATOR_ENV = "HAKU_CONSOLE_TEST_OTHER_AGENT_OPERATOR"
_OTHER_SIBLING_AGENT_TOKEN = "other-sibling-agent-token"
_OTHER_SIBLING_AGENT_TOKEN_ENV = "HAKU_CONSOLE_TEST_OTHER_SIBLING_AGENT_TOKEN"
_OTHER_SIBLING_AGENT_OPERATOR_ENV = "HAKU_CONSOLE_TEST_OTHER_SIBLING_AGENT_OPERATOR"
_STATIC_AGENTS = [
    {
        "agent_id": "40000000-0000-4000-8000-000000000001",
        "display_name": "Haku",
        "token_env_var": _AGENT_TOKEN_ENV,
        "operator_subject_env": _AGENT_OPERATOR_ENV,
    },
    {
        "agent_id": "40000000-0000-4000-8000-000000000002",
        "display_name": "Sibling",
        "token_env_var": _SIBLING_AGENT_TOKEN_ENV,
        "operator_subject_env": _SIBLING_AGENT_OPERATOR_ENV,
    },
    {
        "agent_id": "40000000-0000-4000-8000-000000000003",
        "display_name": "Other",
        "token_env_var": _OTHER_AGENT_TOKEN_ENV,
        "operator_subject_env": _OTHER_AGENT_OPERATOR_ENV,
    },
    {
        "agent_id": "40000000-0000-4000-8000-000000000004",
        "display_name": "Other Sibling",
        "token_env_var": _OTHER_SIBLING_AGENT_TOKEN_ENV,
        "operator_subject_env": _OTHER_SIBLING_AGENT_OPERATOR_ENV,
    },
]


def _assert_valid_json_schema(schema: dict[str, Any] | None) -> None:
    """`schema` must be a structurally valid JSON Schema document with every `$ref` resolvable.

    `check_schema` alone validates shape (types, keyword usage) but not reference resolution, so
    it would not have caught the bug this guards against: combining schemas (e.g. an envelope's
    own `$defs`) can leave a nested `$defs` block whose sibling `$ref`s are root-relative
    JSON pointers that no longer resolve once the block isn't at the document root. Walking every
    `$ref` through a `referencing` registry rooted at the document reproduces exactly the failure
    an MCP client hits when it tries to validate a real result against the schema.
    """
    if schema is None:
        return
    Draft202012Validator.check_schema(schema)
    resource = Resource.from_contents(schema, default_specification=DRAFT202012)
    resolver = Registry().with_resource("", resource).resolver()

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                resolver.lookup(ref)
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(schema)


def test_direct_result_preserves_upstream_error_and_metadata() -> None:
    result = mcp_server_module._direct_to_result(
        {
            "content": [{"type": "text", "text": "upstream rejected the request"}],
            "structuredContent": {"reason": "rejected"},
            "isError": True,
            "_meta": {"upstream": "kept"},
        }
    )

    assert result.is_error is True
    assert result.structured_content == {"reason": "rejected"}
    assert result.meta == {"upstream": "kept"}


@pytest.fixture(autouse=True)
def _static_agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_AGENT_TOKEN_ENV, _AGENT_TOKEN)
    monkeypatch.setenv(_AGENT_OPERATOR_ENV, "42")
    monkeypatch.setenv(_SIBLING_AGENT_TOKEN_ENV, _SIBLING_AGENT_TOKEN)
    monkeypatch.setenv(_SIBLING_AGENT_OPERATOR_ENV, "42")
    monkeypatch.setenv(_OTHER_AGENT_TOKEN_ENV, _OTHER_AGENT_TOKEN)
    monkeypatch.setenv(_OTHER_AGENT_OPERATOR_ENV, "99")
    monkeypatch.setenv(_OTHER_SIBLING_AGENT_TOKEN_ENV, _OTHER_SIBLING_AGENT_TOKEN)
    monkeypatch.setenv(_OTHER_SIBLING_AGENT_OPERATOR_ENV, "99")


@dataclass
class _Harness:
    base: str  # base URL of the served console MCP; open `Client(f"{base}/mcp", auth=_AGENT_TOKEN)`
    tool_calls: ToolCallApplicationService
    operator_id: UUID
    other_operator_id: UUID


@pytest.fixture
async def harness(migrated_db_url: str, tmp_path: Path) -> AsyncGenerator[_Harness]:
    gmail_client = Mock()
    # labels_list is a generated read: it dispatches through gmail_client.service, returning raw Gmail JSON.
    gmail_client.service.users.return_value.labels.return_value.list.return_value.execute.return_value = {
        "labels": [{"id": "Label_1", "name": "haku/triaged", "type": "user"}]
    }
    calendar_client = Mock()
    calendar_client.get_event.return_value = CalendarEvent(
        event_id="series1", summary="Standup", recurrence=["RRULE:FREQ=WEEKLY"]
    )
    config_file = write_config(
        tmp_path / "console.yaml",
        {
            "static_agents": _STATIC_AGENTS,
            "mcp": {
                "servers": [
                    {"id": "gmail", "backend": _in_process_backend({"kind": "none"})},
                    {"id": "google_calendar", "backend": _in_process_backend({"kind": "none"})},
                ]
            },
        },
    )
    settings = console_settings(migrated_db_url, config_file=config_file, ui_base_url="https://haku.test")
    in_process = {
        gmail_tools.GMAIL_SERVER_ID: const_in_process_server(gmail_tools.build_mcp(gmail_client)),
        calendar_tools.GOOGLE_CALENDAR_SERVER_ID: const_in_process_server(calendar_tools.build_mcp(calendar_client)),
    }
    app = create_app(settings, gmail_client=gmail_client, in_process_servers=in_process)
    operator_id = app.state.operator_identity_store.resolve_configured_external_user_key("42")
    other_operator_id = app.state.operator_identity_store.resolve_configured_external_user_key("99")
    with serve_app_sync(app) as base:
        yield _Harness(
            base=base,
            tool_calls=app.state.tool_call_service,
            operator_id=operator_id,
            other_operator_id=other_operator_id,
        )


@pytest.fixture
async def agent_client(harness: _Harness) -> AsyncGenerator[Client]:
    """The common case: a client connected and authenticated as the sole static `Haku` agent.
    Tests that need a different or additional token (comparing multiple agents) open their own
    `Client(...)` instead of using this fixture."""
    async with Client(f"{harness.base}/mcp", auth=_AGENT_TOKEN) as client:
        yield client


async def test_tool_surface_splits_pass_through_and_request(agent_client: Client) -> None:
    tools = {t.name: t for t in await agent_client.list_tools()}
    daemon_status = await agent_client.call_tool("list_node_daemons", {})

    # Gmail reads are transparent pass-through: server-prefixed name, no envelope nesting.
    assert "gmail__labels_list" in tools
    assert "input" not in tools["gmail__labels_list"].inputSchema.get("properties", {})
    gmail_read_meta = tools["gmail__labels_list"].meta
    assert gmail_read_meta is not None
    assert gmail_read_meta[MCP_TOOL_META_KEY] == {
        "server_id": "gmail",
        "upstream_tool_name": "labels_list",
        "approval_mode": "passthrough",
    }
    # Read tools advertise read-only; the write tool stays unannotated (defaults describe mutating).
    gmail_read_ann = tools["gmail__labels_list"].annotations
    assert gmail_read_ann is not None
    assert gmail_read_ann.readOnlyHint is True
    assert tools["gmail__drafts_create"].annotations is None
    # No upstream title to prefix here, so the proxy sets none (clients fall back to the
    # already server-prefixed name).
    assert tools["gmail__labels_list"].title is None
    # Gmail writes are approval-request tools with the envelope.
    assert "gmail__drafts_create" in tools
    envelope = tools["gmail__drafts_create"].inputSchema
    assert set(envelope["required"]) == {"input", "rationale"}
    assert set(envelope["properties"]) == {"input", "title", "rationale", "wait_for_approval_ms"}
    gmail_write_meta = tools["gmail__drafts_create"].meta
    assert gmail_write_meta is not None
    assert gmail_write_meta[MCP_TOOL_META_KEY] == {
        "server_id": "gmail",
        "upstream_tool_name": "drafts_create",
        "approval_mode": "approval_required",
    }
    # The read tools are present.
    assert {
        "get_mcp_server_status",
        "get_tool_call",
        "list_node_daemons",
        "list_tool_calls",
        "list_mcp_servers",
    } <= tools.keys()
    assert "actor" not in tools["get_tool_call"].inputSchema.get("properties", {})
    assert "actor" not in tools["list_tool_calls"].inputSchema.get("properties", {})
    assert "actor" not in tools["list_mcp_servers"].inputSchema.get("properties", {})
    assert "actor" not in tools["get_mcp_server_status"].inputSchema.get("properties", {})
    assert tools["get_mcp_server_status"].inputSchema["properties"]["include_tool_schemas"]["default"] is False
    assert daemon_status.structured_content == {"daemons": []}
    # Native read tools advertise read-only + closed-world so clients (claude.ai) treat them as
    # passive reads and skip approvals. See mcp_infra/docs/tool_annotations.md.
    for meta_tool in (
        "get_mcp_server_status",
        "get_tool_call",
        "list_node_daemons",
        "list_tool_calls",
        "list_mcp_servers",
    ):
        ann = tools[meta_tool].annotations
        assert ann is not None
        assert ann.readOnlyHint is True
        assert ann.openWorldHint is False
    # The promise preamble is in the envelope tool's description.
    gmail_write_description = tools["gmail__drafts_create"].description
    assert gmail_write_description is not None
    assert "operator-approval queue" in gmail_write_description
    # Calendar reads are transparent; creation is the approval-gated request tool. The server
    # prefix supplies "calendar", so no tool repeats it in the local name.
    assert "google_calendar__get_event" in tools
    assert "input" not in tools["google_calendar__get_event"].inputSchema.get("properties", {})
    cal_read_ann = tools["google_calendar__get_event"].annotations
    assert cal_read_ann is not None
    assert cal_read_ann.readOnlyHint is True
    assert "google_calendar__create_event" in tools
    assert "google_calendar__create_calendar_event" not in tools
    # Every advertised tool's schemas — passthrough and envelope input schemas, and any declared
    # output schema — must be valid, fully-resolvable JSON Schema, not just superficially shaped.
    for tool in tools.values():
        _assert_valid_json_schema(tool.inputSchema)
        _assert_valid_json_schema(tool.outputSchema)


async def test_mcp_transport_is_stateless_across_replicas(harness: _Harness) -> None:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {_AGENT_TOKEN}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(base_url=harness.base, headers=headers) as client:
        initialized = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "replica-test", "version": "1"},
                },
            },
        )
        assert initialized.status_code == 200, initialized.text
        assert "mcp-session-id" not in initialized.headers

        # A stateful, process-local transport rejects this as an unknown session. Stateless HTTP
        # deliberately ignores the header, so the request works on any replica behind the Service.
        listed = await client.post(
            "/mcp",
            headers={"Mcp-Session-Id": "session-created-by-another-replica"},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert listed.status_code == 200, listed.text
        assert "gmail__labels_list" in listed.text


async def test_pass_through_read_auto_approves_and_returns_result(harness: _Harness, agent_client: Client) -> None:
    result = await agent_client.call_tool("gmail__labels_list", {})

    assert result.structured_content is not None
    assert result.structured_content["labels"][0]["name"] == "haku/triaged"
    assert result.meta is not None
    calls = harness.tool_calls.list_tool_calls(actor=OperatorActor(operator_id=harness.operator_id))
    assert len(calls) == 1
    assert calls[0].status == ToolCallStatus.OK
    assert calls[0].tool_name == "labels_list"
    # The pass-through call is audited as the static agent that presented the bearer.
    assert calls[0].caller.kind == "agent"
    assert calls[0].caller.display_name == "Haku"
    assert result.meta[MCP_TOOL_CALL_META_KEY] == {"tool_call_id": calls[0].tool_call_id}


async def test_list_tool_calls_tool_filters_by_auto_approved(agent_client: Client) -> None:
    await agent_client.call_tool("gmail__labels_list", {})
    promise = await agent_client.call_tool(
        "gmail__drafts_create",
        {"input": {"to": ["a@b.test"], "subject": "s", "body": "b"}, "rationale": "test", "wait_for_approval_ms": 0},
    )
    assert promise.structured_content is not None
    manual_id = promise.structured_content["tool_call_id"]

    hidden = await agent_client.call_tool("list_tool_calls", {"auto_approved": False})
    shown_only = await agent_client.call_tool("list_tool_calls", {"auto_approved": True})
    unfiltered = await agent_client.call_tool("list_tool_calls", {})

    def call_ids(result: Any) -> list[str]:
        assert result.structured_content is not None
        return [view["call"]["tool_call_id"] for view in result.structured_content["result"]]

    hidden_ids = call_ids(hidden)
    shown_ids = call_ids(shown_only)
    assert hidden_ids == [manual_id]
    assert len(shown_ids) == 1
    assert shown_ids != [manual_id]
    assert set(call_ids(unfiltered)) == {manual_id, *shown_ids}


async def test_schema_invalid_call_fails_fast_and_never_queues(harness: _Harness, agent_client: Client) -> None:
    """A schema-invalid call on an owned in-process server is born-denied: the caller gets the
    validation error immediately and nothing enters the approval queue (operator, 2026-07-16)."""
    with pytest.raises(ToolError, match="single_events"):
        await agent_client.call_tool("google_calendar__list_events", {"single_events": True})

    operator = OperatorActor(operator_id=harness.operator_id)
    calls = harness.tool_calls.list_tool_calls(actor=operator)
    assert len(calls) == 1
    assert calls[0].status == ToolCallStatus.DENIED
    assert calls[0].denial_reason is not None
    assert "single_events" in calls[0].denial_reason
    assert calls[0].auto_approval_evaluation == "denied: arguments failed the registered tool schema"
    assert harness.tool_calls.pending_approvals(actor=operator) == []


async def test_calendar_read_is_transparent_and_audited(harness: _Harness, agent_client: Client) -> None:
    result = await agent_client.call_tool("google_calendar__get_event", {"event_id": "series1"})

    assert result.structured_content is not None
    assert result.structured_content["event_id"] == "series1"
    calls = harness.tool_calls.list_tool_calls(actor=OperatorActor(operator_id=harness.operator_id))
    assert len(calls) == 1
    assert calls[0].server_id == "google_calendar"
    assert calls[0].tool_name == "get_event"


async def test_request_tool_returns_promise_with_deep_link(agent_client: Client) -> None:
    result = await agent_client.call_tool(
        "gmail__drafts_create",
        {"input": {"to": ["a@b.test"], "subject": "s", "body": "b"}, "rationale": "test", "wait_for_approval_ms": 0},
    )
    promise = result.structured_content
    assert promise is not None
    assert promise["status"] == ToolCallStatus.PENDING_APPROVAL
    tool_call_id = promise["tool_call_id"]
    assert result.meta is not None
    assert result.meta[MCP_TOOL_CALL_META_KEY] == {"tool_call_id": tool_call_id}
    assert tool_call_id.startswith("tc_")
    assert promise["url"] == f"https://haku.test/tool-calls/{tool_call_id}"

    got = await agent_client.call_tool("get_tool_call", {"tool_call_id": tool_call_id})
    view = got.structured_content
    assert view is not None
    assert view["call"]["status"] == ToolCallStatus.PENDING_APPROVAL
    assert view["call"]["tool_name"] == "drafts_create"
    assert view["url"] == f"https://haku.test/tool-calls/{tool_call_id}"


async def test_request_tool_preserves_explicit_zero_wait(
    harness: _Harness, agent_client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    submitted_waits: list[int] = []

    async def capture_request(*, req: SubmitToolCallRequest, actor: ToolCallActor) -> ToolCallRecord:
        assert actor.operator_id == harness.operator_id
        submitted_waits.append(req.wait_for_ms)
        raise ToolCallNotFoundError("captured request")

    monkeypatch.setattr(harness.tool_calls, "submit_and_wait", capture_request)
    with pytest.raises(ToolError, match="captured request"):
        await agent_client.call_tool(
            "gmail__drafts_create",
            {
                "input": {"to": ["a@b.test"], "subject": "s", "body": "b"},
                "rationale": "test",
                "wait_for_approval_ms": 0,
            },
        )

    assert submitted_waits == [0]


async def test_get_tool_call_missing_raises(agent_client: Client) -> None:
    with pytest.raises(ToolError, match="not found"):
        await agent_client.call_tool("get_tool_call", {"tool_call_id": "tc_does_not_exist"})


async def test_two_operator_two_agent_mcp_read_matrix(harness: _Harness) -> None:
    async def submit_draft(token: str, subject: str) -> str:
        async with Client(f"{harness.base}/mcp", auth=token) as client:
            result = await client.call_tool(
                "gmail__drafts_create",
                {
                    "input": {"to": ["a@b.test"], "subject": subject, "body": "body"},
                    "rationale": "test agent read isolation",
                    "wait_for_approval_ms": 0,
                },
            )
        assert result.structured_content is not None
        return str(result.structured_content["tool_call_id"])

    agents = (
        (_AGENT_TOKEN, "haku"),
        (_SIBLING_AGENT_TOKEN, "sibling"),
        (_OTHER_AGENT_TOKEN, "other"),
        (_OTHER_SIBLING_AGENT_TOKEN, "other-sibling"),
    )
    call_ids = [await submit_draft(token, name) for token, name in agents]

    for (token, _), own_call_id in zip(agents, call_ids, strict=True):
        async with Client(f"{harness.base}/mcp", auth=token) as client:
            listed = await client.call_tool("list_tool_calls", {})
            assert listed.structured_content is not None
            assert [view["call"]["tool_call_id"] for view in listed.structured_content["result"]] == [own_call_id]
            own = await client.call_tool("get_tool_call", {"tool_call_id": own_call_id})
            assert own.structured_content is not None
            assert own.structured_content["call"]["tool_call_id"] == own_call_id
            for foreign_call_id in set(call_ids) - {own_call_id}:
                with pytest.raises(ToolError, match="not found"):
                    await client.call_tool("get_tool_call", {"tool_call_id": foreign_call_id})

    operator_calls = harness.tool_calls.list_tool_calls(actor=OperatorActor(operator_id=harness.operator_id))
    other_operator_calls = harness.tool_calls.list_tool_calls(
        actor=OperatorActor(operator_id=harness.other_operator_id)
    )
    assert [call.tool_call_id for call in operator_calls] == call_ids[:2]
    assert [call.tool_call_id for call in other_operator_calls] == call_ids[2:]


# ── End-to-end: real upstream MCP server + console served over HTTP + real Postgres ──────────


@contextmanager
def _serve_upstream() -> Generator[str]:
    """A real upstream MCP server process stand-in (echo tool) served over streamable HTTP."""
    upstream: FastMCP = FastMCP("standin")

    @upstream.tool(
        # `title` is the spec-preferred display name and should win over the legacy
        # `annotations.title` below — proves the proxy honors that precedence too.
        title="Echo text",
        icons=[Icon(src="https://example.invalid/echo.png")],
        output_schema={"type": "object", "properties": {"echoed": {"type": "string"}}, "required": ["echoed"]},
        annotations=ToolAnnotations(title="legacy annotations title", readOnlyHint=True, openWorldHint=False),
    )
    async def echo(text: str) -> dict[str, str]:
        """Echo a string back."""
        return {"echoed": f"echo:{text}"}

    with serve_fastmcp(upstream) as url:
        yield url


def _console_config(tmp_path: Path, upstream_url: str) -> Path:
    return write_config(
        tmp_path / "console.yaml",
        {
            "static_agents": _STATIC_AGENTS,
            "mcp": {"servers": [{"id": "standin", "backend": _remote_backend(upstream_url, {"kind": "none"})}]},
        },
    )


async def test_e2e_request_approve_execute_over_http(migrated_db_url: str, tmp_path: Path) -> None:
    with _serve_upstream() as upstream_url:
        console_port = pick_free_port()
        settings = console_settings(
            migrated_db_url,
            config_file=_console_config(tmp_path, upstream_url),
            ui_base_url="https://haku.test",
            public_base_url=f"http://127.0.0.1:{console_port}",
        )
        app = create_app(settings)
        operator_identity = app.state.operator_identity_store.resolve_verified_identity(
            VerifiedExternalIdentity(issuer=settings.operator_oidc.issuer, subject="42")
        )
        with serve_app_sync(app, port=console_port) as base:
            async with httpx.AsyncClient() as anon:
                # No bearer -> unauthorized at the exact canonical resource URL.
                unauth = await anon.post(
                    f"{base}/mcp",
                    headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                )
            assert unauth.status_code == 401

            # The agent (bearer) sees the upstream tool behind the approval envelope and gets a promise.
            async with Client(f"{base}/mcp", auth=_AGENT_TOKEN) as client:
                tools = {t.name: t for t in await client.list_tools()}
                assert "standin__echo" in tools
                # Upstream self-declared annotations propagate through the proxy reflection.
                ann = tools["standin__echo"].annotations
                assert ann is not None
                assert ann.readOnlyHint is True
                assert ann.openWorldHint is False
                # The human-readable title is re-prefixed with the server id, just like the name,
                # and the spec-preferred `title` field wins over the legacy `annotations.title`.
                assert tools["standin__echo"].title == "standin: Echo text"
                # Icons are opaque display assets — propagate unchanged, no server prefix.
                assert tools["standin__echo"].icons == [Icon(src="https://example.invalid/echo.png")]
                # Proxied tools declare no output schema: the result-or-promise union can't be
                # modeled as a conformant outputSchema (claude.ai requires type == "object";
                # anthropics/claude-ai-mcp#400), and outputSchema is optional. The promise
                # behavior is described in the tool description, not its output schema.
                assert tools["standin__echo"].outputSchema is None
                _assert_valid_json_schema(tools["standin__echo"].inputSchema)
                result = await client.call_tool(
                    "standin__echo", {"input": {"text": "hi"}, "rationale": "e2e", "wait_for_approval_ms": 0}
                )
                assert result.structured_content is not None
                assert result.structured_content["status"] == ToolCallStatus.PENDING_APPROVAL
                tool_call_id = result.structured_content["tool_call_id"]

            # The operator approves via the exact-Origin-gated decision endpoint -> the real upstream runs.
            async with httpx.AsyncClient(
                base_url=base,
                cookies={
                    "session": operator_session_cookie(
                        operator_id=str(operator_identity.operator_id),
                        identity_id=str(operator_identity.identity_id),
                        username="operator",
                    )
                },
            ) as operator:
                direct_request = {
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "tools/call",
                    "params": {
                        "name": "standin__echo",
                        "arguments": {"input": {"text": "operator"}, "rationale": "render an operator preview"},
                    },
                }
                mcp_headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}

                initialized = await operator.post(
                    "/mcp",
                    headers={**mcp_headers, "Origin": base},
                    json={
                        "jsonrpc": "2.0",
                        "id": 9,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {"name": "operator-test", "version": "1"},
                        },
                    },
                )
                assert initialized.status_code == 200, (initialized.text, dict(initialized.headers))

                direct = await operator.post("/mcp", headers={**mcp_headers, "Origin": base}, json=direct_request)
                assert direct.status_code == 200, (direct.text, dict(direct.headers))
                assert "echo:operator" in direct.text

                calls = app.state.tool_call_service.list_tool_calls(
                    actor=OperatorActor(operator_id=operator_identity.operator_id)
                )
                assert [call.tool_call_id for call in calls] == [tool_call_id]

                missing_origin = await operator.post("/mcp", headers=mcp_headers, json=direct_request)
                assert missing_origin.status_code == 403
                assert missing_origin.json()["error"] == "operator_session_rejected"

                # An explicit bearer always owns admission. A rejected bearer cannot fall back to
                # the otherwise valid browser session and become an Operator call.
                invalid_bearer = await operator.post(
                    "/mcp",
                    headers={**mcp_headers, "Authorization": "Bearer rejected", "Origin": base},
                    json=direct_request,
                )
                assert invalid_bearer.status_code == 401

                decided = await operator.post(
                    f"/api/tool-calls/{tool_call_id}/decision", headers={"Origin": base}, json={"decision": "approve"}
                )
            assert decided.status_code == 200, decided.text
            # decide records the approval and dispatches execution in the background — it returns RUNNING.
            assert decided.json()["tool_call"]["status"] == "running"

            # The agent resolves its promise; execution runs in the background on the server loop, so
            # poll get_tool_call until it terminalizes, then check the real upstream result.
            terminal = {ToolCallStatus.OK, ToolCallStatus.ERROR, ToolCallStatus.DENIED}
            async with Client(f"{base}/mcp", auth=_AGENT_TOKEN) as client:
                for _ in range(100):
                    got = await client.call_tool("get_tool_call", {"tool_call_id": tool_call_id})
                    assert got.structured_content is not None
                    if got.structured_content["call"]["status"] in terminal:
                        break
                    await asyncio.sleep(0.02)
            assert got.structured_content["call"]["status"] == ToolCallStatus.OK
            assert "echo:hi" in str(got.structured_content["call"]["result"])


async def test_tool_surface_tracks_each_operators_connected_servers(
    migrated_db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _serve_upstream() as upstream_url:
        config_file = write_config(
            tmp_path / "operator-tools.yaml",
            {
                "static_agents": _STATIC_AGENTS,
                "mcp": {
                    "servers": [{"id": "standin", "backend": _remote_backend(upstream_url, _dynamic_remote_oauth())}]
                },
            },
        )
        app = create_app(console_settings(migrated_db_url, config_file=config_file))
        operator_id = app.state.operator_identity_store.resolve_configured_external_user_key("42")
        other_operator_id = app.state.operator_identity_store.resolve_configured_external_user_key("99")
        connected = {operator_id}

        async def access_token_for(*, server: object, operator_id: UUID) -> str | None:
            return "connected-token" if operator_id in connected else None

        monkeypatch.setattr(app.state.mcp_operator_oauth_store, "access_token_for", access_token_for)

        with serve_app_sync(app) as base:
            async with Client(f"{base}/mcp", auth=_AGENT_TOKEN) as client:
                assert "standin__echo" in {tool.name for tool in await client.list_tools()}
                status = await client.call_tool("get_mcp_server_status", {"server_id": "standin"})
                assert status.structured_content is not None
                assert status.structured_content["server"]["state"]["status"] == "alive"
                assert status.structured_content["server"]["server_id"] == "standin"
            async with Client(f"{base}/mcp", auth=_OTHER_AGENT_TOKEN) as client:
                assert "standin__echo" not in {tool.name for tool in await client.list_tools()}
                status = await client.call_tool("get_mcp_server_status", {"server_id": "standin"})
                assert status.structured_content is not None
                assert status.structured_content["server"]["state"]["status"] == "degraded"
                assert status.structured_content["server"]["state"]["failure_stage"] == "credential_resolution"
                degraded_reason = status.structured_content["server"]["state"]["degraded_reason"]
                assert "Connect your standin MCP account" in degraded_reason
                with pytest.raises(ToolError, match="MCP server 'standin' is unavailable"):
                    await client.call_tool("standin__echo", {"input": {"text": "no"}, "rationale": "test"})

            connected.clear()
            connected.add(other_operator_id)

            async with Client(f"{base}/mcp", auth=_AGENT_TOKEN) as client:
                assert "standin__echo" not in {tool.name for tool in await client.list_tools()}
            async with Client(f"{base}/mcp", auth=_OTHER_AGENT_TOKEN) as client:
                assert "standin__echo" in {tool.name for tool in await client.list_tools()}


async def test_list_mcp_servers_passively_reports_persisted_connection_state(
    migrated_db_url: str, tmp_path: Path
) -> None:
    config_file = write_config(
        tmp_path / "connection-status.yaml",
        {
            "static_agents": _STATIC_AGENTS,
            "operator_connection_providers": {
                "google": {
                    "kind": "google",
                    "client_id_env_var": "GOOGLE_CLIENT_ID",
                    "client_secret_env_var": "GOOGLE_CLIENT_SECRET",
                }
            },
            "operator_connections": {
                "google_workspace": {"display_name": "Google Workspace", "provider": "google", "scopes": ["scope"]}
            },
            "mcp": {
                "servers": [
                    {
                        "id": "expired-remote",
                        "backend": _remote_backend(
                            "https://must-not-be-contacted.invalid/mcp", _dynamic_remote_oauth()
                        ),
                    },
                    {
                        "id": "unconnected-remote",
                        "backend": _remote_backend(
                            "https://also-must-not-be-contacted.invalid/mcp", _dynamic_remote_oauth()
                        ),
                    },
                    {
                        "id": "gmail",
                        "backend": _in_process_backend(
                            {"kind": "operator_connection", "connection": "google_workspace"}
                        ),
                    },
                    {"id": "routine", "backend": _in_process_backend({"kind": "none"})},
                    {
                        "id": "static-remote",
                        "backend": _remote_backend(
                            "https://static.invalid/mcp",
                            {"kind": "static_bearer", "bearer_token_secret": "STATIC_REMOTE_TOKEN"},
                        ),
                    },
                ]
            },
        },
    )
    settings = console_settings(migrated_db_url, config_file=config_file)
    expires_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=1)
    connected_at = expires_at - datetime.timedelta(days=1)
    oauth_statuses = Mock(
        return_value=McpOperatorAuthStatusResponse(
            associations=[
                McpOperatorAuthStatus(
                    server_id="expired-remote",
                    username="operator",
                    state=McpOperatorAuthConnected(
                        connected_at=connected_at, token_expires_at=expires_at, scope="openid offline_access"
                    ),
                ),
                McpOperatorAuthStatus(
                    server_id="unconnected-remote", username="operator", state=McpOperatorAuthUnconnected()
                ),
            ]
        )
    )
    provider_statuses = Mock(
        return_value=ProviderConnectionStatusResponse(
            connections=[
                ProviderConnected(
                    connection="google_workspace",
                    display_name="Google Workspace",
                    provider="google",
                    connected_at=connected_at,
                    token_expires_at=None,
                    scope="openid email",
                )
            ]
        )
    )
    oauth_store = Mock(list_statuses=oauth_statuses)
    provider_store = Mock(list_statuses=provider_statuses)
    refresh_remote = AsyncMock(side_effect=AssertionError("list_mcp_servers must not refresh remote OAuth"))
    refresh_provider = AsyncMock(side_effect=AssertionError("list_mcp_servers must not refresh provider OAuth"))
    fetch_metadata = AsyncMock(side_effect=AssertionError("list_mcp_servers must not contact an MCP server"))
    oauth_store.access_token_for = refresh_remote
    provider_store.access_token_for = refresh_provider
    metadata_provider = Mock(metadata=fetch_metadata)
    context = mcp_server_module.ConsoleMcpContext(
        settings=settings,
        tool_calls=Mock(),
        oauth_store=oauth_store,
        provider_store=provider_store,
        metadata_provider=metadata_provider,
    )
    actor = AgentActor(agent_id=UUID(int=1), operator_id=UUID(int=2), binding_id=UUID(int=3))

    response = mcp_server_module._passive_server_connection_statuses(context, actor)

    statuses = {server.server_id: server for server in response.servers}
    assert statuses["expired-remote"].model_dump(mode="json") == {
        "server_id": "expired-remote",
        "backend": {
            "kind": "remote_mcp",
            "url": "https://must-not-be-contacted.invalid/mcp",
            "auth": {
                "kind": "remote_server_oauth",
                "client_registration": {"kind": "dynamic", "client_name": "Haku Console"},
                "scopes": None,
            },
        },
        "connection": {
            "server_id": "expired-remote",
            "username": "operator",
            "state": {
                "status": "connected",
                "connected_at": connected_at.isoformat().replace("+00:00", "Z"),
                "token_expires_at": expires_at.isoformat().replace("+00:00", "Z"),
                "scope": "openid offline_access",
            },
        },
    }
    assert statuses["unconnected-remote"].model_dump(mode="json") == {
        "server_id": "unconnected-remote",
        "backend": {
            "kind": "remote_mcp",
            "url": "https://also-must-not-be-contacted.invalid/mcp",
            "auth": {
                "kind": "remote_server_oauth",
                "client_registration": {"kind": "dynamic", "client_name": "Haku Console"},
                "scopes": None,
            },
        },
        "connection": {"server_id": "unconnected-remote", "username": "operator", "state": {"status": "unconnected"}},
    }
    assert statuses["gmail"].model_dump(mode="json") == {
        "server_id": "gmail",
        "backend": {
            "kind": "in_process",
            "credential": {"kind": "operator_connection", "connection": "google_workspace"},
        },
        "connection": {
            "connection": "google_workspace",
            "display_name": "Google Workspace",
            "provider": "google",
            "status": "connected",
            "connected_at": connected_at.isoformat().replace("+00:00", "Z"),
            "token_expires_at": None,
            "scope": "openid email",
        },
    }
    assert statuses["routine"].model_dump(mode="json") == {
        "server_id": "routine",
        "backend": {"kind": "in_process", "credential": {"kind": "none"}},
        "connection": None,
    }
    assert statuses["static-remote"].model_dump(mode="json") == {
        "server_id": "static-remote",
        "backend": {"kind": "remote_mcp", "url": "https://static.invalid/mcp", "auth": {"kind": "static_bearer"}},
        "connection": None,
    }
    serialized = response.model_dump_json()
    assert "access_token" not in serialized
    assert "refresh_token" not in serialized
    assert "client_secret" not in serialized
    assert "bearer_token_secret" not in serialized
    assert "STATIC_REMOTE_TOKEN" not in serialized
    oauth_statuses.assert_called_once()
    provider_statuses.assert_called_once()
    refresh_remote.assert_not_awaited()
    refresh_provider.assert_not_awaited()
    fetch_metadata.assert_not_awaited()


async def test_get_mcp_server_status_reports_refresh_failure_as_degraded(
    migrated_db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = write_config(
        tmp_path / "refresh-failure.yaml",
        {
            "static_agents": _STATIC_AGENTS,
            "mcp": {
                "servers": [
                    {
                        "id": "standin",
                        "backend": _remote_backend("https://standin.invalid/mcp", _dynamic_remote_oauth()),
                    }
                ]
            },
        },
    )
    app = create_app(console_settings(migrated_db_url, config_file=config_file))
    monkeypatch.setattr(
        app.state.mcp_operator_oauth_store,
        "access_token_for",
        AsyncMock(side_effect=RuntimeError("MCP OAuth token refresh failed: 401")),
    )

    with serve_app_sync(app) as base:
        async with Client(f"{base}/mcp", auth=_AGENT_TOKEN) as client:
            result = await client.call_tool("get_mcp_server_status", {"server_id": "standin"})

    assert result.structured_content is not None
    assert result.structured_content["server"] == {
        "server_id": "standin",
        "title": "standin",
        "state": {
            "status": "degraded",
            "failure_stage": "credential_resolution",
            "degraded_reason": "MCP OAuth token refresh failed: 401",
        },
    }
    assert result.structured_content["connection"]["server_id"] == "standin"


async def test_cataloged_provider_without_oauth_client_is_reflected_as_unprovisioned(
    migrated_db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MISSING_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("MISSING_GOOGLE_CLIENT_SECRET", raising=False)
    config_file = write_config(
        tmp_path / "unprovisioned-provider.yaml",
        {
            "static_agents": _STATIC_AGENTS,
            "operator_connection_providers": {
                "google_calendar": {
                    "kind": "google",
                    "client_id_env_var": "MISSING_GOOGLE_CLIENT_ID",
                    "client_secret_env_var": "MISSING_GOOGLE_CLIENT_SECRET",
                }
            },
            "operator_connections": {
                "google_calendar": {
                    "display_name": "Google Calendar",
                    "provider": "google_calendar",
                    "scopes": ["https://www.googleapis.com/auth/calendar.events"],
                }
            },
            "mcp": {
                "servers": [
                    {
                        "id": "google_calendar",
                        "backend": _in_process_backend(
                            {"kind": "operator_connection", "connection": "google_calendar"}
                        ),
                    }
                ]
            },
        },
    )
    app = create_app(console_settings(migrated_db_url, config_file=config_file))

    with serve_app_sync(app) as base:
        async with Client(f"{base}/mcp", auth=_AGENT_TOKEN) as client:
            listed = await client.call_tool("list_mcp_servers", {})
            probed = await client.call_tool("get_mcp_server_status", {"server_id": "google_calendar"})

    assert listed.structured_content is not None
    connection = listed.structured_content["servers"][0]["connection"]
    assert connection["status"] == "unprovisioned"
    assert connection["display_name"] == "Google Calendar"
    assert probed.structured_content is not None
    assert probed.structured_content["connection"]["connection"] == connection
    assert probed.structured_content["server"]["state"]["status"] == "degraded"
    assert probed.structured_content["server"]["state"]["failure_stage"] == "credential_resolution"
    assert "not provisioned" in probed.structured_content["server"]["state"]["degraded_reason"]


async def test_get_mcp_server_status_includes_schemas_only_when_requested(
    migrated_db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = write_config(
        tmp_path / "schema-detail.yaml",
        {
            "static_agents": _STATIC_AGENTS,
            "mcp": {
                "servers": [
                    {"id": "standin", "backend": _remote_backend("https://standin.invalid/mcp", {"kind": "none"})}
                ]
            },
        },
    )
    app = create_app(console_settings(migrated_db_url, config_file=config_file))

    async def metadata_for_operator(**kwargs: Any) -> list[Tool]:
        return [
            Tool(
                name="echo",
                description="Echo input",
                inputSchema={"type": "object"},
                outputSchema={"type": "object", "properties": {"echoed": {"type": "string"}}},
            )
        ]

    monkeypatch.setattr(mcp_server_module, "metadata_for_operator", metadata_for_operator)
    with serve_app_sync(app) as base:
        async with Client(f"{base}/mcp", auth=_AGENT_TOKEN) as client:
            summary = await client.call_tool("get_mcp_server_status", {"server_id": "standin"})
            detailed = await client.call_tool(
                "get_mcp_server_status", {"server_id": "standin", "include_tool_schemas": True}
            )

    assert summary.structured_content is not None
    assert detailed.structured_content is not None
    assert summary.structured_content["server"]["state"]["tools"][0] == {
        "name": "echo",
        "title": None,
        "description": "Echo input",
        "input_schema": None,
        "output_schema": None,
        "annotations": None,
        "icons": None,
    }
    assert detailed.structured_content["server"]["state"]["tools"][0]["input_schema"] == {"type": "object"}
    assert detailed.structured_content["server"]["state"]["tools"][0]["output_schema"] == {
        "type": "object",
        "properties": {"echoed": {"type": "string"}},
    }


async def test_tool_discovery_is_concurrent_and_preserves_config_order(
    migrated_db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = write_config(
        tmp_path / "concurrent-tools.yaml",
        {
            "static_agents": _STATIC_AGENTS,
            "mcp": {
                "servers": [
                    {"id": "beta", "backend": _remote_backend("https://beta.invalid/mcp", {"kind": "none"})},
                    {"id": "alpha", "backend": _remote_backend("https://alpha.invalid/mcp", {"kind": "none"})},
                ]
            },
        },
    )
    app = create_app(console_settings(migrated_db_url, config_file=config_file))
    started: set[str] = set()
    both_started = asyncio.Event()

    async def metadata_for_operator(**kwargs: Any) -> list[Tool]:
        server_id = str(kwargs["server"].id)
        started.add(server_id)
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)
        if server_id == "beta":
            await asyncio.sleep(0.01)
        return [Tool(name="echo", inputSchema={"type": "object"})]

    monkeypatch.setattr(mcp_server_module, "metadata_for_operator", metadata_for_operator)
    with serve_app_sync(app) as base:
        async with Client(f"{base}/mcp", auth=_AGENT_TOKEN) as client:
            proxy_names = [tool.name for tool in await client.list_tools() if tool.name.endswith("__echo")]

    assert proxy_names == ["beta__echo", "alpha__echo"]


async def test_tool_discovery_isolates_unexpected_server_failure(
    migrated_db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    config_file = write_config(
        tmp_path / "isolated-tools.yaml",
        {
            "static_agents": _STATIC_AGENTS,
            "mcp": {
                "servers": [
                    {"id": "broken", "backend": _remote_backend("https://broken.invalid/mcp", {"kind": "none"})},
                    {"id": "healthy", "backend": _remote_backend("https://healthy.invalid/mcp", {"kind": "none"})},
                ]
            },
        },
    )
    app = create_app(console_settings(migrated_db_url, config_file=config_file))

    async def metadata_for_operator(**kwargs: Any) -> list[Tool]:
        server_id = str(kwargs["server"].id)
        if server_id == "broken":
            raise RuntimeError("unexpected reflection failure")
        return [Tool(name="echo", inputSchema={"type": "object"})]

    monkeypatch.setattr(mcp_server_module, "metadata_for_operator", metadata_for_operator)
    with serve_app_sync(app) as base:
        async with Client(f"{base}/mcp", auth=_AGENT_TOKEN) as client:
            proxy_names = [tool.name for tool in await client.list_tools() if tool.name.endswith("__echo")]

    assert proxy_names == ["healthy__echo"]
    assert "failed to reflect server broken" in caplog.text
    assert "unexpected reflection failure" in caplog.text


async def test_tool_dispatch_reflects_only_target_server(
    migrated_db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = write_config(
        tmp_path / "targeted-dispatch.yaml",
        {
            "static_agents": _STATIC_AGENTS,
            "mcp": {
                "servers": [
                    {"id": "alpha", "backend": _remote_backend("https://alpha.invalid/mcp", {"kind": "none"})},
                    {"id": "beta", "backend": _remote_backend("https://beta.invalid/mcp", {"kind": "none"})},
                ]
            },
        },
    )
    settings = console_settings(migrated_db_url, config_file=config_file)
    app = create_app(settings)
    reflected: list[str] = []

    async def metadata_for_operator(**kwargs: Any) -> list[Tool]:
        server_id = str(kwargs["server"].id)
        reflected.append(server_id)
        return [Tool(name="echo", inputSchema={"type": "object"})]

    monkeypatch.setattr(mcp_server_module, "metadata_for_operator", metadata_for_operator)
    actor = AgentActor(
        agent_id=UUID("40000000-0000-4000-8000-000000000001"),
        operator_id=UUID("10000000-0000-4000-8000-000000000001"),
        binding_id=UUID("50000000-0000-4000-8000-000000000001"),
    )
    actor_resolver = Mock(spec=mcp_server_module.HakuMcpActorResolver)
    actor_resolver.resolve = AsyncMock(return_value=actor)
    provider = mcp_server_module.OperatorToolProvider(
        mcp_server_module.ConsoleMcpContext(
            settings=settings,
            tool_calls=app.state.tool_call_service,
            oauth_store=app.state.mcp_operator_oauth_store,
            provider_store=app.state.provider_connection_store,
            metadata_provider=app.state.tool_call_metadata_provider,
        ),
        actor_resolver,
    )

    tool = await provider._get_tool("beta__echo")

    assert isinstance(tool, mcp_server_module.ProxyTool)
    assert tool.name == "beta__echo"
    assert tool.actor == actor
    assert reflected == ["beta"]


async def test_targeted_dispatch_reports_a_known_degraded_server(
    migrated_db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = write_config(
        tmp_path / "degraded-dispatch.yaml",
        {
            "static_agents": _STATIC_AGENTS,
            "mcp": {
                "servers": [
                    {
                        "id": "grocy-sf",
                        "backend": {"kind": "remote_mcp", "url": "https://grocy.invalid/mcp", "auth": {"kind": "none"}},
                    }
                ]
            },
        },
    )
    settings = console_settings(migrated_db_url, config_file=config_file)
    app = create_app(settings)

    async def metadata_for_operator(**kwargs: Any) -> DegradedReflection:
        return DegradedReflection(
            failure_stage="credential_resolution", degraded_reason="MCP OAuth token refresh failed: 401"
        )

    monkeypatch.setattr(mcp_server_module, "metadata_for_operator", metadata_for_operator)
    actor_resolver = Mock(spec=mcp_server_module.HakuMcpActorResolver)
    actor_resolver.resolve = AsyncMock(
        return_value=AgentActor(agent_id=UUID(int=1), operator_id=UUID(int=2), binding_id=UUID(int=3))
    )
    provider = mcp_server_module.OperatorToolProvider(
        mcp_server_module.ConsoleMcpContext(
            settings=settings,
            tool_calls=app.state.tool_call_service,
            oauth_store=app.state.mcp_operator_oauth_store,
            provider_store=app.state.provider_connection_store,
            metadata_provider=app.state.tool_call_metadata_provider,
        ),
        actor_resolver,
    )

    assert await provider._get_tool("grocy_sf__product_groups_list") is None


@dataclass(frozen=True)
class _MockOidc:
    origin: str
    issuer: str


@contextmanager
def _serve_mock_oidc() -> Generator[_MockOidc]:
    """A signed OIDC provider whose stable subject represents the authorizing operator."""
    private_key, public_key = generate_rsa_keypair()
    oidc_port = pick_free_port()
    oidc_origin = f"http://127.0.0.1:{oidc_port}"
    issuer = f"{oidc_origin}/application/o/haku-agent/"
    app = build_mock_oidc_app(
        issuer_url=issuer,
        private_key=private_key,
        public_key=public_key,
        subject="42",
        extra_id_token_claims={"preferred_username": "Rai"},
        authentik_compatible=True,
    )
    with serve_app_sync(app, port=oidc_port) as base:
        yield _MockOidc(origin=base, issuer=issuer)


def _pkce_challenge(code_verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()


def test_mcp_oauth_requires_postgres_persistence() -> None:
    provider = {
        "oidc_issuer": "https://auth.example.test/application/o/haku-console-mcp/",
        "oidc_client_id": "console",
        "oidc_client_secret": "secret",
    }
    with pytest.raises(ValidationError, match="persistence"):
        McpOAuthConfig.model_validate(provider)
    with pytest.raises(ValidationError, match="persistence"):
        McpOAuthConfig.model_validate({**provider, "persistence": {"kind": "file"}})
    with pytest.raises(ValidationError, match="persistence"):
        McpOAuthConfig.model_validate({**provider, "persistence": {"kind": "valkey", "host": "valkey.example.test"}})


def test_mcp_oauth_persistence_must_share_the_console_database() -> None:
    oauth = McpOAuthConfig(
        oidc_issuer="https://auth.example.test/application/o/haku-console-mcp/",
        oidc_client_id="console",
        oidc_client_secret=SecretStr("secret"),
        persistence=PostgresPersistence(kind="postgres", url="postgresql://app:secret@other-db.example.test:5432/haku"),
    )

    with pytest.raises(ValidationError, match="same Postgres"):
        console_settings("postgresql+psycopg://app:secret@db.example.test:5432/haku", mcp_oauth=oauth)


def test_mcp_oauth_reads_nested_shared_persistence_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HAKU_CONSOLE_MCP_OAUTH__OIDC_ISSUER", "https://auth.example.test/application/o/mcp/")
    monkeypatch.setenv("HAKU_CONSOLE_MCP_OAUTH__OIDC_CLIENT_ID", "console")
    monkeypatch.setenv("HAKU_CONSOLE_MCP_OAUTH__OIDC_CLIENT_SECRET", "secret")
    monkeypatch.setenv("HAKU_CONSOLE_MCP_OAUTH__PERSISTENCE__KIND", "postgres")
    monkeypatch.setenv("HAKU_CONSOLE_MCP_OAUTH__PERSISTENCE__URL", "postgresql://db.example.test/haku")

    oauth = console_settings("postgresql+psycopg://db.example.test/haku").mcp_oauth

    assert oauth is not None
    assert oauth.persistence == PostgresPersistence(
        kind="postgres", url="postgresql://db.example.test/haku", table_name="mcp_oauth_kv"
    )


async def test_oauth_composes_with_static_bearer(migrated_db_url: str, tmp_path: Path) -> None:
    with _serve_mock_oidc() as oidc, _serve_upstream() as upstream_url:
        # public_base_url is the console's own URL, so choose its port before building the app.
        console_port = pick_free_port()
        settings = console_settings(
            migrated_db_url,
            config_file=_console_config(tmp_path, upstream_url),
            ui_base_url="https://haku.test",
            public_base_url=f"http://127.0.0.1:{console_port}",
            mcp_oauth=McpOAuthConfig(
                oidc_issuer=oidc.issuer,
                oidc_client_id="console",
                oidc_client_secret=SecretStr("secret"),
                # Match production's shared Postgres-backed DCR/token state, but use this test's
                # isolated database instead of FastMCP's implicit process-global file store.
                persistence=PostgresPersistence(
                    kind="postgres", url=migrated_db_url.replace("postgresql+psycopg://", "postgresql://", 1)
                ),
            ),
            operator_oidc=OperatorOidcConfig(
                issuer=oidc.issuer,
                client_id="haku-console",
                client_secret=SecretStr("operator-oauth-secret"),
                session_secret=SecretStr("operator-session-secret"),
            ),
        )
        app = create_app(settings)
        with serve_app_sync(app, port=console_port) as base:
            # The static bearer still authenticates (MultiAuth composes OAuth + static).
            async with Client(f"{base}/mcp", auth=_AGENT_TOKEN) as client:
                assert "standin__echo" in {t.name for t in await client.list_tools()}

            async with httpx.AsyncClient() as anon:
                slash_redirect = await anon.get(f"{base}/mcp/", follow_redirects=False)
                assert slash_redirect.status_code == 307
                assert slash_redirect.headers["location"] == "/mcp"
                duplicate_transport = await anon.post(f"{base}/mcp/mcp", follow_redirects=False)
                assert duplicate_transport.status_code == 307
                assert duplicate_transport.headers["location"] == "/mcp"

                # Walk the production OAuth discovery chain from the challenge through DCR. The
                # well-known documents live at the origin root, while every operational endpoint
                # and callback remains namespaced under /mcp (separate from operator /auth/*).
                unauth = await anon.post(
                    f"{base}/mcp",
                    headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                )
                assert unauth.status_code == 401
                challenge = unauth.headers.get("www-authenticate", "")
                match = re.search(r'resource_metadata="([^"]+)"', challenge, flags=re.IGNORECASE)
                assert match is not None, challenge
                resource_metadata_url = match.group(1)
                assert resource_metadata_url == f"{base}/.well-known/oauth-protected-resource/mcp"

                protected_response = await anon.get(resource_metadata_url)
                assert protected_response.status_code == 200, protected_response.text
                protected = protected_response.json()
                assert protected["resource"] == f"{base}/mcp"
                assert protected["authorization_servers"] == [f"{base}/mcp"]

                authorization_metadata_url = f"{base}/.well-known/oauth-authorization-server/mcp"
                authorization_response = await anon.get(authorization_metadata_url)
                assert authorization_response.status_code == 200, authorization_response.text
                authorization = authorization_response.json()
                assert authorization["issuer"] == f"{base}/mcp"
                assert authorization["authorization_endpoint"] == f"{base}/mcp/authorize"
                assert authorization["token_endpoint"] == f"{base}/mcp/token"
                assert authorization["registration_endpoint"] == f"{base}/mcp/register"
                assert authorization["client_id_metadata_document_supported"] is True

                registration = await anon.post(
                    authorization["registration_endpoint"],
                    json={
                        "client_name": "claude.ai",
                        "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                        "grant_types": ["authorization_code", "refresh_token"],
                        "response_types": ["code"],
                        "token_endpoint_auth_method": "client_secret_post",
                        "scope": "openid email profile offline_access",
                    },
                )
                assert registration.status_code == 201, registration.text
                registered = registration.json()
                assert registered["client_id"]
                assert registered["client_secret"]

                code_verifier = secrets.token_urlsafe(32)
                authorize = await anon.get(
                    authorization["authorization_endpoint"],
                    params={
                        "response_type": "code",
                        "client_id": registered["client_id"],
                        "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                        "scope": "openid email profile offline_access",
                        "state": "client-state",
                        "code_challenge": _pkce_challenge(code_verifier),
                        "code_challenge_method": "S256",
                        "resource": f"{base}/mcp",
                    },
                    follow_redirects=False,
                )
                assert authorize.status_code == 302
                enrollment_url = authorize.headers["location"]
                assert enrollment_url.startswith(f"{base}/auth/agent-enrollment/")

                enrollment = await anon.get(enrollment_url, follow_redirects=True)
                settings_redirect = next(
                    response
                    for response in reversed(enrollment.history)
                    if response.headers.get("location", "").startswith("/_console/settings/agents/enroll/")
                )
                settings_enrollment_path = settings_redirect.headers["location"]
                assert settings_enrollment_path.startswith("/_console/settings/agents/enroll/")
                interaction_id = settings_enrollment_path.rsplit("/", 1)[-1]
                enrollment_view = await anon.get(f"{base}/api/agent-enrollment/{interaction_id}")
                assert enrollment_view.status_code == 200, enrollment_view.text
                enrollment_data = enrollment_view.json()
                assert enrollment_data["client_software"] == "claude.ai"
                approved = await anon.post(
                    f"{base}/api/agent-enrollment/{interaction_id}/decision",
                    headers={"Origin": base},
                    json={
                        "kind": "create",
                        "form_token": enrollment_data["form_token"],
                        "display_name": "OAuth Claude",
                    },
                    follow_redirects=False,
                )
                assert approved.status_code == 200, approved.text
                upstream_authorize = httpx.URL(approved.json()["authorization_url"])
                assert str(upstream_authorize).startswith(f"{oidc.origin}/application/o/authorize/?")
                assert upstream_authorize.params["redirect_uri"] == f"{base}/mcp/auth/callback"

                upstream_callback = await anon.get(str(upstream_authorize), follow_redirects=False)
                assert upstream_callback.status_code == 302, upstream_callback.text
                assert upstream_callback.headers["location"].startswith(f"{base}/mcp/auth/callback?")

                downstream_callback = await anon.get(upstream_callback.headers["location"], follow_redirects=False)
                assert downstream_callback.status_code == 302, downstream_callback.text
                client_callback = httpx.URL(downstream_callback.headers["location"])
                assert client_callback.scheme == "https"
                assert client_callback.host == "claude.ai"
                assert client_callback.path == "/api/mcp/auth_callback"
                assert client_callback.params["state"] == "client-state"

                exchanged = await anon.post(
                    authorization["token_endpoint"],
                    data={
                        "grant_type": "authorization_code",
                        "code": client_callback.params["code"],
                        "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                        "client_id": registered["client_id"],
                        "client_secret": registered["client_secret"],
                        "code_verifier": code_verifier,
                    },
                )
                assert exchanged.status_code == 200, exchanged.text
                access_token = exchanged.json()["access_token"]
                assert isinstance(access_token, str)
                assert access_token

                # The returned bearer activates the exact Haku grant/binding and injects its
                # canonical request-local AgentActor into a real FastMCP dependency.
                async with Client(f"{base}/mcp", auth=access_token) as oauth_client:
                    assert {"get_tool_call", "list_tool_calls", "standin__echo"} <= {
                        tool.name for tool in await oauth_client.list_tools()
                    }
                    listed = await oauth_client.call_tool("list_tool_calls", {})
                    assert listed.structured_content == {"result": []}

                # Discovery is shared at root, not the operational OAuth surface.
                assert (await anon.post(f"{base}/register", json={})).status_code == 404


def test_duplicate_static_agent_ids_fail_startup(migrated_db_url: str, tmp_path: Path) -> None:
    config_file = write_config(
        tmp_path / "duplicate-agent.yaml", {"static_agents": [*_STATIC_AGENTS, _STATIC_AGENTS[0]]}
    )
    with pytest.raises(ValidationError, match="duplicate static Agent id"):
        create_app(console_settings(migrated_db_url, config_file=config_file))


def test_duplicate_mcp_server_ids_fail_config_validation() -> None:
    with pytest.raises(ValidationError, match="duplicate MCP server id 'grocy'"):
        ConsoleConfigFile.model_validate(
            {
                "mcp": {
                    "servers": [
                        {"id": "grocy", "backend": _in_process_backend({"kind": "none"})},
                        {"id": "grocy", "backend": _in_process_backend({"kind": "none"})},
                    ]
                }
            }
        )


def test_duplicate_sanitized_mcp_server_prefixes_fail_config_validation() -> None:
    with pytest.raises(ValidationError, match="duplicate MCP server tool prefix 'grocy_sf'"):
        ConsoleConfigFile.model_validate(
            {
                "mcp": {
                    "servers": [
                        {"id": "grocy-sf", "backend": _in_process_backend({"kind": "none"})},
                        {"id": "grocy_sf", "backend": _in_process_backend({"kind": "none"})},
                    ]
                }
            }
        )


def test_duplicate_static_agent_tokens_fail_startup(
    migrated_db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HAKU_CONSOLE_TEST_AGENT2_OPERATOR", "99")
    config_file = write_config(
        tmp_path / "duplicate-token.yaml",
        {
            "static_agents": [
                *_STATIC_AGENTS,
                {
                    "agent_id": "40000000-0000-4000-8000-000000000005",
                    "display_name": "Ops Bot",
                    "token_env_var": _AGENT_TOKEN_ENV,
                    "operator_subject_env": "HAKU_CONSOLE_TEST_AGENT2_OPERATOR",
                },
            ]
        },
    )
    with pytest.raises(RuntimeError, match="duplicate static agent bearer tokens"):
        create_app(console_settings(migrated_db_url, config_file=config_file))


if __name__ == "__main__":
    pytest_bazel.main()
