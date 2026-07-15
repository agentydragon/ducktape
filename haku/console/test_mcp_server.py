"""Tests for haku-console's own MCP server (the connected-server tool proxy)."""

from __future__ import annotations

import base64
import hashlib
import html
import re
import secrets
from collections.abc import AsyncGenerator, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID

import httpx
import pytest
import pytest_bazel
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import SecretStr, ValidationError

from gmail_api.labels import GmailLabel, LabelsListResponse, LabelType
from haku.console.app import create_app
from haku.console.config import McpOAuthConfig, OperatorOidcConfig
from haku.console.conftest import console_settings, operator_session_cookie, write_config
from haku.console.operator_identity import VerifiedExternalIdentity
from haku.console.tool_call_actor import OperatorActor, ToolCallActor
from haku.console.tool_call_service import ToolCallApplicationService, ToolCallNotFoundError
from haku.console.tool_calls import SubmitToolCallRequest, ToolCallRecord, ToolCallStatus
from haku.console.tools import gmail as gmail_tools, google_calendar as calendar_tools
from haku.console.tools.google_calendar_client import CalendarEvent
from mcp_infra.persistence import PostgresPersistence
from util.net import pick_free_port
from util.testing.asgi import serve_app_sync, serve_fastmcp
from util.testing.mock_oidc import build_mock_oidc_app, generate_rsa_keypair

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
    gmail_client.labels_list.return_value = LabelsListResponse(
        labels=[GmailLabel(id="Label_1", name="haku/triaged", type=LabelType.USER)]
    )
    calendar_client = Mock()
    calendar_client.get_event.return_value = CalendarEvent(
        event_id="series1", summary="Standup", recurrence=["RRULE:FREQ=WEEKLY"]
    )
    config_file = write_config(
        tmp_path / "console.yaml",
        {"static_agents": _STATIC_AGENTS, "mcp": {"servers": [{"id": "gmail"}, {"id": "google_calendar"}]}},
    )
    settings = console_settings(migrated_db_url, config_file=config_file, ui_base_url="https://haku.test")
    in_process = {
        gmail_tools.GMAIL_SERVER_ID: gmail_tools.build_mcp(gmail_client),
        calendar_tools.GOOGLE_CALENDAR_SERVER_ID: calendar_tools.build_mcp(calendar_client),
    }
    app = create_app(
        settings, gmail_client=gmail_client, calendar_client=calendar_client, in_process_servers=in_process
    )
    operator_id = app.state.operator_identity_store.resolve_configured_external_user_key("42")
    other_operator_id = app.state.operator_identity_store.resolve_configured_external_user_key("99")
    with serve_app_sync(app) as base:
        yield _Harness(
            base=base,
            tool_calls=app.state.tool_call_service,
            operator_id=operator_id,
            other_operator_id=other_operator_id,
        )


async def test_tool_surface_splits_pass_through_and_request(harness: _Harness) -> None:
    async with Client(f"{harness.base}/mcp", auth=_AGENT_TOKEN) as client:
        tools = {t.name: t for t in await client.list_tools()}

    # Gmail reads are transparent pass-through: server-prefixed name, no envelope nesting.
    assert "gmail_labels_list" in tools
    assert "input" not in tools["gmail_labels_list"].inputSchema.get("properties", {})
    # Gmail writes are approval-request tools with the envelope.
    assert "gmail_drafts_create" in tools
    envelope = tools["gmail_drafts_create"].inputSchema
    assert set(envelope["required"]) == {"input", "rationale"}
    assert set(envelope["properties"]) == {"input", "title", "rationale", "wait_for_approval_ms"}
    # The read tools are present.
    assert {"get_tool_call", "list_tool_calls"} <= tools.keys()
    assert "actor" not in tools["get_tool_call"].inputSchema.get("properties", {})
    assert "actor" not in tools["list_tool_calls"].inputSchema.get("properties", {})
    # The promise preamble is in the envelope tool's description.
    assert "operator-approval queue" in tools["gmail_drafts_create"].description
    # Calendar reads are transparent; creation is the approval-gated request tool. The server
    # prefix supplies "calendar", so no tool repeats it in the local name.
    assert "google_calendar_get_event" in tools
    assert "input" not in tools["google_calendar_get_event"].inputSchema.get("properties", {})
    assert "google_calendar_create_event" in tools
    assert "google_calendar_create_calendar_event" not in tools


async def test_pass_through_read_auto_approves_and_returns_result(harness: _Harness) -> None:
    async with Client(f"{harness.base}/mcp", auth=_AGENT_TOKEN) as client:
        result = await client.call_tool("gmail_labels_list", {})

    assert result.structured_content is not None
    assert result.structured_content["labels"][0]["name"] == "haku/triaged"
    calls = harness.tool_calls.list_tool_calls(actor=OperatorActor(operator_id=harness.operator_id))
    assert len(calls) == 1
    assert calls[0].status == ToolCallStatus.OK
    assert calls[0].tool_name == "labels_list"
    # The pass-through call is audited as the static agent that presented the bearer.
    assert calls[0].caller.kind == "agent"
    assert calls[0].caller.display_name == "Haku"


async def test_calendar_read_is_transparent_and_audited(harness: _Harness) -> None:
    async with Client(f"{harness.base}/mcp", auth=_AGENT_TOKEN) as client:
        result = await client.call_tool("google_calendar_get_event", {"event_id": "series1"})

    assert result.structured_content is not None
    assert result.structured_content["event_id"] == "series1"
    calls = harness.tool_calls.list_tool_calls(actor=OperatorActor(operator_id=harness.operator_id))
    assert len(calls) == 1
    assert calls[0].server_id == "google_calendar"
    assert calls[0].tool_name == "get_event"


async def test_request_tool_returns_promise_with_deep_link(harness: _Harness) -> None:
    async with Client(f"{harness.base}/mcp", auth=_AGENT_TOKEN) as client:
        result = await client.call_tool(
            "gmail_drafts_create",
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


async def test_request_tool_preserves_explicit_zero_wait(harness: _Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    submitted_waits: list[int] = []

    async def capture_request(*, req: SubmitToolCallRequest, actor: ToolCallActor) -> ToolCallRecord:
        assert actor.operator_id == harness.operator_id
        submitted_waits.append(req.wait_for_ms)
        raise ToolCallNotFoundError("captured request")

    monkeypatch.setattr(harness.tool_calls, "submit_and_wait", capture_request)
    async with Client(f"{harness.base}/mcp", auth=_AGENT_TOKEN) as client:
        with pytest.raises(ToolError, match="captured request"):
            await client.call_tool(
                "gmail_drafts_create",
                {
                    "input": {"to": ["a@b.test"], "subject": "s", "body": "b"},
                    "rationale": "test",
                    "wait_for_approval_ms": 0,
                },
            )

    assert submitted_waits == [0]


async def test_get_tool_call_missing_raises(harness: _Harness) -> None:
    async with Client(f"{harness.base}/mcp", auth=_AGENT_TOKEN) as client:
        with pytest.raises(ToolError, match="not found"):
            await client.call_tool("get_tool_call", {"tool_call_id": "tc_does_not_exist"})


async def test_two_operator_two_agent_mcp_read_matrix(harness: _Harness) -> None:
    async def submit_draft(token: str, subject: str) -> str:
        async with Client(f"{harness.base}/mcp", auth=token) as client:
            result = await client.call_tool(
                "gmail_drafts_create",
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
        console_port = pick_free_port()
        settings = console_settings(
            migrated_db_url,
            config_file=_console_config(tmp_path, upstream_url),
            csrf_secret=SecretStr("csrf"),
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
                tools = {t.name for t in await client.list_tools()}
                assert "standin_echo" in tools
                result = await client.call_tool(
                    "standin_echo", {"input": {"text": "hi"}, "rationale": "e2e", "wait_for_approval_ms": 0}
                )
                assert result.structured_content is not None
                assert result.structured_content["status"] == ToolCallStatus.PENDING_APPROVAL
                tool_call_id = result.structured_content["tool_call_id"]

            # The operator approves via the CSRF-gated decision endpoint -> the real upstream runs.
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


def _hidden_input(page: str, name: str) -> str:
    match = re.search(rf'<input[^>]+name="{name}"[^>]+value="([^"]+)"', page)
    if match is None:
        raise AssertionError(f"OAuth consent page has no {name!r} input")
    return match.group(1)


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
            csrf_secret=SecretStr("csrf"),
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
                assert "standin_echo" in {t.name for t in await client.list_tools()}

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
                assert enrollment.status_code == 200, enrollment.text
                assert "Connect an agent" in enrollment.text
                create_action = re.search(r'<form method="post" action="([^"]+/new)">', enrollment.text)
                assert create_action is not None
                approved = await anon.post(
                    f"{base}{create_action.group(1)}",
                    headers={"Origin": base},
                    data={"form_token": _hidden_input(enrollment.text, "form_token"), "agent_name": "OAuth Claude"},
                    follow_redirects=False,
                )
                assert approved.status_code == 200, approved.text
                continue_link = re.search(r'<a class="continue" href="([^"]+)">', approved.text)
                assert continue_link is not None
                upstream_authorize = httpx.URL(html.unescape(continue_link.group(1)))
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
                    assert {"get_tool_call", "list_tool_calls", "standin_echo"} <= {
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
