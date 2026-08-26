"""Operator-approved MCP tool-call API tests."""

from __future__ import annotations

import datetime
import json
import time
from collections.abc import AsyncGenerator, Callable, Generator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, call
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastmcp import FastMCP
from fastmcp.dependencies import Depends
from mcp import types as mcp_types
from pydantic import ValidationError
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocketDisconnect

from haku.console import console_events, operator_auth
from haku.console.agents.authorization import fingerprint_static_token
from haku.console.agents.models import (
    AgentStatus,
    ClientRegistrationKind,
    CredentialBindingStatus,
    CredentialKind,
    EnrollmentPhase,
)
from haku.console.chat_models import RuntimeKind, SessionStatus
from haku.console.conftest import operator_id, write_config
from haku.console.database_migrate import apply_migrations
from haku.console.database_schema import (
    Agent,
    Conversation,
    CredentialBinding,
    McpOperatorOAuthAssociation,
    McpToolCall,
    McpToolCallPrincipal,
    Session,
    StaticCredential,
)
from haku.console.mcp_approval import (
    DegradedReflection,
    McpServerDispatcher,
    PostgresToolCallLedger,
    ToolCallRecord,
    _execution_auth,
    _mcp_result_to_json,
    metadata_for_operator,
)
from haku.console.mcp_config import (
    ConsoleConfigFile,
    InProcessBackend,
    InProcessCredentialKind,
    InProcessServerRegistration,
    McpServerEntry,
    NoCredential,
    OperatorConnectionCredential,
    RemoteServerOAuthAuth,
    validate_in_process_server_bindings,
)
from haku.console.mcp_execution import McpExecutionContext, OperatorMcpExecutionCaller, require_mcp_execution_context
from haku.console.mcp_operator_oauth import PostgresMcpOperatorOAuthStore
from haku.console.mcp_reflection_cache import ReflectedCatalog
from haku.console.node_daemon_models import NodeDaemonExecutionStatus
from haku.console.oauth_token_state import new_oauth_token_state
from haku.console.operator_identity import OperatorStatus
from haku.console.tool_call_actor import AgentActor, OperatorActor, ToolCallActor
from haku.console.tool_call_service import ToolCallApplicationService, backend_auth_for_operator
from haku.console.tool_calls import (
    AgentToolCallCaller,
    OperatorToolCallCaller,
    SubmitToolCallRequest,
    ToolCallPayloadField,
    ToolCallStatus,
)
from haku.console.tools.gmail import build_mcp as build_gmail_mcp
from util.net import pick_free_port
from util.testing.asgi import serve_app_sync

_EXECUTION_CONTEXT_DEPENDENCY = Depends(require_mcp_execution_context)


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
    async def caller_id(execution: McpExecutionContext = _EXECUTION_CONTEXT_DEPENDENCY) -> str:
        caller = execution.caller
        return str(caller.operator_id) if isinstance(caller, OperatorMcpExecutionCaller) else str(caller.agent_id)

    return server


@asynccontextmanager
async def _serve_remote_oauth(
    *,
    preregistered_client_id: str | None = None,
    expected_client_secret: str | None = None,
    bearers: list[str | None] | None = None,
) -> AsyncGenerator[str]:
    """A fake OAuth server. With `preregistered_client_id` set, the metadata omits
    `registration_endpoint` and no `/auth/register` route is mounted at all — mirroring
    Authentik (fronted by the Kubernetes MCP server), which has no DCR endpoint — so the test
    fails loudly if the client under test attempts dynamic registration anyway.
    """
    port = pick_free_port()
    base_url = f"http://127.0.0.1:{port}"
    expected_client_id = preregistered_client_id or "dynamic-client"

    async def mcp(request: Request) -> JSONResponse:
        # This endpoint only ever issues the 401 challenge that starts the DCR dance -- it is not a
        # real MCP server. Recording here is still the wire: it is what the console dialled and the
        # credential it presented, which is the whole claim under test.
        if bearers is not None:
            header = request.headers.get("authorization")
            bearers.append(header.removeprefix("Bearer ") if header else None)
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
        if preregistered_client_id is None:
            metadata["registration_endpoint"] = f"{base_url}/auth/register"
        return JSONResponse(metadata)

    async def register(request: Request) -> JSONResponse:
        body = await request.json()
        assert body["client_name"] == "Haku Console"
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
            if expected_client_secret is not None:
                assert form["client_secret"] == expected_client_secret
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
        # POST too: the DCR challenge is a GET probe, but an executed tool call is a POST, and the
        # bearer it carries is what these tests assert on.
        Route("/mcp", mcp, methods=["GET", "POST"]),
        Route("/.well-known/oauth-protected-resource/mcp", protected_resource),
        Route("/.well-known/oauth-protected-resource", protected_resource),
        Route("/.well-known/oauth-authorization-server/auth", oauth_metadata),
        Route("/auth/token", token, methods=["POST"]),
    ]
    if preregistered_client_id is None:
        routes.append(Route("/auth/register", register, methods=["POST"]))
    app = Starlette(routes=routes)
    with serve_app_sync(app, port=port):
        yield base_url


@pytest.fixture
async def remote_oauth_url(upstream_bearers: list[str | None]) -> AsyncGenerator[str]:
    async with _serve_remote_oauth(bearers=upstream_bearers) as url:
        yield url


@pytest.fixture
async def preregistered_remote_oauth_url() -> AsyncGenerator[str]:
    async with _serve_remote_oauth(preregistered_client_id="preregistered-client") as url:
        yield url


@pytest.fixture
async def preregistered_confidential_remote_oauth_url() -> AsyncGenerator[str]:
    async with _serve_remote_oauth(
        preregistered_client_id="github-client-id", expected_client_secret="github-client-secret"
    ) as url:
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
        "access_profile_id": "no_auto_approval",
    }
]


@pytest.fixture(autouse=True)
async def _static_agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_AGENT_TOKEN_ENV, _AGENT_TOKEN)
    monkeypatch.setenv(_AGENT_OPERATOR_ENV, "op-haku")


class _BearerRecorder:
    """ASGI wrapper recording the bearer each tool call actually arrives with.

    Deliberately at the transport, not at an executor seam: this asserts the credential reached the
    wire, so it still catches a console that resolves the right token and then sends another — and
    it proves one Operator's token never rides another Operator's call. Catalog reconciliation also
    initializes this transport and lists tools, so only JSON-RPC ``tools/call`` requests belong in
    the execution-auth assertion.
    """

    def __init__(self, app: ASGIApp, bearers: list[str | None]) -> None:
        self._app = app
        self._bearers = bearers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            request = Request(scope, receive)
            body = await request.body()
            message = json.loads(body) if body else None
            if isinstance(message, dict) and message.get("method") == "tools/call":
                raw = dict(scope["headers"]).get(b"authorization")
                self._bearers.append(raw.decode().removeprefix("Bearer ") if raw else None)

            delivered = False

            async def replay_receive() -> Message:
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            await self._app(scope, replay_receive, send)
            return
        await self._app(scope, receive, send)


@contextmanager
def _serve_recording_mcp(server: FastMCP, bearers: list[str | None]) -> Generator[str]:
    """`serve_fastmcp`, plus a record of the credentials that reached it."""
    mcp_app = server.http_app(path="/")
    app = Starlette(routes=[Mount("/mcp", app=_BearerRecorder(mcp_app, bearers))], lifespan=mcp_app.lifespan)
    with serve_app_sync(app) as base:
        yield f"{base}/mcp"


@pytest.fixture
def upstream_bearers() -> list[str | None]:
    return []


@pytest.fixture
def mcp_server_url(monkeypatch: pytest.MonkeyPatch, upstream_bearers: list[str | None]) -> Generator[str]:
    monkeypatch.setenv("HAKU_CONSOLE_MCP_CREDENTIAL_HAKU_CONSOLE_GROCY_SF_TOKEN", "test-token")
    with _serve_recording_mcp(_build_test_mcp_server(), upstream_bearers) as url:
        yield url


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
        "mcp": {"servers": servers},
        "static_agents": _STATIC_AGENTS,
        "auto_approval_policies": [{"id": "no_auto_approval", "type": "never"}],
        "access_profiles": [{"id": "no_auto_approval", "auto_approval_policy": "no_auto_approval"}],
        "default_access_profile_id": "no_auto_approval",
    }


def _remote_server(server_id: str, url: str, auth: dict[str, Any]) -> dict[str, Any]:
    return {"id": server_id, "backend": {"kind": "remote_mcp", "url": url, "auth": auth}}


def _dynamic_remote_oauth() -> dict[str, Any]:
    return {"kind": "remote_server_oauth", "client_registration": {"kind": "dynamic", "client_name": "Haku Console"}}


def _in_process_server(server_id: str, credential: dict[str, Any]) -> dict[str, Any]:
    return {"id": server_id, "backend": {"kind": "in_process", "credential": credential}}


def _build_gmail_shaped_mcp() -> FastMCP:
    """A real MCP server standing in for the `gmail` upstream.

    These tests are about the auto-approval policy and the approve-then-execute path, not about
    Gmail: the policy keys on `gmail/<tool>`, so the server id and tool names must match, but what
    sits behind them is a separate service. A real in-process MCP server is the honest double —
    it exercises tool dispatch, schema validation, and result marshalling, where an executor stub
    replaced all three.
    """
    server = FastMCP("gmail-stand-in")

    @server.tool()
    async def labels_list() -> str:
        """List the user's labels."""
        return "labels_list:ok"

    @server.tool()
    async def drafts_create(to: list[str], subject: str, body: str) -> str:
        """Create a draft message."""
        return f"drafts_create:{subject}"

    return server


def _operator_connection_server(mcp: FastMCP) -> InProcessServerRegistration:
    return InProcessServerRegistration(
        builder=lambda _context: mcp, credential_kind=InProcessCredentialKind.OPERATOR_CONNECTION
    )


def _config_file(tmp_path: Path, mcp_server_url: str) -> Path:
    servers = [
        _remote_server(
            "grocy-sf", mcp_server_url, {"kind": "static_bearer", "bearer_token_secret": "haku-console-grocy-sf-token"}
        ),
        _remote_server("smoke", mcp_server_url, {"kind": "none"}),
    ]
    return write_config(tmp_path / "haku_console.yaml", _config(servers))


@pytest.fixture
def console_config(tmp_path: Path, mcp_server_url: str) -> Path:
    """The standard two-server console config (`grocy-sf` static-bearer + `smoke`) most tests use."""
    return _config_file(tmp_path, mcp_server_url)


@pytest.fixture
def operator_client(make_operator_client: Callable[..., Any], console_config: Path) -> Generator[TestClient]:
    """An operator-session client against the standard `console_config` — the setup the majority of
    operator-facing tests need. Tests with a bespoke config call `make_operator_client`
    (or `make_client`) directly instead."""
    with make_operator_client(config_file=console_config) as client:
        yield client


@pytest.fixture
def operator_oauth_config_file(tmp_path: Path, remote_oauth_url: str) -> Path:
    servers = [_remote_server("grocy-sf", f"{remote_oauth_url}/mcp", _dynamic_remote_oauth())]
    return write_config(tmp_path / "haku_console_operator_oauth.yaml", _config(servers))


@pytest.fixture
def gmail_config_file(tmp_path: Path) -> Path:
    config = _config([_in_process_server("gmail", {"kind": "operator_connection", "connection": "google_mail"})])
    config["static_agents"] = [{**_STATIC_AGENTS[0], "access_profile_id": "haku"}]
    config["auto_approval_policies"] = [
        {"id": "manual_review", "type": "never"},
        {"id": "gmail_reads", "type": "exact_tools", "tools": {"gmail": ["labels_list"]}},
        {"id": "managed_gmail_labels", "type": "gmail_label_namespace", "server": "gmail", "label_prefix": "haku/"},
        {"id": "haku_v1", "type": "any_of", "policies": ["gmail_reads", "managed_gmail_labels"]},
    ]
    config["access_profiles"] = [
        {"id": "manual-review", "auto_approval_policy": "manual_review"},
        {"id": "haku", "auto_approval_policy": "haku_v1"},
    ]
    config["default_access_profile_id"] = "manual-review"
    config["operator_connection_providers"] = {
        "google_mail": {
            "kind": "google",
            "client_id_env_var": "GOOGLE_MAIL_CLIENT_ID",
            "client_secret_env_var": "GOOGLE_MAIL_CLIENT_SECRET",
        }
    }
    config["operator_connections"] = {
        "google_mail": {
            "display_name": "Google Mail",
            "provider": "google_mail",
            "scopes": ["https://www.googleapis.com/auth/gmail.modify"],
        }
    }
    return write_config(tmp_path / "haku_console_gmail.yaml", config)


@pytest.fixture
def preregistered_operator_oauth_config_file(tmp_path: Path, preregistered_remote_oauth_url: str) -> Path:
    servers = [
        _remote_server(
            "grocy-sf",
            f"{preregistered_remote_oauth_url}/mcp",
            {
                "kind": "remote_server_oauth",
                "client_registration": {"kind": "preregistered", "client_id": "preregistered-client"},
            },
        )
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
    return client.portal.call(resolve)


async def test_session_agent_tool_call_retains_exact_session_attribution(
    *, make_client, console_config: Path, migrated_sessions: async_sessionmaker[AsyncSession]
) -> None:
    with make_client(config_file=console_config) as client:
        static_actor = _static_agent_actor(client, "tool-token")
        assert static_actor.access_profile_id is not None
        conversation_id, session_id = uuid4(), uuid4()
        now = datetime.datetime.now(datetime.UTC)
        async with migrated_sessions.begin() as db:
            db.add(
                Conversation(
                    conversation_id=conversation_id,
                    operator_id=static_actor.operator_id,
                    agent_id=static_actor.agent_id,
                    access_profile_id=static_actor.access_profile_id,
                    runtime_kind=RuntimeKind.CLAUDE_CODE,
                    created_at=now,
                )
            )
            db.add(
                Session(
                    session_id=session_id,
                    operator_id=static_actor.operator_id,
                    conversation_id=conversation_id,
                    agent_binding_id=static_actor.binding_id,
                    status=SessionStatus.READY,
                    bridge_token_fingerprint=session_id.bytes,
                    bridge_connected_at=now,
                    lease_expires_at=now + datetime.timedelta(minutes=1),
                    lease_holder="test-replica",
                    created_at=now,
                    updated_at=now,
                )
            )

        session_actor = AgentActor(
            agent_id=static_actor.agent_id,
            operator_id=static_actor.operator_id,
            binding_id=static_actor.binding_id,
            access_profile_id=static_actor.access_profile_id,
            session_id=session_id,
        )
        submitted = _submit_request(
            client,
            SubmitToolCallRequest(server_id="smoke", tool_name="echo", arguments={"text": "session"}, wait_for_ms=0),
            actor=session_actor,
        )
        call_id = cast(str, submitted["tool_call_id"])

    ledger = PostgresToolCallLedger(migrated_sessions)
    async with migrated_sessions() as db:
        principal = await db.get(McpToolCallPrincipal, call_id)
        assert principal is not None
        assert principal.binding_id == static_actor.binding_id
        assert principal.session_id == session_id
    operator_actor = OperatorActor(operator_id=static_actor.operator_id)
    record = await ledger.get(call_id, actor=operator_actor)
    assert record.caller == AgentToolCallCaller(
        agent_id=static_actor.agent_id, display_name="Haku", session_id=session_id
    )
    await ledger.mark_running(call_id, actor=operator_actor)
    execution = await ledger.authorize_execution(call_id, actor=session_actor)
    assert execution.caller == AgentActor(
        agent_id=static_actor.agent_id,
        operator_id=static_actor.operator_id,
        binding_id=static_actor.binding_id,
        access_profile_id=static_actor.access_profile_id,
        session_id=session_id,
    )


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


@pytest.fixture
def gmail_client() -> Mock:
    return Mock()


@pytest.fixture
def routing_upstream(mcp_server_url: str, upstream_bearers: list[str | None]) -> tuple[str, list[str | None]]:
    return mcp_server_url, upstream_bearers


@pytest.mark.parametrize(
    ("method", "path", "json"),
    [
        ("POST", "/api/mcp/operator-auth/grocy-sf/connect", None),
        ("DELETE", "/api/mcp/operator-auth/grocy-sf", None),
        ("POST", "/api/operator-connections/google_mail/connect", None),
        ("DELETE", "/api/operator-connections/google_mail", None),
        ("POST", "/api/tool-calls/not-a-call/decision", {"decision": "approve"}),
    ],
)
def test_operator_mutations_reject_untrusted_origin(
    operator_client: TestClient, method: str, path: str, json: dict[str, str] | None
) -> None:
    response = operator_client.request(method, path, headers={"Origin": "https://haku-ui.test"}, json=json)

    assert response.status_code == 403
    assert response.json()["detail"] == "operator mutations require the console's exact Origin"


def test_operator_oauth_association_emits_console_events(
    make_operator_client, operator_oauth_config_file: Path
) -> None:
    with (
        make_operator_client(config_file=operator_oauth_config_file) as client,
        client.websocket_connect("/api/events/ws", headers={"Origin": "https://haku.test"}) as events,
    ):
        assert events.receive_json() == {"event_type": "hello"}
        started = client.post("/api/mcp/operator-auth/grocy-sf/connect")
        state = parse_qs(urlparse(started.json()["authorization_url"]).query)["state"][0]
        callback = client.get(
            "/api/mcp/operator-auth/callback", params={"state": state, "code": "operator-code"}, follow_redirects=False
        )
        association_event = events.receive_json()
        disconnected = client.delete("/api/mcp/operator-auth/grocy-sf")
        disassociation_event = events.receive_json()

    assert callback.status_code == 303, callback.text
    assert callback.headers["location"].startswith("/_console/settings?oauth_result=")
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


def test_operator_oauth_preregistered_client_skips_dynamic_registration(
    make_operator_client, preregistered_operator_oauth_config_file: Path
) -> None:
    """Mirrors kubectl-passthrough-mcp: fronted by Authentik, which has no DCR endpoint —
    dynamic registration would 401, so a pre-registered client must skip it entirely.
    """
    with make_operator_client(config_file=preregistered_operator_oauth_config_file) as client:
        started = client.post("/api/mcp/operator-auth/grocy-sf/connect")
        assert started.status_code == 200, started.text
        auth_query = parse_qs(urlparse(started.json()["authorization_url"]).query)
        assert auth_query["client_id"] == ["preregistered-client"]

        callback = client.get(
            "/api/mcp/operator-auth/callback",
            params={"state": auth_query["state"][0], "code": "operator-code"},
            follow_redirects=False,
        )

    assert callback.status_code == 303, callback.text


def test_operator_oauth_preregistered_confidential_client_reads_deploy_secret(
    make_operator_client,
    tmp_path: Path,
    preregistered_confidential_remote_oauth_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_MCP_CLIENT_ID", "github-client-id")
    monkeypatch.setenv("GITHUB_MCP_CLIENT_SECRET", "github-client-secret")
    config_file = write_config(
        tmp_path / "haku_console_github_mcp.yaml",
        _config(
            [
                _remote_server(
                    "github",
                    f"{preregistered_confidential_remote_oauth_url}/mcp",
                    {
                        "kind": "remote_server_oauth",
                        "client_registration": {
                            "kind": "preregistered",
                            "client_id_env_var": "GITHUB_MCP_CLIENT_ID",
                            "client_secret_env_var": "GITHUB_MCP_CLIENT_SECRET",
                            "token_endpoint_auth_method": "client_secret_post",
                        },
                    },
                )
            ]
        ),
    )

    with make_operator_client(config_file=config_file) as client:
        started = client.post("/api/mcp/operator-auth/github/connect")
        assert started.status_code == 200, started.text
        auth_query = parse_qs(urlparse(started.json()["authorization_url"]).query)
        assert auth_query["client_id"] == ["github-client-id"]
        callback = client.get(
            "/api/mcp/operator-auth/callback",
            params={"state": auth_query["state"][0], "code": "operator-code"},
            follow_redirects=False,
        )

    assert callback.status_code == 303, callback.text


async def test_operator_oauth_callback_is_bound_to_flow_operator(
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
        started = operator_a.post("/api/mcp/operator-auth/grocy-sf/connect")
        state = parse_qs(urlparse(started.json()["authorization_url"]).query)["state"][0]

        wrong_operator = operator_b.get(
            "/api/mcp/operator-auth/callback", params={"state": state, "code": "operator-code"}, follow_redirects=False
        )
        wrong_result_id = parse_qs(urlparse(wrong_operator.headers["location"]).query)["oauth_result"][0]
        wrong_result = operator_b.post(f"/api/oauth-results/{wrong_result_id}")
        completed = operator_a.get(
            "/api/mcp/operator-auth/callback", params={"state": state, "code": "operator-code"}, follow_redirects=False
        )

    assert wrong_operator.status_code == 303
    assert wrong_result.json() == {
        "status": "error",
        "title": "Couldn't connect the MCP account",
        "message": "OAuth flow belongs to a different operator",
    }
    # A mismatched session does not consume the flow: its owner can still complete it.
    assert completed.status_code == 303, completed.text


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


async def test_haku_gmail_labels_list_auto_approves_executes_and_records_policy(
    make_client, make_operator_client, gmail_config_file: Path, gmail_client: Mock
) -> None:
    with (
        make_client(
            config_file=gmail_config_file,
            gmail_client=gmail_client,
            in_process_servers={"gmail": _operator_connection_server(_build_gmail_shaped_mcp())},
        ) as client,
        make_operator_client(config_file=gmail_config_file, operator_external_user_key="op-haku") as operator,
    ):
        client.app.state.provider_connection_store.access_token_for = AsyncMock(return_value="operator-token")
        record = _submit_request(
            client,
            SubmitToolCallRequest(server_id="gmail", tool_name="labels_list", arguments={}, wait_for_ms=0),
            actor=_static_agent_actor(client, "tool-token"),
        )
        pending = operator.get("/api/approvals/pending").json()

    assert record["status"] == "ok"
    assert record["approval_policy_id"] == "agent_policy_v1"
    assert record["auto_approval_evaluation"] == (
        "approved: Agent policy 'haku_v1' matched haku_v1 -> gmail_reads: exact tool gmail/labels_list is listed"
    )
    assert record["approved_at"] is not None
    assert record["result"]["content"][0]["text"] == "labels_list:ok"
    assert pending["approvals"] == []


async def test_operator_gmail_labels_list_stays_pending(
    make_operator_client, gmail_config_file: Path, gmail_client: Mock
) -> None:
    with make_operator_client(
        config_file=gmail_config_file,
        gmail_client=gmail_client,
        # Matching a configured agent id must not turn an operator into an auto-approved agent.
        operator_username="haku",
    ) as client:
        record = _submit_request(
            client, SubmitToolCallRequest(server_id="gmail", tool_name="labels_list", arguments={}, wait_for_ms=0)
        )

    assert record["status"] == "pending_approval"
    assert record["approval_policy_id"] is None
    assert record["auto_approval_evaluation"] is None


async def test_list_tool_calls_filters_by_auto_approved(
    make_client, make_operator_client, gmail_config_file: Path, gmail_client: Mock
) -> None:
    with (
        make_client(
            config_file=gmail_config_file,
            gmail_client=gmail_client,
            in_process_servers={"gmail": _operator_connection_server(_build_gmail_shaped_mcp())},
        ) as client,
        make_operator_client(config_file=gmail_config_file, operator_external_user_key="op-haku") as operator,
    ):
        client.app.state.provider_connection_store.access_token_for = AsyncMock(return_value="operator-token")
        agent = _static_agent_actor(client, "tool-token")
        auto = _submit_request(
            client,
            SubmitToolCallRequest(server_id="gmail", tool_name="labels_list", arguments={}, wait_for_ms=0),
            actor=agent,
        )
        manual = _submit_request(
            client,
            SubmitToolCallRequest(
                server_id="gmail",
                tool_name="drafts_create",
                arguments={"to": ["a@b.test"], "subject": "s", "body": "b"},
                wait_for_ms=0,
            ),
            actor=agent,
        )

        hidden = operator.get("/api/tool-calls", params={"auto_approved": "false"}).json()["tool_calls"]
        shown_only = operator.get("/api/tool-calls", params={"auto_approved": "true"}).json()["tool_calls"]
        unfiltered = operator.get("/api/tool-calls").json()["tool_calls"]

    assert [c["tool_call_id"] for c in hidden] == [manual["tool_call_id"]]
    assert [c["tool_call_id"] for c in shown_only] == [auto["tool_call_id"]]
    assert {c["tool_call_id"] for c in unfiltered} == {manual["tool_call_id"], auto["tool_call_id"]}


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
    make_client: Callable[..., Any], make_operator_client: Callable[..., Any], console_config: Path
) -> None:
    with (
        make_client(config_file=console_config) as client,
        make_operator_client(config_file=console_config, operator_external_user_key="op-haku") as operator,
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
    assert withdrawn["denial_reason"] is None
    # Out of the queue, still in the ledger: withdrawal is an audit fact, not a delete.
    assert [(c["tool_call_id"], c["status"]) for c in history] == [(pending["tool_call_id"], "withdrawn")]
    # Deciding a call the agent already retracted is a conflict, not a silent re-approval.
    assert decision.status_code == 409
    assert "not pending approval" in decision.json()["detail"]


async def test_websocket_receives_agent_withdrawal_invalidation(
    make_client: Callable[..., Any], make_operator_client: Callable[..., Any], console_config: Path
) -> None:
    with (
        make_client(config_file=console_config) as client,
        make_operator_client(config_file=console_config, operator_external_user_key="op-haku") as operator,
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


async def test_haku_gmail_nonmatching_policy_evaluation_is_recorded(
    make_client, make_operator_client, gmail_config_file: Path, gmail_client: Mock
) -> None:
    with (
        make_client(
            config_file=gmail_config_file,
            gmail_client=gmail_client,
            in_process_servers={"gmail": _operator_connection_server(build_gmail_mcp(gmail_client))},
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
    assert record["auto_approval_evaluation"] == (
        "manual: Agent policy 'haku_v1' did not auto-approve gmail/threads_modify_labels "
        "(managed_gmail_labels: at least one label name is outside 'haku/')"
    )
    assert pending[0]["auto_approval_evaluation"] == record["auto_approval_evaluation"]


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


async def test_configured_credential_approval_passes_canonical_operator_id(
    *,
    make_operator_client,
    console_config: Path,
    migrated_db_url: str,
    migrated_sessions,
    upstream_bearers: list[str | None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_operator_ids = _record_execution_operator_ids(monkeypatch)
    with make_operator_client(
        config_file=console_config, operator_external_user_key="configured-credential-sub"
    ) as client:
        submitted = _submit(client)
        approved = client.post(f"/api/tool-calls/{submitted['tool_call_id']}/decision", json={"decision": "approve"})
        # Drain before the client (and its lifespan aclose) tears down, so execution runs to completion.
        _drain_executions(client)

    assert approved.status_code == 200, approved.text
    assert approved.json()["tool_call"]["status"] == "running"
    assert execution_operator_ids == [await operator_id(migrated_sessions, "configured-credential-sub")]
    assert upstream_bearers == ["test-token"]


async def test_operator_oauth_association_drives_approved_tool_execution(
    *,
    make_operator_client,
    operator_oauth_config_file: Path,
    upstream_bearers: list[str | None],
    migrated_db_url: str,
    migrated_sessions,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_operator_ids = _record_execution_operator_ids(monkeypatch)
    with make_operator_client(
        config_file=operator_oauth_config_file, operator_external_user_key="operator-oauth-sub"
    ) as client:
        started = client.post("/api/mcp/operator-auth/grocy-sf/connect")
        assert started.status_code == 200, started.text
        authorization_url = started.json()["authorization_url"]
        parsed_authorization = urlparse(authorization_url)
        auth_query = parse_qs(parsed_authorization.query)
        assert parsed_authorization.path == "/auth/authorize"
        assert auth_query["client_id"] == ["dynamic-client"]
        assert auth_query["redirect_uri"] == ["https://haku.test/api/mcp/operator-auth/callback"]
        assert auth_query["code_challenge_method"] == ["S256"]

        callback = client.get(
            "/api/mcp/operator-auth/callback",
            params={"state": auth_query["state"][0], "code": "operator-code"},
            follow_redirects=False,
        )
        assert callback.status_code == 303, callback.text

        reconnect = client.post("/api/mcp/operator-auth/grocy-sf/connect")
        removed_start = client.post("/api/mcp/operator-auth/grocy-sf/start")
        assert reconnect.status_code == 409
        assert reconnect.json()["detail"] == "MCP server grocy-sf is already connected; disconnect it first"
        assert removed_start.status_code == 404

        submitted = _submit(client)
        approved = client.post(f"/api/tool-calls/{submitted['tool_call_id']}/decision", json={"decision": "approve"})
        _drain_executions(client)

    assert approved.status_code == 200, approved.text
    assert approved.json()["tool_call"]["status"] == "running"
    assert execution_operator_ids == [await operator_id(migrated_sessions, "operator-oauth-sub")]
    # The upstream saw the unauthenticated probe that starts the DCR challenge, then both the
    # connection-change catalog refresh and approved execution carrying this Operator's token.
    assert upstream_bearers == [None, "operator-access-token", "operator-access-token"]


async def test_operator_oauth_approval_requires_existing_association(
    make_operator_client, operator_oauth_config_file: Path, upstream_bearers: list[str | None]
) -> None:
    with make_operator_client(config_file=operator_oauth_config_file) as client:
        submitted = _submit(client)
        resp = client.post(f"/api/tool-calls/{submitted['tool_call_id']}/decision", json={"decision": "approve"})
        fetched = client.get(f"/api/tool-calls/{submitted['tool_call_id']}").json()

    assert resp.status_code == 409
    assert "Connect your grocy-sf MCP account" in resp.json()["detail"]
    assert fetched["status"] == "pending_approval"
    assert upstream_bearers == []


async def _seed_association(
    sessions: async_sessionmaker[AsyncSession], *, operator_external_user_key: str, access_token: str
) -> None:
    """Insert a connected operator_oauth association for grocy-sf (bypassing the DCR/PKCE flow)."""
    now = datetime.datetime.now(datetime.UTC)
    resolved_operator_id = await operator_id(sessions, operator_external_user_key)
    async with sessions.begin() as session:
        session.add(
            McpOperatorOAuthAssociation(
                server_id="grocy-sf",
                operator_id=resolved_operator_id,
                created_at=now,
                client_id="test-client",
                token_endpoint="http://unused.test/token",
                token_state=new_oauth_token_state(
                    operator_id=resolved_operator_id,
                    access_token=access_token,
                    refresh_token=None,
                    token_type="Bearer",
                    scope=None,
                    expires_at=now + datetime.timedelta(hours=1),
                    now=now,
                ),
            )
        )


async def test_routing_executes_each_agent_as_its_own_operator(
    *,
    make_client,
    tmp_path: Path,
    migrated_db_url: str,
    migrated_sessions: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    routing_upstream: tuple[str, list[str | None]],
) -> None:
    """Two static agents bound to two operators: each agent's auto-approved operator_oauth call
    executes with *its* operator's token, with no crosstalk."""
    # `haku` (bearer tool-token → op-haku) comes from the autouse env; add a second agent `ops-bot`.
    monkeypatch.setenv("HAKU_CONSOLE_TEST_AGENT2_TOKEN", "ops-token")
    monkeypatch.setenv("HAKU_CONSOLE_TEST_AGENT2_OPERATOR", "op-ops")
    mcp_server_url, upstream_bearers = routing_upstream
    await _seed_association(migrated_sessions, operator_external_user_key="op-haku", access_token="grocy-token-haku")
    await _seed_association(migrated_sessions, operator_external_user_key="op-ops", access_token="grocy-token-ops")

    config = _config([_remote_server("grocy-sf", mcp_server_url, _dynamic_remote_oauth())])
    config["auto_approval_policies"] = [
        {"id": "manual_review", "type": "never"},
        {"id": "grocy_reads", "type": "exact_tools", "tools": {"grocy-sf": ["products_list"]}},
    ]
    config["static_agents"] = [
        {**_STATIC_AGENTS[0], "access_profile_id": "grocy-reader"},
        {
            "agent_id": "30000000-0000-4000-8000-000000000002",
            "display_name": "Ops Bot",
            "token_env_var": "HAKU_CONSOLE_TEST_AGENT2_TOKEN",
            "operator_subject_env": "HAKU_CONSOLE_TEST_AGENT2_OPERATOR",
            "access_profile_id": "grocy-reader",
        },
    ]
    config["access_profiles"] = [
        {"id": "manual-review", "auto_approval_policy": "manual_review"},
        {"id": "grocy-reader", "auto_approval_policy": "grocy_reads"},
    ]
    config["default_access_profile_id"] = "manual-review"
    with make_client(config_file=write_config(tmp_path / "routing.yaml", config)) as client:
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

            async def list_calls(actor: ToolCallActor = actor) -> list[ToolCallRecord]:
                return cast(list[ToolCallRecord], await client.app.state.tool_call_service.list_tool_calls(actor=actor))

            assert client.portal is not None
            listed = client.portal.call(list_calls)
            assert [call.tool_call_id for call in listed] == [expected_call_id]
            assert client.get("/api/tool-calls", headers={"Authorization": f"Bearer {bearer}"}).status_code == 401

    # haku's call executed with op-haku's token; ops-bot's with op-ops's — each routed to its operator.
    assert upstream_bearers == ["grocy-token-haku", "grocy-token-ops"]


async def test_two_operator_two_agent_http_authorization_matrix(
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
        [
            _remote_server(
                "grocy-sf",
                mcp_server_url,
                {"kind": "static_bearer", "bearer_token_secret": "haku-console-grocy-sf-token"},
            )
        ]
    )
    config["static_agents"] = [
        {
            "agent_id": f"30000000-0000-4000-8000-{index:012d}",
            "display_name": name.replace("-", " ").title(),
            "token_env_var": token_env,
            "operator_subject_env": operator_env,
            "access_profile_id": "no_auto_approval",
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
            response = operator.post(f"/api/tool-calls/{foreign_call_id}/decision", json={"decision": "approve"})
            assert response.status_code == 404

        approved = operator_a.post(f"/api/tool-calls/{call_ids['haku']}/decision", json={"decision": "approve"})
        denied = operator_b.post(
            f"/api/tool-calls/{call_ids['ops']}/decision", json={"decision": "deny", "reason": "no"}
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["tool_call"]["status"] == "running"
        assert denied.status_code == 200, denied.text
        assert denied.json()["tool_call"]["status"] == "denied"


async def test_approval_denial_is_terminal_and_does_not_execute(operator_client: TestClient) -> None:
    submitted = _submit(operator_client)
    resp = operator_client.post(
        f"/api/tool-calls/{submitted['tool_call_id']}/decision", json={"decision": "deny", "reason": "not today"}
    )
    assert resp.status_code == 200
    tool_call = resp.json()["tool_call"]
    assert tool_call["status"] == "denied"
    assert tool_call["result"] is None
    assert tool_call["denial_reason"] == "not today"


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


async def test_unknown_oauth_server_maps_to_http_not_found(operator_client: TestClient) -> None:
    connected = operator_client.post("/api/mcp/operator-auth/missing/connect")

    assert connected.status_code == 404
    assert connected.json()["detail"] == "unknown MCP server: missing"


async def test_operator_tenants_cannot_read_or_decide_each_others_calls(
    make_operator_client, console_config: Path
) -> None:
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
    console_config: Path,
    migrated_db_url: str,
    migrated_sessions,
    migrated_engine: AsyncEngine,
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
        summaries = await ledger.list_tool_calls(actor=actor, fields=frozenset())
        assert len(summaries) == 2
        assert len(statements) == 1, statements
        summary_sql = statements[0].casefold()
        assert "arguments_json" not in summary_sql
        assert "rationale" not in summary_sql
        assert "result_json" not in summary_sql

        statements.clear()
        resolved = await ledger.get(agent_call_id, actor=actor, fields=frozenset({ToolCallPayloadField.RESULT}))
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
        "node_daemon_execution_status": tuple(status.value for status in NodeDaemonExecutionStatus),
        "tool_call_status": tuple(status.value for status in ToolCallStatus),
    }
    assert baseline_values == current_values


# --- In-process MCP servers (McpServerDispatcher in-process registration) ---
# Unit tests only: no postgres/network fixtures, exercising McpServerDispatcher
# directly (over a fresh `_build_test_mcp_server()` instance, in-memory — no HTTP)
# rather than through the FastAPI app.


def test_server_entry_allows_in_process_backend() -> None:
    McpServerEntry(
        id="google", backend=InProcessBackend(credential=OperatorConnectionCredential(connection="google_workspace"))
    )  # ok: resolved via the in-process registry at runtime, not this model


async def test_config_rejects_unknown_operator_connection() -> None:
    with pytest.raises(ValidationError, match="unknown operator connection 'missing'"):
        ConsoleConfigFile.model_validate(
            {
                **_config([]),
                "mcp": {
                    "servers": [_in_process_server("google", {"kind": "operator_connection", "connection": "missing"})]
                },
            }
        )


async def test_config_allows_distinct_provider_instances_of_one_kind() -> None:
    config = ConsoleConfigFile.model_validate(
        {
            **_config([]),
            "operator_connection_providers": {
                "google_mail": {
                    "kind": "google",
                    "client_id_env_var": "GOOGLE_MAIL_CLIENT_ID",
                    "client_secret_env_var": "GOOGLE_MAIL_CLIENT_SECRET",
                },
                "google_calendar": {
                    "kind": "google",
                    "client_id_env_var": "GOOGLE_CALENDAR_CLIENT_ID",
                    "client_secret_env_var": "GOOGLE_CALENDAR_CLIENT_SECRET",
                },
            },
            "operator_connections": {
                "google_mail": {"display_name": "Google Mail", "provider": "google_mail", "scopes": ["gmail"]},
                "google_calendar": {
                    "display_name": "Google Calendar",
                    "provider": "google_calendar",
                    "scopes": ["calendar"],
                },
            },
        }
    )
    assert list(config.operator_connections) == ["google_mail", "google_calendar"]


async def test_config_rejects_incompatible_registered_credential_kind() -> None:
    config = ConsoleConfigFile.model_validate(
        {
            **_config([]),
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
                    _in_process_server("google", {"kind": "operator_connection", "connection": "google_workspace"})
                ]
            },
        }
    )
    registration = InProcessServerRegistration(
        builder=lambda _context: _build_test_mcp_server(), credential_kind=InProcessCredentialKind.NONE
    )

    with pytest.raises(ValueError, match="requires 'none' credential, got 'operator_connection'"):
        validate_in_process_server_bindings(config, {"google": registration})


async def test_remote_oauth_client_registration_variants_reject_each_others_fields() -> None:
    with pytest.raises(ValidationError, match="client_id"):
        RemoteServerOAuthAuth.model_validate(
            {
                "client_registration": {
                    "kind": "dynamic",
                    "client_name": "Haku Console",
                    "client_id": "not-valid-for-dcr",
                }
            }
        )
    with pytest.raises(ValidationError, match="client_secret_env_var"):
        RemoteServerOAuthAuth.model_validate(
            {
                "client_registration": {
                    "kind": "preregistered",
                    "client_id": "existing-client",
                    "client_secret_env_var": "GITHUB_MCP_CLIENT_SECRET",
                }
            }
        )
    with pytest.raises(ValidationError, match="client_name"):
        RemoteServerOAuthAuth.model_validate(
            {
                "client_registration": {
                    "kind": "preregistered",
                    "client_id": "existing-client",
                    "client_name": "not-valid-for-preregistered",
                }
            }
        )


async def test_executor_dispatches_to_registered_in_process_server() -> None:
    builder = Mock(return_value=_build_test_mcp_server())
    registration = InProcessServerRegistration(
        builder=builder, credential_kind=InProcessCredentialKind.OPERATOR_CONNECTION
    )
    executor = McpServerDispatcher({"google": registration}, catalog_cache_ttl_seconds=0.0)
    server = McpServerEntry(
        id="google", backend=InProcessBackend(credential=OperatorConnectionCredential(connection="google_workspace"))
    )
    context = McpExecutionContext(caller=OperatorMcpExecutionCaller(operator_id=UUID(int=42)), tool_call_id="tc_test")
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
            caller=OperatorMcpExecutionCaller(operator_id=operator_id), tool_call_id="tc_test"
        ),
    )

    assert result["content"][0]["text"] == str(operator_id)


async def test_executor_raises_when_in_process_backend_is_not_registered() -> None:
    executor = McpServerDispatcher({}, catalog_cache_ttl_seconds=0.0)
    server = McpServerEntry(
        id="google", backend=InProcessBackend(credential=OperatorConnectionCredential(connection="google_workspace"))
    )
    with pytest.raises(RuntimeError, match="no in-process registration"):
        await executor.execute(
            server,
            "echo",
            {},
            auth_token=None,
            execution_context=McpExecutionContext(
                caller=OperatorMcpExecutionCaller(operator_id=UUID(int=42)), tool_call_id=None
            ),
        )


async def test_dispatcher_reflects_in_process_server_tools() -> None:
    builder = Mock(return_value=_build_test_mcp_server())
    registration = InProcessServerRegistration(
        builder=builder, credential_kind=InProcessCredentialKind.OPERATOR_CONNECTION
    )
    dispatcher = McpServerDispatcher({"google": registration}, catalog_cache_ttl_seconds=0.0)
    server = McpServerEntry(
        id="google", backend=InProcessBackend(credential=OperatorConnectionCredential(connection="google_workspace"))
    )
    metadata = await dispatcher.metadata(server, auth_token=None)
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


async def test_operator_connection_reflection_checks_presence_without_resolving_token() -> None:
    provider_store = AsyncMock()
    provider_store.is_provisioned.return_value = True
    provider_store.is_connected.return_value = True
    builder = Mock(return_value=_build_test_mcp_server())
    dispatcher = McpServerDispatcher(
        {
            "google": InProcessServerRegistration(
                builder=builder, credential_kind=InProcessCredentialKind.OPERATOR_CONNECTION
            )
        },
        catalog_cache_ttl_seconds=0.0,
    )
    operator = UUID(int=42)
    server = McpServerEntry(
        id="google", backend=InProcessBackend(credential=OperatorConnectionCredential(connection="google_workspace"))
    )

    metadata = await metadata_for_operator(
        operator_id=operator, server=server, dispatcher=dispatcher, oauth_store=Mock(), provider_store=provider_store
    )

    assert isinstance(metadata, ReflectedCatalog)
    builder.assert_called_once_with(None)
    provider_store.is_provisioned.assert_awaited_once_with(connection="google_workspace")
    provider_store.is_connected.assert_awaited_once_with(connection="google_workspace", operator_id=operator)
    provider_store.access_token_for.assert_not_called()


async def test_dispatcher_reuses_a_reflected_catalog_within_the_ttl() -> None:
    builder = Mock(return_value=_build_test_mcp_server())
    registration = InProcessServerRegistration(
        builder=builder, credential_kind=InProcessCredentialKind.OPERATOR_CONNECTION
    )
    dispatcher = McpServerDispatcher({"google": registration}, catalog_cache_ttl_seconds=3600.0)
    server = McpServerEntry(
        id="google", backend=InProcessBackend(credential=OperatorConnectionCredential(connection="google_workspace"))
    )

    first = await dispatcher.metadata(server, auth_token=None)
    second = await dispatcher.metadata(server, auth_token=None)

    assert isinstance(first, ReflectedCatalog)
    assert isinstance(second, ReflectedCatalog)
    assert {tool.name for tool in second.tools} == {tool.name for tool in first.tools}
    builder.assert_called_once_with(None)


async def test_dispatcher_does_not_cache_a_degraded_reflection() -> None:
    """A server that failed must be retried on the next listing, not held degraded for the TTL."""
    dispatcher = McpServerDispatcher({}, catalog_cache_ttl_seconds=3600.0)
    server = McpServerEntry(
        id="google", backend=InProcessBackend(credential=OperatorConnectionCredential(connection="google_workspace"))
    )

    assert isinstance(await dispatcher.metadata(server, auth_token=None), DegradedReflection)

    registered = McpServerDispatcher(
        {
            "google": InProcessServerRegistration(
                builder=Mock(return_value=_build_test_mcp_server()),
                credential_kind=InProcessCredentialKind.OPERATOR_CONNECTION,
            )
        },
        catalog_cache_ttl_seconds=3600.0,
    )
    assert isinstance(await registered.metadata(server, auth_token=None), ReflectedCatalog)


async def test_dispatcher_does_not_serve_one_credentials_catalog_to_another() -> None:
    builder = Mock(return_value=_build_test_mcp_server())
    dispatcher = McpServerDispatcher(
        {"google": InProcessServerRegistration(builder=builder, credential_kind=InProcessCredentialKind.NONE)},
        catalog_cache_ttl_seconds=3600.0,
    )
    server = McpServerEntry(id="google", backend=InProcessBackend(credential=NoCredential()))

    await dispatcher.metadata(server, auth_token="operator-a-token")
    await dispatcher.metadata(server, auth_token="operator-b-token")

    assert builder.call_args_list == [call("operator-a-token"), call("operator-b-token")]


async def test_dispatcher_degrades_when_in_process_backend_is_not_registered() -> None:
    dispatcher = McpServerDispatcher({}, catalog_cache_ttl_seconds=0.0)
    server = McpServerEntry(
        id="google", backend=InProcessBackend(credential=OperatorConnectionCredential(connection="google_workspace"))
    )
    metadata = await dispatcher.metadata(server, auth_token=None)
    assert isinstance(metadata, DegradedReflection)


if __name__ == "__main__":
    pytest_bazel.main()
