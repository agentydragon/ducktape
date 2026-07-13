"""Tests for haku-console's own MCP server (the connected-server tool proxy)."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
import pytest_bazel
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from gmail_api.labels import GmailLabel, LabelsListResponse, LabelType
from haku.console.app import create_app
from haku.console.conftest import console_settings, write_config
from haku.console.mcp_approval import (
    McpMetadataProvider,
    McpToolExecutor,
    PostgresToolCallLedger,
    ToolCallEventHub,
    _agent_operator,
)
from haku.console.mcp_config import ResolvedStaticAgent
from haku.console.mcp_operator_oauth import PostgresMcpOperatorOAuthStore
from haku.console.mcp_server import ConsoleMcpContext, build_console_mcp, register_proxy_tools
from haku.console.tool_calls import ToolCallStatus
from haku.console.tools import gmail as gmail_tools
from mcp_infra.authentik_auth.auth import AuthentikAuthConfig
from util.net import pick_free_port
from util.testing.asgi import serve_app_sync, serve_fastmcp

# The `/mcp` static bearer used across these tests, and the static-agent config that binds it to the
# `haku` agent id (which acts as operator subject "42"). Env-referenced, like the deploy.
_AGENT_TOKEN = "agent-token"
_AGENT_TOKEN_ENV = "HAKU_CONSOLE_TEST_AGENT_TOKEN"
_AGENT_OPERATOR_ENV = "HAKU_CONSOLE_TEST_AGENT_OPERATOR"
_STATIC_AGENTS = [{"agent": "haku", "token_env_var": _AGENT_TOKEN_ENV, "operator_subject_env": _AGENT_OPERATOR_ENV}]


@pytest.fixture(autouse=True)
def _static_agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_AGENT_TOKEN_ENV, _AGENT_TOKEN)
    monkeypatch.setenv(_AGENT_OPERATOR_ENV, "42")


@dataclass
class _Harness:
    base: str  # base URL of the served console MCP; open `Client(f"{base}/mcp", auth=_AGENT_TOKEN)`
    ledger: PostgresToolCallLedger


@pytest.fixture
async def harness(migrated_db_url: str, tmp_path: Path) -> AsyncGenerator[_Harness]:
    gmail_client = Mock()
    gmail_client.labels_list.return_value = LabelsListResponse(
        labels=[GmailLabel(id="Label_1", name="haku/triaged", type=LabelType.USER)]
    )
    config_file = write_config(tmp_path / "console.yaml", {"mcp": {"servers": [{"id": "gmail"}]}})
    settings = console_settings(migrated_db_url, config_file=config_file, ui_base_url="https://haku.test")
    in_process = {gmail_tools.GMAIL_SERVER_ID: gmail_tools.build_mcp(gmail_client)}
    context = ConsoleMcpContext(
        settings=settings,
        static_agents=[ResolvedStaticAgent(agent="haku", token=SecretStr(_AGENT_TOKEN), operator_subject="42")],
        ledger=PostgresToolCallLedger(migrated_db_url),
        hub=ToolCallEventHub(migrated_db_url),
        executor=McpToolExecutor(in_process),
        oauth_store=PostgresMcpOperatorOAuthStore(migrated_db_url),
        metadata_provider=McpMetadataProvider(in_process),
        in_process_servers=in_process,
        gmail_client=gmail_client,
    )
    server = build_console_mcp(context)
    await register_proxy_tools(server, context)
    # Serve over HTTP: the in-memory transport can't carry the static bearer, and the proxy tools
    # need an authenticated caller (`_agent_id`). The verifier maps `agent-token` → client_id "haku".
    mcp_app = server.http_app(path="/")
    app = Starlette(routes=[Mount("/mcp", app=mcp_app)], lifespan=mcp_app.lifespan)
    with serve_app_sync(app) as base:
        yield _Harness(base=base, ledger=context.ledger)


async def test_tool_surface_splits_pass_through_and_request(harness: _Harness) -> None:
    async with Client(f"{harness.base}/mcp", auth=_AGENT_TOKEN) as client:
        tools = {t.name: t for t in await client.list_tools()}

    # Gmail reads are transparent pass-through: original name, no envelope nesting.
    assert "labels_list" in tools
    assert "input" not in tools["labels_list"].inputSchema.get("properties", {})
    # Gmail writes are approval-request tools with the envelope.
    assert "request_gmail_drafts_create" in tools
    envelope = tools["request_gmail_drafts_create"].inputSchema
    assert set(envelope["required"]) == {"input", "rationale"}
    assert set(envelope["properties"]) == {"input", "title", "rationale", "wait_for_approval_ms"}
    # The read tools are present.
    assert {"get_tool_call", "list_tool_calls"} <= tools.keys()
    # The promise preamble is in the request_ description.
    assert "operator-approval queue" in tools["request_gmail_drafts_create"].description


async def test_pass_through_read_auto_approves_and_returns_result(harness: _Harness) -> None:
    async with Client(f"{harness.base}/mcp", auth=_AGENT_TOKEN) as client:
        result = await client.call_tool("labels_list", {})

    assert result.structured_content is not None
    assert result.structured_content["labels"][0]["name"] == "haku/triaged"
    calls = harness.ledger.list().tool_calls
    assert len(calls) == 1
    assert calls[0].status == ToolCallStatus.OK
    assert calls[0].tool_name == "labels_list"
    # The pass-through call is audited as the static agent that presented the bearer.
    assert calls[0].caller_principal == "haku"


async def test_request_tool_returns_promise_with_deep_link(harness: _Harness) -> None:
    async with Client(f"{harness.base}/mcp", auth=_AGENT_TOKEN) as client:
        result = await client.call_tool(
            "request_gmail_drafts_create",
            {
                "input": {"to": ["a@b.test"], "subject": "s", "body": "b"},
                "rationale": "test",
                "wait_for_approval_ms": 0,
            },
        )
        promise = result.structured_content
        assert promise is not None
        assert promise["status"] == ToolCallStatus.PENDING_APPROVAL
        tool_call_id = promise["tool_call_id"]
        assert tool_call_id.startswith("tc_")
        assert promise["url"] == f"https://haku.test/tool-calls/{tool_call_id}"

        got = await client.call_tool("get_tool_call", {"tool_call_id": tool_call_id})
        view = got.structured_content
        assert view is not None
        assert view["call"]["status"] == ToolCallStatus.PENDING_APPROVAL
        assert view["call"]["tool_name"] == "drafts_create"
        assert view["url"] == f"https://haku.test/tool-calls/{tool_call_id}"


async def test_get_tool_call_missing_raises(harness: _Harness) -> None:
    async with Client(f"{harness.base}/mcp", auth=_AGENT_TOKEN) as client:
        with pytest.raises(ToolError, match="not found"):
            await client.call_tool("get_tool_call", {"tool_call_id": "tc_does_not_exist"})


# ── End-to-end: real upstream MCP server + console served over HTTP + real Postgres ──────────


@contextmanager
def _serve_upstream() -> Generator[str]:
    """A real upstream MCP server process stand-in (echo tool) served over streamable HTTP."""
    upstream: FastMCP = FastMCP("standin")

    @upstream.tool
    async def echo(text: str) -> str:
        """Echo a string back."""
        return f"echo:{text}"

    with serve_fastmcp(upstream) as url:
        yield url


def _console_config(tmp_path: Path, upstream_url: str) -> Path:
    return write_config(
        tmp_path / "console.yaml",
        {"static_agents": _STATIC_AGENTS, "mcp": {"servers": [{"id": "standin", "server_url": upstream_url}]}},
    )


async def test_e2e_request_approve_execute_over_http(migrated_db_url: str, tmp_path: Path) -> None:
    with _serve_upstream() as upstream_url:
        settings = console_settings(
            migrated_db_url,
            config_file=_console_config(tmp_path, upstream_url),
            csrf_secret=SecretStr("csrf"),
            ui_base_url="https://haku.test",
        )
        with serve_app_sync(create_app(settings)) as base:
            async with httpx.AsyncClient() as anon:
                # No bearer -> unauthorized. Hit /mcp/ directly (the mount redirects /mcp -> /mcp/).
                unauth = await anon.post(
                    f"{base}/mcp/",
                    headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                )
            assert unauth.status_code == 401

            # The agent (bearer) sees the upstream tool as a request_ envelope and gets a promise.
            async with Client(f"{base}/mcp", auth=_AGENT_TOKEN) as client:
                tools = {t.name for t in await client.list_tools()}
                assert "request_standin_echo" in tools
                result = await client.call_tool(
                    "request_standin_echo", {"input": {"text": "hi"}, "rationale": "e2e", "wait_for_approval_ms": 0}
                )
                assert result.structured_content is not None
                assert result.structured_content["status"] == ToolCallStatus.PENDING_APPROVAL
                tool_call_id = result.structured_content["tool_call_id"]

            # The operator approves via the CSRF-gated decision endpoint -> the real upstream runs.
            async with httpx.AsyncClient(base_url=base) as operator:
                csrf = (await operator.get("/api/capabilities/csrf")).json()["csrf_token"]
                decided = await operator.post(
                    f"/api/tool-calls/{tool_call_id}/decision",
                    headers={"X-CSRF-Token": csrf},
                    json={"decision": "approve"},
                )
            assert decided.status_code == 200, decided.text
            assert decided.json()["tool_call"]["status"] == "ok"

            # The agent resolves its promise and sees the real upstream result.
            async with Client(f"{base}/mcp", auth=_AGENT_TOKEN) as client:
                got = await client.call_tool("get_tool_call", {"tool_call_id": tool_call_id})
            assert got.structured_content is not None
            assert got.structured_content["call"]["status"] == ToolCallStatus.OK
            assert "echo:hi" in str(got.structured_content["call"]["result"])


@contextmanager
def _serve_mock_oidc() -> Generator[str]:
    """A minimal OIDC discovery endpoint so `build_authentik_auth` can construct the OIDCProxy."""

    async def discovery(request: Request) -> JSONResponse:
        base = str(request.base_url).rstrip("/")
        return JSONResponse(
            {
                "issuer": base,
                "authorization_endpoint": f"{base}/authorize",
                "token_endpoint": f"{base}/token",
                "jwks_uri": f"{base}/jwks",
                "userinfo_endpoint": f"{base}/userinfo",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "subject_types_supported": ["public"],
                "id_token_signing_alg_values_supported": ["RS256"],
                "code_challenge_methods_supported": ["S256"],
                "scopes_supported": ["openid", "email", "profile", "offline_access"],
            }
        )

    async def jwks(request: Request) -> JSONResponse:
        return JSONResponse({"keys": []})

    app = Starlette(routes=[Route("/.well-known/openid-configuration", discovery), Route("/jwks", jwks)])
    with serve_app_sync(app) as base:
        yield base


async def test_oauth_composes_with_static_bearer(migrated_db_url: str, tmp_path: Path) -> None:
    with _serve_mock_oidc() as oidc_base, _serve_upstream() as upstream_url:
        # public_base_url is the console's own URL, so choose its port before building the app.
        console_port = pick_free_port()
        settings = console_settings(
            migrated_db_url,
            config_file=_console_config(tmp_path, upstream_url),
            csrf_secret=SecretStr("csrf"),
            ui_base_url="https://haku.test",
            mcp_oauth=AuthentikAuthConfig(
                oidc_issuer=oidc_base,
                oidc_client_id="console",
                oidc_client_secret="secret",
                public_base_url=f"http://127.0.0.1:{console_port}",
            ),
        )
        with serve_app_sync(create_app(settings), port=console_port) as base:
            # The static bearer still authenticates (MultiAuth composes OAuth + static).
            async with Client(f"{base}/mcp", auth=_AGENT_TOKEN) as client:
                assert "request_standin_echo" in {t.name for t in await client.list_tools()}

            # OAuth is advertised: an unauthenticated request is challenged with resource metadata.
            async with httpx.AsyncClient() as anon:
                unauth = await anon.post(
                    f"{base}/mcp/",
                    headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                )
                assert unauth.status_code == 401
                assert "resource_metadata" in unauth.headers.get("www-authenticate", "").lower()


def test_agent_operator_link_round_trips(migrated_db_url: str) -> None:
    store = PostgresMcpOperatorOAuthStore(migrated_db_url)
    assert store.agent_operator(agent_dcr_client_id="dcr-1") is None
    store.upsert_agent_operator(agent_dcr_client_id="dcr-1", operator_subject="42")
    assert store.agent_operator(agent_dcr_client_id="dcr-1") == "42"
    # Re-linking the same agent updates in place (idempotent reconnect) — no duplicate row.
    store.upsert_agent_operator(agent_dcr_client_id="dcr-1", operator_subject="99")
    assert store.agent_operator(agent_dcr_client_id="dcr-1") == "99"


def test_operator_resolution_routes_multiple_agents_and_operators(migrated_db_url: str) -> None:
    """Each caller resolves to its own operator subject with several static agents and OAuth links
    configured — no crosstalk between them."""
    store = PostgresMcpOperatorOAuthStore(migrated_db_url)
    # Two static agents, each bound (by explicit config) to a different operator subject.
    static_agents = [
        ResolvedStaticAgent(agent="haku", token=SecretStr("tok-haku"), operator_subject="op-haku"),
        ResolvedStaticAgent(agent="ops-bot", token=SecretStr("tok-ops"), operator_subject="op-ops"),
    ]
    # Two OAuth DCR agents, each auto-linked (at connect) to a different operator subject.
    store.upsert_agent_operator(agent_dcr_client_id="dcr-claude", operator_subject="op-claude")
    store.upsert_agent_operator(agent_dcr_client_id="dcr-cli", operator_subject="op-cli")

    # Static agents route by their config binding; the subjects never cross over.
    assert _agent_operator("haku", static_agents, store) == "op-haku"
    assert _agent_operator("ops-bot", static_agents, store) == "op-ops"
    # OAuth agents route by their linked operator.
    assert _agent_operator("dcr-claude", static_agents, store) == "op-claude"
    assert _agent_operator("dcr-cli", static_agents, store) == "op-cli"
    # An unknown/unlinked caller resolves to no operator (fails closed into the 409 connect path).
    assert _agent_operator("dcr-unlinked", static_agents, store) is None


if __name__ == "__main__":
    pytest_bazel.main()
