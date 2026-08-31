"""Tests for haku-console's own MCP server (the connected-server tool proxy)."""

from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import re
import secrets
import warnings
from collections.abc import AsyncGenerator, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_bazel
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from jsonschema import Draft202012Validator
from mcp.types import Icon, TextContent, Tool, ToolAnnotations
from pydantic import SecretStr, ValidationError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012
from sqlalchemy.engine import make_url

from haku.console.app import create_app
from haku.console.config import McpOAuthConfig, OperatorOidcConfig
from haku.console.conftest import console_settings, operator_session_cookie, resolve_operator_identity, write_config
from haku.console.identity.operator_identity import ResolvedOperatorIdentity
from haku.console.mcp import catalog_reconciler as mcp_catalog_reconciler_module, server as mcp_server_module
from haku.console.mcp.approval import DegradedReflection, ReflectionFailureStage
from haku.console.mcp.guidance import SERVER_INSTRUCTIONS
from haku.console.mcp.operator_oauth import (
    McpOperatorAuthConnected,
    McpOperatorAuthStatus,
    McpOperatorAuthStatusResponse,
    McpOperatorAuthUnconnected,
)
from haku.console.mcp.reflection_cache import ReflectedCatalog
from haku.console.mcp.tool_call_service import ToolCallApplicationService, ToolCallNotFoundError
from haku.console.mcp_config import ConsoleConfigFile, McpServerEntry, const_in_process_server
from haku.console.oauth.provider_connection import ProviderConnected, ProviderConnectionStatusResponse
from haku.console.tool_call_actor import AgentActor, OperatorActor, RuntimeActor
from haku.console.tool_calls import (
    MCP_TOOL_CALL_META_KEY,
    MCP_TOOL_META_KEY,
    AgentToolCallCaller,
    SubmitToolCallRequest,
    ToolCallPayloadField,
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
# `haku` agent id (which acts as operator subject "42").
_AGENT_TOKEN = "agent-token"
_SIBLING_AGENT_TOKEN = "sibling-agent-token"
_OTHER_AGENT_TOKEN = "other-agent-token"
_OTHER_SIBLING_AGENT_TOKEN = "other-sibling-agent-token"
_MANUAL_POLICY_ID = "manual_review"
_MANUAL_ACCESS_PROFILE_ID = "manual_review"
_MANUAL_AUTHORITY_CONFIG = {
    "auto_approval_policies": [{"id": _MANUAL_POLICY_ID, "type": "never"}],
    "access_profiles": [{"id": _MANUAL_ACCESS_PROFILE_ID, "auto_approval_policy": _MANUAL_POLICY_ID}],
    "default_access_profile_id": _MANUAL_ACCESS_PROFILE_ID,
}
_STATIC_AGENTS = {
    "haku": {
        "agent_id": "40000000-0000-4000-8000-000000000001",
        "display_name": "Haku",
        "token": _AGENT_TOKEN,
        "operator_subject": "42",
        "access_profile_id": _MANUAL_ACCESS_PROFILE_ID,
    },
    "sibling": {
        "agent_id": "40000000-0000-4000-8000-000000000002",
        "display_name": "Sibling",
        "token": _SIBLING_AGENT_TOKEN,
        "operator_subject": "42",
        "access_profile_id": _MANUAL_ACCESS_PROFILE_ID,
    },
    "other": {
        "agent_id": "40000000-0000-4000-8000-000000000003",
        "display_name": "Other",
        "token": _OTHER_AGENT_TOKEN,
        "operator_subject": "99",
        "access_profile_id": _MANUAL_ACCESS_PROFILE_ID,
    },
    "other_sibling": {
        "agent_id": "40000000-0000-4000-8000-000000000004",
        "display_name": "Other Sibling",
        "token": _OTHER_SIBLING_AGENT_TOKEN,
        "operator_subject": "99",
        "access_profile_id": _MANUAL_ACCESS_PROFILE_ID,
    },
}


def _with_manual_authority(config: dict[str, Any]) -> dict[str, Any]:
    return {**_MANUAL_AUTHORITY_CONFIG, **config}


def _write_console_config(path: Path, config: dict[str, Any]) -> Path:
    return write_config(path, _with_manual_authority(config))


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


@dataclass
class _Harness:
    base: str  # base URL of the served console MCP; open `Client(f"{base}/mcp", auth=_AGENT_TOKEN)`
    origin: str
    tool_calls: ToolCallApplicationService
    operator_identity: ResolvedOperatorIdentity
    other_operator_identity: ResolvedOperatorIdentity
    max_wait_for_result_ms: int


@pytest.fixture
async def harness(migrated_db_url: str, migrated_sessions, tmp_path: Path) -> AsyncGenerator[_Harness]:
    gmail_client = Mock()
    # labels_list is a generated read: it dispatches through gmail_client.service, returning raw Gmail JSON.
    gmail_client.service.users.return_value.labels.return_value.list.return_value.execute.return_value = {
        "labels": [{"id": "Label_1", "name": "haku/triaged", "type": "user"}]
    }
    calendar_client = Mock()
    calendar_client.get_event.return_value = CalendarEvent(
        event_id="series1", summary="Standup", recurrence=["RRULE:FREQ=WEEKLY"]
    )
    config_file = _write_console_config(
        tmp_path / "console.yaml",
        {
            "static_agents": {
                slot: {**agent, **({"access_profile_id": "haku"} if agent["display_name"] == "Haku" else {})}
                for slot, agent in _STATIC_AGENTS.items()
            },
            "auto_approval_policies": [
                {
                    "id": "transparent_reads",
                    "type": "exact_tools",
                    "tools": {
                        "gmail": [
                            "threads_list",
                            "threads_get",
                            "messages_get",
                            "labels_list",
                            "labels_get",
                            "filters_list",
                            "filters_get",
                            "drafts_list",
                            "drafts_get",
                        ],
                        "google_calendar": ["get_event", "list_events", "list_event_instances"],
                    },
                },
                {
                    "id": "managed_gmail_labels",
                    "type": "gmail_label_namespace",
                    "server": "gmail",
                    "label_prefix": "haku/",
                },
                {"id": "haku_v1", "type": "any_of", "policies": ["transparent_reads", "managed_gmail_labels"]},
                {"id": _MANUAL_POLICY_ID, "type": "never"},
            ],
            "access_profiles": [
                {"id": "haku", "auto_approval_policy": "haku_v1"},
                {"id": _MANUAL_ACCESS_PROFILE_ID, "auto_approval_policy": _MANUAL_POLICY_ID},
            ],
            "default_access_profile_id": _MANUAL_ACCESS_PROFILE_ID,
            "mcp": {
                "servers": {
                    "gmail": {"id": "gmail", "backend": _in_process_backend({"kind": "none"})},
                    "google_calendar": {"id": "google_calendar", "backend": _in_process_backend({"kind": "none"})},
                }
            },
        },
    )
    settings = console_settings(migrated_db_url, config_file=config_file)
    in_process = {
        gmail_tools.GMAIL_SERVER_ID: const_in_process_server(gmail_tools.build_mcp(gmail_client)),
        calendar_tools.GOOGLE_CALENDAR_SERVER_ID: const_in_process_server(calendar_tools.build_mcp(calendar_client)),
    }
    app = create_app(settings, gmail_client=gmail_client, in_process_servers=in_process)
    operator_identity = await resolve_operator_identity(
        migrated_sessions, issuer=settings.operator_oidc.issuer, subject="42"
    )
    other_operator_identity = await resolve_operator_identity(
        migrated_sessions, issuer=settings.operator_oidc.issuer, subject="99"
    )
    with serve_app_sync(app) as base:
        yield _Harness(
            base=base,
            origin=settings.public_base_url.rstrip("/"),
            tool_calls=app.state.tool_call_service,
            operator_identity=operator_identity,
            other_operator_identity=other_operator_identity,
            max_wait_for_result_ms=settings.max_wait_for_result_ms,
        )


@pytest.fixture
async def agent_client(harness: _Harness) -> AsyncGenerator[Client]:
    """The common case: a client connected and authenticated as the sole static `Haku` agent.
    Tests that need a different or additional token (comparing multiple agents) open their own
    `Client(...)` instead of using this fixture."""
    async with Client(f"{harness.base}/mcp", auth=_AGENT_TOKEN) as client:
        yield client


def _operator_cookies(identity: ResolvedOperatorIdentity) -> dict[str, str]:
    return {
        "session": operator_session_cookie(
            operator_id=str(identity.operator_id), identity_id=str(identity.identity_id), username="operator"
        )
    }


async def _operator_get(
    harness: _Harness, path: str, *, identity: ResolvedOperatorIdentity | None = None, **kwargs: Any
) -> httpx.Response:
    identity = identity or harness.operator_identity
    async with httpx.AsyncClient(base_url=harness.base, cookies=_operator_cookies(identity)) as client:
        return await client.get(path, **kwargs)


async def _operator_post(
    harness: _Harness, path: str, *, identity: ResolvedOperatorIdentity | None = None, **kwargs: Any
) -> httpx.Response:
    identity = identity or harness.operator_identity
    async with httpx.AsyncClient(base_url=harness.base, cookies=_operator_cookies(identity)) as client:
        return await client.post(path, headers={"Origin": harness.origin}, **kwargs)


async def test_tool_surface_splits_pass_through_and_request(agent_client: Client, harness: _Harness) -> None:
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
    assert set(envelope["properties"]) == {"input", "title", "rationale", "wait_for_result_ms"}
    assert envelope["additionalProperties"] is False
    wait_schema = envelope["properties"]["wait_for_result_ms"]
    assert wait_schema == {
        "default": mcp_server_module.DEFAULT_WAIT_MS,
        "description": wait_schema["description"],
        "maximum": harness.max_wait_for_result_ms,
        "minimum": 0,
        "title": "Wait For Result Ms",
        "type": "integer",
    }
    assert "anyOf" not in wait_schema
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
    get_fields = tools["get_tool_call"].inputSchema["properties"]["fields"]
    list_fields = tools["list_tool_calls"].inputSchema["properties"]["fields"]
    assert get_fields["items"]["enum"] == [field.value for field in ToolCallPayloadField]
    assert get_fields["default"] == [ToolCallPayloadField.RESULT]
    assert list_fields["items"]["enum"] == [field.value for field in ToolCallPayloadField]
    assert list_fields["default"] == []
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
    # The envelope shape and the polling operation are in the tool's description; lifecycle
    # semantics are shared through the server instructions.
    gmail_write_description = tools["gmail__drafts_create"].description
    assert gmail_write_description is not None
    for phrase in ("requires operator approval", "wait_for_result_ms", "get_tool_call"):
        assert phrase in gmail_write_description
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


def test_console_server_instructions_keep_client_critical_guidance() -> None:
    assert SERVER_INSTRUCTIONS == mcp_server_module.SERVER_INSTRUCTIONS
    for phrase in ("<server>__<tool>", "pending_approval", "get_tool_call", "call_mcp_tool"):
        assert phrase in SERVER_INSTRUCTIONS


def test_approval_envelope_rejects_old_wait_field_name() -> None:
    model = mcp_server_module._approval_request_envelope_model(max_wait_ms=60_000)
    with pytest.raises(ValidationError, match="wait_for_approval_ms"):
        model.model_validate({"input": {}, "rationale": "test", "wait_for_approval_ms": 0})


def test_approval_envelope_wait_has_default_and_strict_bounds() -> None:
    max_wait_ms = 60_000
    model = mcp_server_module._approval_request_envelope_model(max_wait_ms=max_wait_ms)
    envelope = model.model_validate({"input": {}, "rationale": "test"})
    assert envelope.wait_for_result_ms == mcp_server_module.DEFAULT_WAIT_MS
    for wait_ms in (0, max_wait_ms):
        assert (
            model.model_validate({"input": {}, "rationale": "test", "wait_for_result_ms": wait_ms}).wait_for_result_ms
            == wait_ms
        )
    for invalid_wait_ms in (-1, max_wait_ms + 1, None, "0"):
        with pytest.raises(ValidationError):
            model.model_validate({"input": {}, "rationale": "test", "wait_for_result_ms": invalid_wait_ms})


def test_approval_envelope_schema_uses_requested_dynamic_bounds() -> None:
    model = mcp_server_module._approval_request_envelope_model(default_wait_ms=7, min_wait_ms=1, max_wait_ms=9)
    wait_schema = model.model_json_schema()["properties"]["wait_for_result_ms"]
    assert wait_schema["default"] == 7
    assert wait_schema["minimum"] == 1
    assert wait_schema["maximum"] == 9


async def test_tool_surface_is_specific_to_the_authenticated_agent(harness: _Harness) -> None:
    async with Client(f"{harness.base}/mcp", auth=_SIBLING_AGENT_TOKEN) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    # Haku's exact-tools policy makes this transparent only for Haku. The unassigned sibling sees
    # the approval envelope and cannot inherit Haku's standing read authority.
    labels_list = tools["gmail__labels_list"]
    assert set(labels_list.inputSchema["required"]) == {"input", "rationale"}
    assert labels_list.meta is not None
    assert labels_list.meta[MCP_TOOL_META_KEY]["approval_mode"] == "approval_required"


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
    tool_call_id = result.meta[MCP_TOOL_CALL_META_KEY]["tool_call_id"]
    response = await _operator_get(harness, f"/api/tool-calls/{tool_call_id}")
    assert response.status_code == 200, response.text
    call = response.json()
    assert call["status"] == ToolCallStatus.OK
    assert call["tool_name"] == "labels_list"
    # The pass-through call is audited as the static agent that presented the bearer.
    assert call["caller"]["kind"] == "agent"
    assert call["caller"]["display_name"] == "Haku"


async def test_list_tool_calls_tool_filters_by_auto_approved(agent_client: Client) -> None:
    await agent_client.call_tool("gmail__labels_list", {})
    stub = await agent_client.call_tool(
        "gmail__drafts_create",
        {"input": {"to": ["a@b.test"], "subject": "s", "body": "b"}, "rationale": "test", "wait_for_result_ms": 0},
    )
    assert stub.structured_content is not None
    manual_id = stub.structured_content["tool_call_id"]

    hidden = await agent_client.call_tool("list_tool_calls", {"auto_approved": False})
    shown_only = await agent_client.call_tool("list_tool_calls", {"auto_approved": True})
    unfiltered = await agent_client.call_tool("list_tool_calls", {})

    def call_ids(result: Any) -> list[str]:
        assert result.structured_content is not None
        return [view["tool_call_id"] for view in result.structured_content["result"]]

    hidden_ids = call_ids(hidden)
    shown_ids = call_ids(shown_only)
    assert hidden_ids == [manual_id]
    assert len(shown_ids) == 1
    assert shown_ids != [manual_id]
    assert set(call_ids(unfiltered)) == {manual_id, *shown_ids}


async def test_tool_call_payload_fields_project_and_omit_nullable_values(agent_client: Client) -> None:
    result = await agent_client.call_tool("gmail__labels_list", {})
    assert result.meta is not None
    assert result.structured_content is not None
    tool_call_id = result.meta[MCP_TOOL_CALL_META_KEY]["tool_call_id"]

    listed = await agent_client.call_tool("list_tool_calls", {})
    assert listed.structured_content is not None
    listed_call = listed.structured_content["result"][0]
    assert {"arguments", "caller", "rationale", "result"}.isdisjoint(listed_call)

    default = await agent_client.call_tool("get_tool_call", {"tool_call_id": tool_call_id})
    assert default.structured_content is not None
    default_call = default.structured_content
    assert default_call["result"]["structuredContent"] == result.structured_content
    assert {"arguments", "caller", "rationale"}.isdisjoint(default_call)

    compact = await agent_client.call_tool("get_tool_call", {"tool_call_id": tool_call_id, "fields": []})
    assert compact.structured_content is not None
    assert {"arguments", "caller", "rationale", "result"}.isdisjoint(compact.structured_content)

    selected = await agent_client.call_tool(
        "get_tool_call", {"tool_call_id": tool_call_id, "fields": ["arguments", "caller", "rationale", "result"]}
    )
    assert selected.structured_content is not None
    selected_call = selected.structured_content
    assert {"arguments", "caller", "rationale", "result"} <= selected_call.keys()
    assert selected_call["caller"]["kind"] == "agent"
    assert set(selected_call["caller"]) == {"kind", "agent_id", "display_name", "session_id"}

    caller_only = await agent_client.call_tool("list_tool_calls", {"fields": ["caller"]})
    assert caller_only.structured_content is not None
    assert {"arguments", "rationale", "result"}.isdisjoint(caller_only.structured_content["result"][0])
    assert caller_only.structured_content["result"][0]["caller"]["kind"] == "agent"

    pending = await agent_client.call_tool(
        "gmail__drafts_create",
        {"input": {"to": ["a@b.test"], "subject": "s", "body": "b"}, "rationale": "test", "wait_for_result_ms": 0},
    )
    assert pending.structured_content is not None
    pending_id = pending.structured_content["tool_call_id"]
    selected_null = await agent_client.call_tool("get_tool_call", {"tool_call_id": pending_id, "fields": ["result"]})
    assert selected_null.structured_content is not None
    assert "result" in selected_null.structured_content
    assert selected_null.structured_content["result"] is None


def test_mcp_tool_call_response_preserves_the_typed_caller() -> None:
    caller = AgentToolCallCaller(agent_id=uuid4(), display_name="Agent")
    now = datetime.datetime.now(datetime.UTC)
    response = mcp_server_module._mcp_tool_call_response(
        ToolCallRecord(
            tool_call_id="tc_typed_caller",
            server_id="gmail",
            tool_name="labels_list",
            caller=caller,
            status=ToolCallStatus.OK,
            created_at=now,
            updated_at=now,
            arguments={},
        ),
        console_settings("postgresql://unused/typed-caller"),
    )
    assert isinstance(response.caller, AgentToolCallCaller)
    assert response.caller == caller
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert response.model_dump()["caller"]["agent_id"] == caller.agent_id


async def test_schema_invalid_call_fails_fast_and_never_queues(harness: _Harness, agent_client: Client) -> None:
    """A schema-invalid call on an owned in-process server is born-denied: the caller gets the
    validation error immediately and nothing enters the approval queue (operator, 2026-07-16)."""
    with pytest.raises(ToolError, match="single_events"):
        await agent_client.call_tool("google_calendar__list_events", {"single_events": True})

    response = await _operator_get(harness, "/api/tool-calls")
    assert response.status_code == 200, response.text
    calls = response.json()["tool_calls"]
    assert len(calls) == 1
    assert calls[0]["status"] == ToolCallStatus.DENIED
    assert calls[0]["decision_note"] is not None
    assert calls[0]["decision_operator_id"] is None
    assert "single_events" in calls[0]["decision_note"]
    assert calls[0]["auto_approval_evaluation"] == "denied: arguments failed the registered tool schema"
    pending = await _operator_get(harness, "/api/approvals/pending")
    assert pending.status_code == 200, pending.text
    assert pending.json()["approvals"] == []


async def test_calendar_read_is_transparent_and_audited(harness: _Harness, agent_client: Client) -> None:
    result = await agent_client.call_tool("google_calendar__get_event", {"event_id": "series1"})

    assert result.structured_content is not None
    assert result.structured_content["event_id"] == "series1"
    response = await _operator_get(harness, "/api/tool-calls")
    assert response.status_code == 200, response.text
    calls = response.json()["tool_calls"]
    assert len(calls) == 1
    assert calls[0]["server_id"] == "google_calendar"
    assert calls[0]["tool_name"] == "get_event"


async def test_request_tool_returns_pending_stub_with_deep_link(agent_client: Client) -> None:
    result = await agent_client.call_tool(
        "gmail__drafts_create",
        {"input": {"to": ["a@b.test"], "subject": "s", "body": "b"}, "rationale": "test", "wait_for_result_ms": 0},
    )
    stub = result.structured_content
    assert stub is not None
    assert stub["status"] == ToolCallStatus.PENDING_APPROVAL
    tool_call_id = stub["tool_call_id"]
    assert result.meta is not None
    assert result.meta[MCP_TOOL_CALL_META_KEY] == {"tool_call_id": tool_call_id}
    assert tool_call_id.startswith("tc_")
    assert stub["url"] == f"https://haku.test/_console/tool-calls/{tool_call_id}"
    assert "did not approve or deny before the synchronous wait ended" in stub["message"]
    assert "may approve or deny it later" in stub["message"]
    assert "if approved, the tool call will execute" in stub["message"]

    got = await agent_client.call_tool("get_tool_call", {"tool_call_id": tool_call_id})
    view = got.structured_content
    assert view is not None
    assert view["status"] == ToolCallStatus.PENDING_APPROVAL
    assert view["tool_name"] == "drafts_create"
    assert view["url"] == f"https://haku.test/_console/tool-calls/{tool_call_id}"


async def test_request_tool_preserves_explicit_zero_wait(
    harness: _Harness, agent_client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    submitted_waits: list[int] = []

    async def capture_request(*, req: SubmitToolCallRequest, actor: RuntimeActor) -> ToolCallRecord:
        assert actor.operator_id == harness.operator_identity.operator_id
        submitted_waits.append(req.wait_for_ms)
        raise ToolCallNotFoundError("captured request")

    monkeypatch.setattr(harness.tool_calls, "submit_and_wait", capture_request)
    with pytest.raises(ToolError, match="captured request"):
        await agent_client.call_tool(
            "gmail__drafts_create",
            {"input": {"to": ["a@b.test"], "subject": "s", "body": "b"}, "rationale": "test", "wait_for_result_ms": 0},
        )

    assert submitted_waits == [0]


async def test_get_tool_call_missing_raises(agent_client: Client) -> None:
    with pytest.raises(ToolError, match="not found"):
        await agent_client.call_tool("get_tool_call", {"tool_call_id": "tc_does_not_exist"})


async def test_request_tool_dispatches_valid_wait_without_clamping(
    harness: _Harness, agent_client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    submitted_waits: list[int] = []

    async def capture_request(*, req: SubmitToolCallRequest, actor: RuntimeActor) -> ToolCallRecord:
        assert actor.operator_id == harness.operator_identity.operator_id
        submitted_waits.append(req.wait_for_ms)
        raise ToolCallNotFoundError("captured request")

    monkeypatch.setattr(harness.tool_calls, "submit_and_wait", capture_request)
    expected_waits = [0, harness.max_wait_for_result_ms]
    for wait_ms in expected_waits:
        with pytest.raises(ToolError, match="captured request"):
            await agent_client.call_tool(
                "gmail__drafts_create",
                {
                    "input": {"to": ["a@b.test"], "subject": "s", "body": "b"},
                    "rationale": "test",
                    "wait_for_result_ms": wait_ms,
                },
            )
    assert submitted_waits == expected_waits


async def test_two_operator_two_agent_mcp_read_matrix(harness: _Harness) -> None:
    async def submit_draft(token: str, subject: str) -> str:
        async with Client(f"{harness.base}/mcp", auth=token) as client:
            result = await client.call_tool(
                "gmail__drafts_create",
                {
                    "input": {"to": ["a@b.test"], "subject": subject, "body": "body"},
                    "rationale": "test agent read isolation",
                    "wait_for_result_ms": 0,
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
            assert [view["tool_call_id"] for view in listed.structured_content["result"]] == [own_call_id]
            own = await client.call_tool("get_tool_call", {"tool_call_id": own_call_id})
            assert own.structured_content is not None
            assert own.structured_content["tool_call_id"] == own_call_id
            for foreign_call_id in set(call_ids) - {own_call_id}:
                with pytest.raises(ToolError, match="not found"):
                    await client.call_tool("get_tool_call", {"tool_call_id": foreign_call_id})

    operator_response = await _operator_get(harness, "/api/tool-calls")
    assert operator_response.status_code == 200, operator_response.text
    other_response = await _operator_get(harness, "/api/tool-calls", identity=harness.other_operator_identity)
    assert other_response.status_code == 200, other_response.text
    assert [call["tool_call_id"] for call in operator_response.json()["tool_calls"]] == call_ids[:2]
    assert [call["tool_call_id"] for call in other_response.json()["tool_calls"]] == call_ids[2:]


async def _submit_pending_draft(client: Client, subject: str = "s") -> str:
    result = await client.call_tool(
        "gmail__drafts_create",
        {"input": {"to": ["a@b.test"], "subject": subject, "body": "b"}, "rationale": "test", "wait_for_result_ms": 0},
    )
    assert result.structured_content is not None
    return str(result.structured_content["tool_call_id"])


async def test_withdraw_tool_call_retracts_a_pending_stub(agent_client: Client) -> None:
    tool_call_id = await _submit_pending_draft(agent_client)

    result = await agent_client.call_tool(
        "withdraw_tool_call", {"tool_call_id": tool_call_id, "reason": "superseded by a corrected draft"}
    )

    view = result.structured_content
    assert view is not None
    assert view["call"]["status"] == ToolCallStatus.WITHDRAWN
    assert view["call"]["withdrawal_reason"] == "superseded by a corrected draft"
    assert view["url"] == f"https://haku.test/_console/tool-calls/{tool_call_id}"

    # The durable row is what the agent re-reads, so the retraction has to be visible there too.
    got = await agent_client.call_tool("get_tool_call", {"tool_call_id": tool_call_id})
    assert got.structured_content is not None
    assert got.structured_content["status"] == ToolCallStatus.WITHDRAWN


async def test_withdraw_tool_call_is_advertised_as_a_mutation(agent_client: Client) -> None:
    """Guards against `withdraw_tool_call` being copy-pasted onto `_READ_ONLY_META`, which would let
    clients treat a ledger write as a free read."""
    tools = {tool.name: tool for tool in await agent_client.list_tools()}

    annotations = tools["withdraw_tool_call"].annotations
    assert annotations is not None
    assert annotations.readOnlyHint is False
    assert annotations.openWorldHint is False
    read_tool_annotations = tools["get_tool_call"].annotations
    assert read_tool_annotations is not None
    assert read_tool_annotations.readOnlyHint is True


async def test_call_mcp_tool_dispatches_an_auto_approved_read(harness: _Harness, agent_client: Client) -> None:
    """The fallback reaches a tool the policy auto-approves and returns the real upstream result,
    audited against the named server/tool rather than as a call to `call_mcp_tool` itself."""
    result = await agent_client.call_tool("call_mcp_tool", {"server_id": "gmail", "tool_name": "labels_list"})

    assert result.structured_content is not None
    assert result.structured_content["labels"][0]["name"] == "haku/triaged"
    assert result.meta is not None
    response = await _operator_get(harness, f"/api/tool-calls/{result.meta[MCP_TOOL_CALL_META_KEY]['tool_call_id']}")
    assert response.status_code == 200, response.text
    call = response.json()
    assert call["status"] == ToolCallStatus.OK
    assert (call["server_id"], call["tool_name"]) == ("gmail", "labels_list")


async def test_call_mcp_tool_queues_what_the_named_tool_would_queue(harness: _Harness, agent_client: Client) -> None:
    """The security property: naming a tool through the fallback must not escape approval. The same
    manual call that returns a stub through `gmail__drafts_create` returns one here."""
    result = await agent_client.call_tool(
        "call_mcp_tool",
        {
            "server_id": "gmail",
            "tool_name": "drafts_create",
            # Byte-identical to what `gmail__drafts_create` takes, because that is the contract.
            "arguments": {
                "input": {"to": ["a@b.test"], "subject": "s", "body": "b"},
                "rationale": "drafting the reply",
                "wait_for_result_ms": 0,
            },
        },
    )

    stub = result.structured_content
    assert stub is not None
    assert stub["status"] == ToolCallStatus.PENDING_APPROVAL
    pending = await _operator_get(harness, "/api/approvals/pending")
    assert pending.status_code == 200, pending.text
    queued = pending.json()["approvals"]
    assert len(queued) == 1
    assert (queued[0]["server_id"], queued[0]["tool_name"]) == ("gmail", "drafts_create")
    # The operator sees the requester's own words, not a generic "called via the fallback".
    assert queued[0]["rationale"] == "drafting the reply"


async def test_call_mcp_tool_still_enforces_the_tool_schema(harness: _Harness, agent_client: Client) -> None:
    """A free-form `arguments` object must not become a way past the schema check that makes a bad
    call born-denied — otherwise the fallback would queue work that can never execute."""
    with pytest.raises(ToolError, match="single_events"):
        await agent_client.call_tool(
            "call_mcp_tool",
            # `list_events` is auto-approved, so it takes raw arguments here just as it does
            # through `google_calendar__list_events`.
            {"server_id": "google_calendar", "tool_name": "list_events", "arguments": {"single_events": True}},
        )

    response = await _operator_get(harness, "/api/tool-calls")
    assert response.status_code == 200, response.text
    calls = response.json()["tool_calls"]
    assert len(calls) == 1
    assert calls[0]["status"] == ToolCallStatus.DENIED
    pending = await _operator_get(harness, "/api/approvals/pending")
    assert pending.json()["approvals"] == []


async def test_call_mcp_tool_rejects_an_unknown_server(harness: _Harness, agent_client: Client) -> None:
    with pytest.raises(ToolError, match="unknown configured MCP server"):
        await agent_client.call_tool("call_mcp_tool", {"server_id": "not_a_server", "tool_name": "whatever"})

    # A rejected server name never reaches the ledger, so it cannot spend operator attention.
    response = await _operator_get(harness, "/api/tool-calls")
    assert response.json()["tool_calls"] == []


async def test_reflected_schema_is_the_one_call_mcp_tool_accepts(agent_client: Client, harness: _Harness) -> None:
    """The round trip the pair exists for: reflect a tool you cannot see, send back exactly the
    shape reported, get the real behaviour. If these two ever disagree the fallback is unusable,
    because a caller with no generated tool has nothing else to learn the shape from."""
    status = await agent_client.call_tool("get_mcp_server_status", {"server_id": "gmail", "include_tool_schemas": True})
    assert status.structured_content is not None
    tools = {tool["name"]: tool for tool in status.structured_content["server"]["state"]["tools"]}

    # An auto-approved read reports raw upstream arguments...
    assert tools["labels_list"]["approval_mode"] == "passthrough"
    assert "input" not in tools["labels_list"]["input_schema"].get("properties", {})
    # ...and a write reports the envelope, with the upstream schema nested under `input`.
    create = tools["drafts_create"]
    assert create["approval_mode"] == "approval_required"
    assert set(create["input_schema"]["required"]) == {"input", "rationale"}
    assert "subject" in create["input_schema"]["properties"]["input"]["properties"]
    reflected_wait = create["input_schema"]["properties"]["wait_for_result_ms"]
    assert reflected_wait["default"] == mcp_server_module.DEFAULT_WAIT_MS
    assert reflected_wait["minimum"] == 0
    assert reflected_wait["maximum"] == harness.max_wait_for_result_ms
    assert "anyOf" not in reflected_wait

    # Now send each reported shape back through the fallback.
    read = await agent_client.call_tool("call_mcp_tool", {"server_id": "gmail", "tool_name": "labels_list"})
    assert read.structured_content is not None
    assert read.structured_content["labels"][0]["name"] == "haku/triaged"
    write = await agent_client.call_tool(
        "call_mcp_tool",
        {
            "server_id": "gmail",
            "tool_name": "drafts_create",
            "arguments": {"input": {"to": ["a@b.test"], "subject": "s", "body": "b"}, "rationale": "r"},
        },
    )
    assert write.structured_content is not None
    assert write.structured_content["status"] == ToolCallStatus.PENDING_APPROVAL


async def test_call_mcp_tool_names_the_shape_it_wanted(agent_client: Client) -> None:
    """A caller naming a tool by hand can get the shape wrong in a way a generated tool's schema
    would have prevented, so the error has to say which shape was expected rather than surfacing a
    bare pydantic complaint about a missing `input` field."""
    with pytest.raises(ToolError, match="requires operator approval"):
        await agent_client.call_tool(
            "call_mcp_tool",
            {
                "server_id": "gmail",
                "tool_name": "drafts_create",
                "arguments": {"to": ["a@b.test"], "subject": "s", "body": "b"},
            },
        )


async def test_call_mcp_tool_is_advertised_as_an_open_world_call(agent_client: Client) -> None:
    """It can reach any tool on any server, so it must not be annotated read-only or closed-world
    the way the console-native reads are."""
    tools = {tool.name: tool for tool in await agent_client.list_tools()}

    annotations = tools["call_mcp_tool"].annotations
    assert annotations is not None
    assert annotations.readOnlyHint is False
    assert annotations.openWorldHint is True


async def test_withdraw_tool_call_after_approval_reports_the_real_status(
    harness: _Harness, agent_client: Client
) -> None:
    tool_call_id = await _submit_pending_draft(agent_client)
    response = await _operator_post(harness, f"/api/tool-calls/{tool_call_id}/decision", json={"decision": "approve"})
    assert response.status_code == 200, response.text

    # Withdrawal never stops an approved call; the agent is told the real status and reads the
    # outcome with get_tool_call instead.
    with pytest.raises(ToolError, match="not pending approval"):
        await agent_client.call_tool("withdraw_tool_call", {"tool_call_id": tool_call_id, "reason": "too late"})


async def test_withdraw_tool_call_rejects_another_agents_call(harness: _Harness) -> None:
    agents = (_AGENT_TOKEN, _SIBLING_AGENT_TOKEN, _OTHER_AGENT_TOKEN, _OTHER_SIBLING_AGENT_TOKEN)
    call_ids = []
    for token in agents:
        async with Client(f"{harness.base}/mcp", auth=token) as client:
            call_ids.append(await _submit_pending_draft(client, subject=token))

    for token, own_call_id in zip(agents, call_ids, strict=True):
        async with Client(f"{harness.base}/mcp", auth=token) as client:
            # A sibling agent under the same operator is as foreign as another operator's agent:
            # neither gets an existence oracle for a queue that isn't theirs.
            for foreign_call_id in set(call_ids) - {own_call_id}:
                with pytest.raises(ToolError, match="not found"):
                    await client.call_tool(
                        "withdraw_tool_call", {"tool_call_id": foreign_call_id, "reason": "not mine"}
                    )
            withdrawn = await client.call_tool("withdraw_tool_call", {"tool_call_id": own_call_id})
            assert withdrawn.structured_content is not None
            assert withdrawn.structured_content["call"]["status"] == ToolCallStatus.WITHDRAWN


def test_withdrawn_record_is_not_reported_as_a_stub() -> None:
    """A terminal record must never render as "still pending" — the stub is a named arm of
    `_record_to_result`, not its fallback."""
    record = ToolCallRecord(
        tool_call_id="tc_withdrawn",
        server_id="gmail",
        tool_name="drafts_create",
        caller=AgentToolCallCaller(agent_id=uuid4(), display_name="Haku"),
        status=ToolCallStatus.WITHDRAWN,
        created_at=datetime.datetime.now(datetime.UTC),
        updated_at=datetime.datetime.now(datetime.UTC),
        arguments={},
        withdrawal_reason="superseded",
    )

    # Pure result-mapping: `console_settings` only builds the model, so no database is contacted.
    result = mcp_server_module._record_to_result(record, console_settings("postgresql://unused/record-to-result"))

    assert result.is_error is True
    assert result.structured_content is None
    block = result.content[0]
    assert isinstance(block, TextContent)
    assert "withdrawn: superseded" in block.text


def test_running_record_is_reported_as_a_non_terminal_stub() -> None:
    record = ToolCallRecord(
        tool_call_id="tc_running",
        server_id="gmail",
        tool_name="drafts_create",
        caller=AgentToolCallCaller(agent_id=uuid4(), display_name="Haku"),
        status=ToolCallStatus.RUNNING,
        created_at=datetime.datetime.now(datetime.UTC),
        updated_at=datetime.datetime.now(datetime.UTC),
        arguments={},
    )

    result = mcp_server_module._record_to_result(record, console_settings("postgresql://unused/record-to-result"))

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["status"] == ToolCallStatus.RUNNING
    assert "approved" in result.structured_content["message"]
    assert "execution continues in the background" in result.structured_content["message"].lower()


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
    return _write_console_config(
        tmp_path / "console.yaml",
        {
            "static_agents": _STATIC_AGENTS,
            "mcp": {
                "servers": {"standin": {"id": "standin", "backend": _remote_backend(upstream_url, {"kind": "none"})}}
            },
        },
    )


async def test_e2e_request_approve_execute_over_http(migrated_db_url: str, migrated_sessions, tmp_path: Path) -> None:
    with _serve_upstream() as upstream_url:
        console_port = pick_free_port()
        settings = console_settings(
            migrated_db_url,
            config_file=_console_config(tmp_path, upstream_url),
            public_base_url=f"http://127.0.0.1:{console_port}",
        )
        app = create_app(settings)
        operator_identity = await resolve_operator_identity(
            migrated_sessions, issuer=settings.operator_oidc.issuer, subject="42"
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

            # The agent (bearer) sees the upstream tool behind the approval envelope and gets a stub.
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
                # Proxied tools declare no output schema: the result-or-stub union can't be
                # modeled as a conformant outputSchema (claude.ai requires type == "object";
                # anthropics/claude-ai-mcp#400), and outputSchema is optional. The stub behavior is
                # described in the tool description, not its output schema.
                assert tools["standin__echo"].outputSchema is None
                _assert_valid_json_schema(tools["standin__echo"].inputSchema)
                result = await client.call_tool(
                    "standin__echo", {"input": {"text": "hi"}, "rationale": "e2e", "wait_for_result_ms": 0}
                )
                assert result.structured_content is not None
                assert result.structured_content["status"] == ToolCallStatus.PENDING_APPROVAL
                tool_call_id = result.structured_content["tool_call_id"]

            # The Operator uses the upstream tool's native shape and the exact-Origin-gated MCP
            # request executes directly, without entering the approval queue.
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
                    "params": {"name": "standin__echo", "arguments": {"text": "operator"}},
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

                listed = await operator.get("/api/tool-calls")
                assert listed.status_code == 200, listed.text
                assert [call["tool_call_id"] for call in listed.json()["tool_calls"]] == [tool_call_id]

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

            # The agent resolves its stub; execution runs in the background on the server loop, so
            # poll get_tool_call until it terminalizes, then check the real upstream result.
            terminal = {ToolCallStatus.OK, ToolCallStatus.ERROR, ToolCallStatus.DENIED}
            async with Client(f"{base}/mcp", auth=_AGENT_TOKEN) as client:
                for _ in range(100):
                    got = await client.call_tool("get_tool_call", {"tool_call_id": tool_call_id})
                    assert got.structured_content is not None
                    if got.structured_content["status"] in terminal:
                        break
                    await asyncio.sleep(0.02)
            assert got.structured_content["status"] == ToolCallStatus.OK
            assert "echo:hi" in str(got.structured_content["result"])


async def test_tool_surface_tracks_each_operators_connected_servers(
    migrated_db_url: str, migrated_sessions, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _serve_upstream() as upstream_url:
        config_file = _write_console_config(
            tmp_path / "operator-tools.yaml",
            {
                "static_agents": _STATIC_AGENTS,
                "mcp": {
                    "servers": {
                        "standin": {"id": "standin", "backend": _remote_backend(upstream_url, _dynamic_remote_oauth())}
                    }
                },
            },
        )
        settings = console_settings(migrated_db_url, config_file=config_file)
        app = create_app(settings)
        operator_identity = await resolve_operator_identity(
            migrated_sessions, issuer=settings.operator_oidc.issuer, subject="42"
        )
        other_operator_identity = await resolve_operator_identity(
            migrated_sessions, issuer=settings.operator_oidc.issuer, subject="99"
        )
        connected = {operator_identity.operator_id}
        other_operator_id = other_operator_identity.operator_id

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
            await asyncio.gather(
                app.state.mcp_catalogs.refresh_operator(operator_identity.operator_id),
                app.state.mcp_catalogs.refresh_operator(other_operator_id),
            )

            async with Client(f"{base}/mcp", auth=_AGENT_TOKEN) as client:
                assert "standin__echo" not in {tool.name for tool in await client.list_tools()}
            async with Client(f"{base}/mcp", auth=_OTHER_AGENT_TOKEN) as client:
                assert "standin__echo" in {tool.name for tool in await client.list_tools()}


async def test_list_mcp_servers_passively_reports_persisted_connection_state(
    migrated_db_url: str, tmp_path: Path
) -> None:
    config_file = _write_console_config(
        tmp_path / "connection-status.yaml",
        {
            "static_agents": _STATIC_AGENTS,
            "operator_connection_providers": {
                "google": {"kind": "google", "client_id": "google-client", "client_secret": "google-secret"}
            },
            "operator_connections": {
                "google_workspace": {"display_name": "Google Workspace", "provider": "google", "scopes": ["scope"]}
            },
            "mcp": {
                "servers": {
                    "expired_remote": {
                        "id": "expired-remote",
                        "backend": _remote_backend(
                            "https://must-not-be-contacted.invalid/mcp", _dynamic_remote_oauth()
                        ),
                    },
                    "unconnected_remote": {
                        "id": "unconnected-remote",
                        "backend": _remote_backend(
                            "https://also-must-not-be-contacted.invalid/mcp", _dynamic_remote_oauth()
                        ),
                    },
                    "preregistered_remote": {
                        "id": "preregistered-remote",
                        "backend": _remote_backend(
                            "https://preregistered.invalid/mcp",
                            {
                                "kind": "remote_server_oauth",
                                "client_registration": {
                                    "kind": "preregistered",
                                    "client_id": "must-not-be-reflected",
                                    "client_secret": "must-not-be-reflected",
                                    "token_endpoint_auth_method": "client_secret_post",
                                },
                            },
                        ),
                    },
                    "gmail": {
                        "id": "gmail",
                        "backend": _in_process_backend(
                            {"kind": "operator_connection", "connection": "google_workspace"}
                        ),
                    },
                    "routine": {"id": "routine", "backend": _in_process_backend({"kind": "none"})},
                    "static_remote": {
                        "id": "static-remote",
                        "backend": _remote_backend(
                            "https://static.invalid/mcp", {"kind": "static_bearer", "token": "static-remote-token"}
                        ),
                    },
                }
            },
        },
    )
    settings = console_settings(migrated_db_url, config_file=config_file)
    expires_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=1)
    connected_at = expires_at - datetime.timedelta(days=1)
    oauth_statuses = AsyncMock(
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
    provider_statuses = AsyncMock(
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
    dispatcher = Mock(metadata=fetch_metadata)
    context = mcp_server_module.ConsoleMcpContext(
        settings=settings,
        tool_calls=Mock(),
        oauth_store=oauth_store,
        provider_store=provider_store,
        dispatcher=dispatcher,
        catalogs=Mock(),
    )
    actor = AgentActor(agent_id=UUID(int=1), operator_id=UUID(int=2), binding_id=UUID(int=3))

    response = await mcp_server_module._passive_server_connection_statuses(context, actor)

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
    assert statuses["preregistered-remote"].model_dump(mode="json") == {
        "server_id": "preregistered-remote",
        "backend": {
            "kind": "remote_mcp",
            "url": "https://preregistered.invalid/mcp",
            "auth": {
                "kind": "remote_server_oauth",
                "client_registration": {"kind": "preregistered", "token_endpoint_auth_method": "client_secret_post"},
                "scopes": None,
            },
        },
        "connection": None,
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
    assert '"client_id":' not in serialized
    assert '"client_secret":' not in serialized
    assert "must-not-be-reflected" not in serialized
    assert "static-remote-token" not in serialized
    oauth_statuses.assert_called_once()
    provider_statuses.assert_called_once()
    refresh_remote.assert_not_awaited()
    refresh_provider.assert_not_awaited()
    fetch_metadata.assert_not_awaited()


async def test_get_mcp_server_status_reports_refresh_failure_as_degraded(
    migrated_db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_console_config(
        tmp_path / "refresh-failure.yaml",
        {
            "static_agents": _STATIC_AGENTS,
            "mcp": {
                "servers": {
                    "standin": {
                        "id": "standin",
                        "backend": _remote_backend("https://standin.invalid/mcp", _dynamic_remote_oauth()),
                    }
                }
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
    config_file = _write_console_config(
        tmp_path / "unprovisioned-provider.yaml",
        {
            "static_agents": _STATIC_AGENTS,
            "operator_connection_providers": {"google_calendar": {"kind": "google"}},
            "operator_connections": {
                "google_calendar": {
                    "display_name": "Google Calendar",
                    "provider": "google_calendar",
                    "scopes": ["https://www.googleapis.com/auth/calendar.events"],
                }
            },
            "mcp": {
                "servers": {
                    "google_calendar": {
                        "id": "google_calendar",
                        "backend": _in_process_backend(
                            {"kind": "operator_connection", "connection": "google_calendar"}
                        ),
                    }
                }
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
    config_file = _write_console_config(
        tmp_path / "schema-detail.yaml",
        {
            "static_agents": _STATIC_AGENTS,
            "mcp": {
                "servers": {
                    "standin": {
                        "id": "standin",
                        "backend": _remote_backend("https://standin.invalid/mcp", {"kind": "none"}),
                    }
                }
            },
        },
    )
    app = create_app(console_settings(migrated_db_url, config_file=config_file))

    async def metadata_for_operator(**kwargs: Any) -> ReflectedCatalog:
        return ReflectedCatalog(
            tools=[
                Tool(
                    name="echo",
                    description="Echo input",
                    inputSchema={"type": "object"},
                    outputSchema={"type": "object", "properties": {"echoed": {"type": "string"}}},
                )
            ],
            instructions="Echo server: send text, get it back.",
        )

    monkeypatch.setattr(mcp_catalog_reconciler_module, "metadata_for_operator", metadata_for_operator)
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
        # No policy auto-approves `standin`, so this tool is reported as taking the envelope even
        # though its own schema is bare — that is the shape a caller must actually send.
        "approval_mode": "approval_required",
        "annotations": None,
        "icons": None,
    }
    # The server's own `initialize` guidance passes through instead of being dropped at the proxy.
    assert summary.structured_content["server"]["state"]["instructions"] == "Echo server: send text, get it back."
    exposed = detailed.structured_content["server"]["state"]["tools"][0]["input_schema"]
    assert exposed["properties"]["input"] == {"type": "object"}
    assert set(exposed["required"]) == {"input", "rationale"}
    assert detailed.structured_content["server"]["state"]["tools"][0]["output_schema"] == {
        "type": "object",
        "properties": {"echoed": {"type": "string"}},
    }


async def test_tool_discovery_is_concurrent_and_preserves_config_order(
    migrated_db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_console_config(
        tmp_path / "concurrent-tools.yaml",
        {
            "static_agents": _STATIC_AGENTS,
            "mcp": {
                "servers": {
                    "beta": {"id": "beta", "backend": _remote_backend("https://beta.invalid/mcp", {"kind": "none"})},
                    "alpha": {"id": "alpha", "backend": _remote_backend("https://alpha.invalid/mcp", {"kind": "none"})},
                }
            },
        },
    )
    app = create_app(console_settings(migrated_db_url, config_file=config_file))
    started: set[str] = set()
    both_started = asyncio.Event()

    async def metadata_for_operator(**kwargs: Any) -> ReflectedCatalog:
        server_id = str(kwargs["server"].id)
        started.add(server_id)
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)
        if server_id == "beta":
            await asyncio.sleep(0.01)
        return ReflectedCatalog(tools=[Tool(name="echo", inputSchema={"type": "object"})])

    monkeypatch.setattr(mcp_catalog_reconciler_module, "metadata_for_operator", metadata_for_operator)
    with serve_app_sync(app) as base:
        async with Client(f"{base}/mcp", auth=_AGENT_TOKEN) as client:
            proxy_names = [tool.name for tool in await client.list_tools() if tool.name.endswith("__echo")]

    assert proxy_names == ["beta__echo", "alpha__echo"]


async def test_tool_discovery_isolates_unexpected_server_failure(
    migrated_db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    config_file = _write_console_config(
        tmp_path / "isolated-tools.yaml",
        {
            "static_agents": _STATIC_AGENTS,
            "mcp": {
                "servers": {
                    "broken": {
                        "id": "broken",
                        "backend": _remote_backend("https://broken.invalid/mcp", {"kind": "none"}),
                    },
                    "healthy": {
                        "id": "healthy",
                        "backend": _remote_backend("https://healthy.invalid/mcp", {"kind": "none"}),
                    },
                }
            },
        },
    )
    app = create_app(console_settings(migrated_db_url, config_file=config_file))

    async def metadata_for_operator(**kwargs: Any) -> ReflectedCatalog:
        server_id = str(kwargs["server"].id)
        if server_id == "broken":
            raise RuntimeError("unexpected reflection failure")
        return ReflectedCatalog(tools=[Tool(name="echo", inputSchema={"type": "object"})])

    monkeypatch.setattr(mcp_catalog_reconciler_module, "metadata_for_operator", metadata_for_operator)
    with serve_app_sync(app) as base:
        async with Client(f"{base}/mcp", auth=_AGENT_TOKEN) as client:
            proxy_names = [tool.name for tool in await client.list_tools() if tool.name.endswith("__echo")]

    assert proxy_names == ["healthy__echo"]
    assert "catalog reconciliation failed for server broken" in caplog.text
    assert "unexpected reflection failure" in caplog.text


async def test_tool_dispatch_reads_only_target_server_snapshot(migrated_db_url: str, tmp_path: Path) -> None:
    config_file = _write_console_config(
        tmp_path / "targeted-dispatch.yaml",
        {
            "static_agents": _STATIC_AGENTS,
            "mcp": {
                "servers": {
                    "alpha": {"id": "alpha", "backend": _remote_backend("https://alpha.invalid/mcp", {"kind": "none"})},
                    "beta": {"id": "beta", "backend": _remote_backend("https://beta.invalid/mcp", {"kind": "none"})},
                }
            },
        },
    )
    settings = console_settings(migrated_db_url, config_file=config_file)
    app = create_app(settings)
    reflected: list[str] = []

    def metadata(*, operator_id: UUID, server: Any) -> ReflectedCatalog:
        _ = operator_id
        server_id = str(server.id)
        reflected.append(server_id)
        return ReflectedCatalog(tools=[Tool(name="echo", inputSchema={"type": "object"})])

    catalogs = Mock()
    catalogs.metadata = Mock(side_effect=metadata)
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
            dispatcher=app.state.mcp_dispatcher,
            catalogs=catalogs,
        ),
        actor_resolver,
    )

    tool = await provider._get_tool("beta__echo")

    assert isinstance(tool, mcp_server_module.ProxyTool)
    assert tool.name == "beta__echo"
    assert tool.actor == actor
    assert reflected == ["beta"]


async def test_operator_proxy_advertises_and_dispatches_native_arguments(migrated_db_url: str, tmp_path: Path) -> None:
    config_file = _write_console_config(
        tmp_path / "operator-input-shape.yaml",
        {
            "static_agents": _STATIC_AGENTS,
            "mcp": {
                "servers": {
                    "beta": {"id": "beta", "backend": _remote_backend("https://beta.invalid/mcp", {"kind": "none"})}
                }
            },
        },
    )
    settings = console_settings(migrated_db_url, config_file=config_file)
    app = create_app(settings)
    execute_direct = AsyncMock(return_value={"content": [{"type": "text", "text": "listed"}]})
    app.state.tool_call_service.execute_direct = execute_direct
    catalogs = Mock()
    catalogs.metadata.return_value = ReflectedCatalog(
        tools=[
            Tool(
                name="list_active",
                description="List active sessions",
                inputSchema={"type": "object", "properties": {"limit": {"type": "integer"}}},
            )
        ]
    )
    actor = OperatorActor(operator_id=UUID("10000000-0000-4000-8000-000000000001"))
    actor_resolver = Mock(spec=mcp_server_module.HakuMcpActorResolver)
    actor_resolver.resolve = AsyncMock(return_value=actor)
    provider = mcp_server_module.OperatorToolProvider(
        mcp_server_module.ConsoleMcpContext(
            settings=settings,
            tool_calls=app.state.tool_call_service,
            oauth_store=app.state.mcp_operator_oauth_store,
            provider_store=app.state.provider_connection_store,
            dispatcher=app.state.mcp_dispatcher,
            catalogs=catalogs,
        ),
        actor_resolver,
    )

    tool = await provider._get_tool("beta__list_active")

    assert isinstance(tool, mcp_server_module.ProxyTool)
    advertised = tool.to_mcp_tool()
    assert advertised.inputSchema == {"type": "object", "properties": {"limit": {"type": "integer"}}}
    result = await tool.run({"limit": 100})
    assert isinstance(result.content[0], TextContent)
    assert result.content[0].text == "listed"
    call = execute_direct.await_args
    assert call is not None
    request = call.kwargs["req"]
    assert request.arguments == {"limit": 100}
    assert call.kwargs["actor"] == actor


async def test_targeted_dispatch_reports_a_known_degraded_server(migrated_db_url: str, tmp_path: Path) -> None:
    config_file = _write_console_config(
        tmp_path / "degraded-dispatch.yaml",
        {
            "static_agents": _STATIC_AGENTS,
            "mcp": {
                "servers": {
                    "grocy_sf": {
                        "id": "grocy-sf",
                        "backend": {"kind": "remote_mcp", "url": "https://grocy.invalid/mcp", "auth": {"kind": "none"}},
                    }
                }
            },
        },
    )
    settings = console_settings(migrated_db_url, config_file=config_file)
    app = create_app(settings)

    catalogs = Mock()
    catalogs.metadata.return_value = DegradedReflection(
        failure_stage=ReflectionFailureStage.CREDENTIAL_RESOLUTION,
        degraded_reason="MCP OAuth token refresh failed: 401",
    )
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
            dispatcher=app.state.mcp_dispatcher,
            catalogs=catalogs,
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
    monkeypatch.setenv("HAKU_CONSOLE__MCP_OAUTH__OIDC_ISSUER", "https://auth.example.test/application/o/mcp/")
    monkeypatch.setenv("HAKU_CONSOLE__MCP_OAUTH__OIDC_CLIENT_ID", "console")
    monkeypatch.setenv("HAKU_CONSOLE__MCP_OAUTH__OIDC_CLIENT_SECRET", "secret")
    monkeypatch.setenv("HAKU_CONSOLE__MCP_OAUTH__PERSISTENCE__KIND", "postgres")
    monkeypatch.setenv("HAKU_CONSOLE__MCP_OAUTH__PERSISTENCE__URL", "postgresql://db.example.test/haku")

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
            public_base_url=f"http://127.0.0.1:{console_port}",
            mcp_oauth=McpOAuthConfig(
                oidc_issuer=oidc.issuer,
                oidc_client_id="console",
                oidc_client_secret=SecretStr("secret"),
                # Match production's shared Postgres-backed DCR/token state, but use this test's
                # isolated database instead of FastMCP's implicit process-global file store.
                persistence=PostgresPersistence(
                    kind="postgres",
                    url=make_url(migrated_db_url).set(drivername="postgresql").render_as_string(hide_password=False),
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
                        "access_profile_id": enrollment_data["default_access_profile_id"],
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
    config_file = _write_console_config(
        tmp_path / "duplicate-agent.yaml", {"static_agents": {**_STATIC_AGENTS, "duplicate": _STATIC_AGENTS["haku"]}}
    )
    with pytest.raises(ValidationError, match="duplicate static Agent id"):
        create_app(console_settings(migrated_db_url, config_file=config_file))


def test_missing_deploy_config_fails_startup(migrated_db_url: str) -> None:
    with pytest.raises(RuntimeError, match="config file does not exist"):
        console_settings(migrated_db_url, config_file=Path("/nonexistent/haku-console.yaml"))


def test_duplicate_mcp_server_ids_fail_config_validation() -> None:
    with pytest.raises(ValidationError, match="duplicate MCP server id 'grocy'"):
        ConsoleConfigFile.model_validate(
            _with_manual_authority(
                {
                    "mcp": {
                        "servers": {
                            "grocy_one": {"id": "grocy", "backend": _in_process_backend({"kind": "none"})},
                            "grocy_two": {"id": "grocy", "backend": _in_process_backend({"kind": "none"})},
                        }
                    }
                }
            )
        )


def test_duplicate_sanitized_mcp_server_prefixes_fail_config_validation() -> None:
    with pytest.raises(ValidationError, match="duplicate MCP server tool prefix 'grocy_sf'"):
        ConsoleConfigFile.model_validate(
            _with_manual_authority(
                {
                    "mcp": {
                        "servers": {
                            "grocy_hyphen": {"id": "grocy-sf", "backend": _in_process_backend({"kind": "none"})},
                            "grocy_underscore": {"id": "grocy_sf", "backend": _in_process_backend({"kind": "none"})},
                        }
                    }
                }
            )
        )


def test_duplicate_static_agent_tokens_fail_startup(
    migrated_db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_console_config(
        tmp_path / "duplicate-token.yaml",
        {
            "static_agents": {
                **_STATIC_AGENTS,
                "ops": {
                    "agent_id": "40000000-0000-4000-8000-000000000005",
                    "display_name": "Ops Bot",
                    "token": _AGENT_TOKEN,
                    "operator_subject": "99",
                    "access_profile_id": _MANUAL_ACCESS_PROFILE_ID,
                },
            }
        },
    )
    with pytest.raises(RuntimeError, match="duplicate static agent bearer tokens"):
        create_app(console_settings(migrated_db_url, config_file=config_file))


def test_agent_tool_denylist_applies_only_to_agents() -> None:
    server = McpServerEntry(
        id="github",
        backend=_in_process_backend({"kind": "none"}),
        agent_tool_denylist={"create_pull_request_with_copilot"},
    )
    agent = AgentActor(agent_id=UUID(int=1), operator_id=UUID(int=2), binding_id=UUID(int=3))
    operator = OperatorActor(operator_id=UUID(int=2))

    assert mcp_server_module._is_agent_tool_blocked(server, agent, "create_pull_request_with_copilot")
    assert not mcp_server_module._is_agent_tool_blocked(server, agent, "get_commit")
    assert not mcp_server_module._is_agent_tool_blocked(server, operator, "create_pull_request_with_copilot")


async def test_agent_tool_denylist_rejects_hand_built_dispatch(tmp_path: Path) -> None:
    config_file = _write_console_config(
        tmp_path / "denylist.yaml",
        {
            "mcp": {
                "servers": {
                    "github": {
                        "id": "github",
                        "backend": _in_process_backend({"kind": "none"}),
                        "agent_tool_denylist": ["create_pull_request_with_copilot"],
                    }
                }
            }
        },
    )
    context = Mock()
    context.settings = console_settings("postgresql://unused/denylist", config_file=config_file)
    agent = AgentActor(agent_id=UUID(int=1), operator_id=UUID(int=2), binding_id=UUID(int=3))

    with pytest.raises(ToolError, match="not available to Agents"):
        await mcp_server_module._dispatch(
            context,
            server_id="github",
            tool_name="create_pull_request_with_copilot",
            arguments={},
            passthrough=False,
            actor=agent,
        )


if __name__ == "__main__":
    pytest_bazel.main()
