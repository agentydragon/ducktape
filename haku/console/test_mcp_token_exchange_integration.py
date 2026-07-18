"""Production-shaped Haku console → authenticated MCP → protected-backend token chain."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
import jwt
import pytest
import pytest_bazel
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from fastmcp import Client
from mcp import types as mcp_types
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from grocy_mcp.mcp_types import ServerSettings
from grocy_mcp.server import build_server
from haku.console.agents.authorization import fingerprint_static_token
from haku.console.app import create_app
from haku.console.config import OperatorOidcConfig
from haku.console.conftest import console_settings, write_config
from haku.console.mcp_config import McpServerEntry, RemoteServerOAuthAuth
from haku.console.tool_call_actor import AgentActor
from haku.console.tool_call_service import ToolCallApplicationService
from haku.console.tool_calls import SubmitToolCallRequest
from mcp_infra.authentik_auth.config import AuthentikAuthConfig
from mcp_infra.persistence import PostgresPersistence
from util.net import pick_free_port
from util.testing.asgi import serve_app
from util.testing.mock_oidc import build_mock_oidc_app, generate_rsa_keypair

_OPERATOR_SUBJECT = "authentik-user-42"
_OPERATOR_USERNAME = "agentydragon"
_AGENT_TOKEN = "haku-static-token"
_AGENT_TOKEN_ENV = "HAKU_CONSOLE_TOKEN_EXCHANGE_TEST_AGENT_TOKEN"
_AGENT_OPERATOR_ENV = "HAKU_CONSOLE_TOKEN_EXCHANGE_TEST_OPERATOR_SUBJECT"
_PROXY_CLIENT_ID = "grocy-proxy-client"


@dataclass(frozen=True)
class _ProtectedBackendCall:
    operator_subject: str
    path: str


@dataclass(frozen=True)
class _TokenChainResult:
    tool_call: dict[str, Any]
    stored_reference: str
    exchanged_assertions: list[str]
    backend_calls: list[_ProtectedBackendCall]
    direct_result_is_error: bool | None
    direct_result_text: str | None
    issuer: str
    public_key: RSAPublicKey


@dataclass
class _ExchangeGate:
    allowed: bool = True

    def validate(self, _: str) -> bool:
        return self.allowed


@dataclass
class _TokenChainHarness:
    operator: httpx.AsyncClient
    tool_calls: ToolCallApplicationService
    agent_actor: AgentActor
    downstream_url: str
    csrf: str
    stored_reference: str
    exchange_gate: _ExchangeGate
    exchanged_assertions: list[str]
    backend_calls: list[_ProtectedBackendCall]
    issuer: str
    public_key: RSAPublicKey

    async def exercise(self, *, exchange_allowed: bool) -> _TokenChainResult:
        self.exchange_gate.allowed = exchange_allowed
        direct_result_is_error: bool | None = None
        direct_result_text: str | None = None
        if not exchange_allowed:
            # Pin the downstream MCP protocol behavior independently of the
            # console's conversion of CallToolResult(isError=True) into its
            # persisted error status.
            async with Client(f"{self.downstream_url}/mcp", auth=self.stored_reference) as downstream_client:
                direct_result = await downstream_client.call_tool_mcp("get_system_info", {})
            direct_result_is_error = direct_result.isError
            direct_result_text = "\n".join(
                block.text for block in direct_result.content if isinstance(block, mcp_types.TextContent)
            )

        submitted = await self.tool_calls.submit_and_wait(
            req=SubmitToolCallRequest(
                server_id="grocy-test",
                tool_name="get_system_info",
                arguments={},
                rationale="verify Grocy connectivity",
                wait_for_ms=0,
            ),
            actor=self.agent_actor,
        )
        assert submitted.status == "pending_approval"

        decided = await self.operator.post(
            f"/api/tool-calls/{submitted.tool_call_id}/decision",
            headers={"X-CSRF-Token": self.csrf},
            json={"decision": "approve"},
        )
        assert decided.status_code == 200, decided.text
        return _TokenChainResult(
            tool_call=decided.json()["tool_call"],
            stored_reference=self.stored_reference,
            exchanged_assertions=self.exchanged_assertions,
            backend_calls=self.backend_calls,
            direct_result_is_error=direct_result_is_error,
            direct_result_text=direct_result_text,
            issuer=self.issuer,
            public_key=self.public_key,
        )


def _protected_backend(
    *, issuer: str, audience: str, public_key: RSAPublicKey, calls: list[_ProtectedBackendCall]
) -> Starlette:
    async def get_system_info(request: Request) -> JSONResponse:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            return JSONResponse({"error": "missing bearer"}, status_code=401)
        try:
            claims = jwt.decode(token, public_key, algorithms=["RS256"], issuer=issuer, audience=audience)
        except jwt.PyJWTError as error:
            return JSONResponse({"error": str(error)}, status_code=401)
        calls.append(_ProtectedBackendCall(operator_subject=claims["sub"], path=request.url.path))
        return JSONResponse({"grocy_version": "integration-test", "operator_subject": claims["sub"]})

    return Starlette(routes=[Route("/api/system/info", get_system_info, methods=["GET"])])


def _downstream_mcp(*, auth_config: AuthentikAuthConfig, backend_url: str, postgres_url: str) -> Starlette:
    settings = ServerSettings(
        grocy_url=backend_url,
        auth=auth_config,
        persistence=PostgresPersistence(
            kind="postgres",
            url=postgres_url.replace("postgresql+psycopg://", "postgresql://", 1),
            table_name="mcp_token_exchange_integration_oauth",
        ),
    )
    return build_server(settings).http_app(path="/mcp")


def _console_config(path: Path, downstream_url: str) -> Path:
    return write_config(
        path,
        {
            "static_agents": [
                {
                    "agent_id": "20000000-0000-4000-8000-000000000001",
                    "display_name": "Haku",
                    "token_env_var": _AGENT_TOKEN_ENV,
                    "operator_subject_env": _AGENT_OPERATOR_ENV,
                }
            ],
            "mcp": {
                "servers": [
                    {
                        "id": "grocy-test",
                        "server_url": f"{downstream_url}/mcp",
                        "auth": {"kind": "remote_server_oauth", "client_name": "Haku Console"},
                    }
                ]
            },
        },
    )


def _hidden_input(page: str, name: str) -> str:
    match = re.search(rf'<input[^>]+name="{name}"[^>]+value="([^"]+)"', page)
    if match is None:
        raise AssertionError(f"OAuth consent page has no {name!r} input")
    return match.group(1)


async def _approve_downstream_consent(client: httpx.AsyncClient, authorization_url: str) -> httpx.Response:
    response = await client.get(authorization_url, follow_redirects=False)
    while response.is_redirect:
        response = await client.get(urljoin(str(response.url), response.headers["location"]), follow_redirects=False)
    assert response.status_code == 200, response.text
    assert "Application Access Request" in response.text
    return await client.post(
        str(response.url),
        data={
            "txn_id": _hidden_input(response.text, "txn_id"),
            "csrf_token": _hidden_input(response.text, "csrf_token"),
            "action": "approve",
        },
        follow_redirects=True,
    )


@pytest.fixture(scope="module")
def oidc_key_pair() -> tuple[RSAPrivateKey, RSAPublicKey]:
    return generate_rsa_keypair()


@pytest.fixture
async def token_chain_harness(
    migrated_db_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    oidc_key_pair: tuple[RSAPrivateKey, RSAPublicKey],
) -> AsyncIterator[_TokenChainHarness]:
    monkeypatch.setenv(_AGENT_TOKEN_ENV, _AGENT_TOKEN)
    monkeypatch.setenv(_AGENT_OPERATOR_ENV, _OPERATOR_SUBJECT)
    private_key, public_key = oidc_key_pair
    idp_port, backend_port, downstream_port, console_port = (pick_free_port() for _ in range(4))
    idp_base = f"http://127.0.0.1:{idp_port}"
    issuer = f"{idp_base}/application/o/grocy-test/"
    backend_url = f"http://127.0.0.1:{backend_port}"
    downstream_url = f"http://127.0.0.1:{downstream_port}"
    console_url = f"http://127.0.0.1:{console_port}"
    exchange_gate = _ExchangeGate()
    exchanged_assertions: list[str] = []
    backend_calls: list[_ProtectedBackendCall] = []
    idp = build_mock_oidc_app(
        issuer_url=issuer,
        private_key=private_key,
        public_key=public_key,
        subject=_OPERATOR_SUBJECT,
        extra_id_token_claims={"preferred_username": _OPERATOR_USERNAME},
        authentik_compatible=True,
        exchange_token_audience=_PROXY_CLIENT_ID,
        expected_exchange_client_id=_PROXY_CLIENT_ID,
        on_client_assertion=exchanged_assertions.append,
        client_assertion_validator=exchange_gate.validate,
    )
    backend = _protected_backend(issuer=issuer, audience=_PROXY_CLIENT_ID, public_key=public_key, calls=backend_calls)

    async with AsyncExitStack() as stack:
        await stack.enter_async_context(serve_app(idp, port=idp_port))
        auth_config = AuthentikAuthConfig(
            oidc_issuer=issuer,
            oidc_client_id="grocy-mcp",
            oidc_client_secret="grocy-mcp-secret",
            public_base_url=downstream_url,
            proxy_client_id=_PROXY_CLIENT_ID,
        )
        downstream_app = _downstream_mcp(auth_config=auth_config, backend_url=backend_url, postgres_url=migrated_db_url)
        settings = console_settings(
            migrated_db_url,
            haku_ui_url="about:blank",
            config_file=_console_config(tmp_path / "console.yaml", downstream_url),
            public_base_url=console_url,
            csrf_secret=SecretStr("csrf-secret"),
            operator_oidc=OperatorOidcConfig(
                issuer=issuer,
                client_id="haku-console",
                client_secret=SecretStr("haku-console-secret"),
                session_secret=SecretStr("session-secret"),
            ),
        )
        console = create_app(settings)
        await stack.enter_async_context(serve_app(backend, port=backend_port))
        await stack.enter_async_context(serve_app(downstream_app, port=downstream_port))
        await stack.enter_async_context(serve_app(console, port=console_port))
        operator = await stack.enter_async_context(httpx.AsyncClient(base_url=console_url, follow_redirects=True))
        await operator.get("/auth/login")
        me = await operator.get("/auth/me")
        assert me.status_code == 200, me.text
        assert me.json() == {"username": _OPERATOR_USERNAME}

        csrf = (await operator.get("/api/capabilities/csrf")).json()["csrf_token"]
        connected = await operator.post("/api/mcp/operator-auth/grocy-test/connect", headers={"X-CSRF-Token": csrf})
        assert connected.status_code == 200, connected.text
        callback = await _approve_downstream_consent(operator, connected.json()["authorization_url"])
        assert callback.status_code == 200, callback.text
        assert "Connected grocy-test" in callback.text

        server_entry = McpServerEntry(
            id="grocy-test", server_url=f"{downstream_url}/mcp", auth=RemoteServerOAuthAuth(client_name="Haku Console")
        )
        operator_id = console.state.operator_identity_store.resolve_configured_external_user_key(_OPERATOR_SUBJECT)
        stored_reference = await console.state.mcp_operator_oauth_store.access_token_for(
            server=server_entry, operator_id=operator_id
        )
        assert stored_reference is not None
        authorization = await console.state.agent_enrollment_service.static_authorization_for_fingerprint(
            fingerprint=fingerprint_static_token(_AGENT_TOKEN)
        )

        yield _TokenChainHarness(
            operator=operator,
            tool_calls=console.state.tool_call_service,
            agent_actor=AgentActor(
                agent_id=authorization.agent_id,
                operator_id=authorization.operator_id,
                binding_id=authorization.binding_id,
            ),
            downstream_url=downstream_url,
            csrf=csrf,
            stored_reference=stored_reference,
            exchange_gate=exchange_gate,
            exchanged_assertions=exchanged_assertions,
            backend_calls=backend_calls,
            issuer=issuer,
            public_key=public_key,
        )


async def test_static_agent_executes_with_operator_identity_through_full_token_chain(
    token_chain_harness: _TokenChainHarness,
) -> None:
    result = await token_chain_harness.exercise(exchange_allowed=True)
    assert result.tool_call["status"] == "ok"
    assert result.backend_calls == [_ProtectedBackendCall(operator_subject=_OPERATOR_SUBJECT, path="/api/system/info")]
    assert result.direct_result_is_error is None
    assert result.direct_result_text is None
    assert len(result.exchanged_assertions) == 1
    assert result.exchanged_assertions[0] != result.stored_reference
    assertion_claims = jwt.decode(
        result.exchanged_assertions[0],
        result.public_key,
        algorithms=["RS256"],
        issuer=result.issuer,
        options={"verify_aud": False},
    )
    assert assertion_claims["sub"] == _OPERATOR_SUBJECT
    local_reference_claims = jwt.decode(result.stored_reference, options={"verify_signature": False})
    assert local_reference_claims["iss"] != assertion_claims["iss"]


async def test_rejected_exchange_fails_before_tool_dispatch(token_chain_harness: _TokenChainHarness) -> None:
    result = await token_chain_harness.exercise(exchange_allowed=False)
    assert result.tool_call["status"] == "error"
    assert result.tool_call["result"] is None
    assert "Backend authentication failed" in result.tool_call["error"]
    assert "invalid_grant" not in result.tool_call["error"]
    assert result.direct_result_is_error is True
    assert result.direct_result_text is not None
    assert "Backend authentication failed" in result.direct_result_text
    assert "invalid_grant" not in result.direct_result_text
    assert result.backend_calls == []
    assert len(result.exchanged_assertions) == 2


if __name__ == "__main__":
    pytest_bazel.main()
