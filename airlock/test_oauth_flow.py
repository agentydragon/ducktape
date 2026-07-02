"""Test the full MCP OAuth flow: DCR → authorize → token → authenticated MCP call.

Uses a mock OIDC server (minimal Starlette app) so the test is fully hermetic —
no real Authentik needed. Exercises OIDCProxy + MultiAuth wiring end-to-end.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from pathlib import Path
from textwrap import dedent
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import jwt as pyjwt
import pytest
import pytest_bazel
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
from fastmcp import FastMCP
from fastmcp.mcp_config import RemoteMCPServer
from fastmcp.server.auth import MultiAuth
from fastmcp.server.auth.oidc_proxy import OIDCProxy
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Mount, Route

from airlock.app import create_app
from airlock.config import Settings
from airlock.conftest import GateClient, agent_transport, serve_app
from airlock.oauth.provider import OAuthConfig
from mcp_infra.prefix import MCPMountPrefix
from util.net import pick_free_port

# ── Mock OIDC Server ─────────────────────────────────────────────────────────


def _build_jwks(public_key) -> dict[str, Any]:
    """Build a JWKS document from an RSA public key."""
    numbers: RSAPublicNumbers = public_key.public_numbers()

    def _b64url(n: int, length: int) -> str:
        return base64.urlsafe_b64encode(n.to_bytes(length, "big")).rstrip(b"=").decode()

    e_bytes = (numbers.e.bit_length() + 7) // 8
    n_bytes = (numbers.n.bit_length() + 7) // 8
    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": "test-key",
                "n": _b64url(numbers.n, n_bytes),
                "e": _b64url(numbers.e, e_bytes),
            }
        ]
    }


def _sign_jwt(private_key, claims: dict[str, Any]) -> str:
    """Sign a JWT with the test RSA key."""
    return pyjwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test-key"})


def build_mock_oidc_app(*, issuer_url: str, private_key, public_key) -> Starlette:
    """Build a minimal OIDC provider that supports discovery, JWKS, authorize, and token."""
    jwks = _build_jwks(public_key)
    pending_codes: dict[str, dict[str, Any]] = {}

    async def discovery(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "issuer": issuer_url,
                "authorization_endpoint": f"{issuer_url}/authorize",
                "token_endpoint": f"{issuer_url}/token",
                "jwks_uri": f"{issuer_url}/jwks",
                "response_types_supported": ["code"],
                "subject_types_supported": ["public"],
                "id_token_signing_alg_values_supported": ["RS256"],
                "scopes_supported": ["openid", "propose", "read", "decide"],
                "grant_types_supported": ["authorization_code"],
            }
        )

    async def jwks_endpoint(request: Request) -> JSONResponse:
        return JSONResponse(jwks)

    async def authorize(request: Request) -> RedirectResponse:
        """Simulate user consent + login by immediately issuing an auth code."""
        params = dict(request.query_params)
        code = hashlib.sha256(f"{time.time()}".encode()).hexdigest()[:16]
        pending_codes[code] = {
            "redirect_uri": params.get("redirect_uri", ""),
            "scope": params.get("scope", "openid"),
            "code_challenge": params.get("code_challenge"),
        }
        redirect_uri = params["redirect_uri"]
        state = params.get("state", "")
        return RedirectResponse(f"{redirect_uri}?code={code}&state={state}", status_code=302)

    async def token(request: Request) -> JSONResponse:
        """Exchange auth code for tokens."""
        body = parse_qs((await request.body()).decode())
        code = body.get("code", [""])[0]
        code_data = pending_codes.pop(code, None)
        if code_data is None:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        now = int(time.time())
        scope = code_data.get("scope", "openid")
        claims = {
            "iss": issuer_url,
            "sub": "test-user",
            "aud": body.get("resource", [issuer_url])[0],
            "iat": now,
            "exp": now + 3600,
            "scope": scope,
        }
        access_token = _sign_jwt(private_key, claims)
        id_token = _sign_jwt(
            private_key,
            {**claims, "aud": body.get("client_id", ["test"])[0], "azp": body.get("client_id", ["test"])[0]},
        )
        return JSONResponse(
            {
                "access_token": access_token,
                "id_token": id_token,
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": scope,
            }
        )

    return Starlette(
        routes=[
            Route("/.well-known/openid-configuration", discovery),
            Route("/jwks", jwks_endpoint),
            Route("/authorize", authorize),
            Route("/token", token, methods=["POST"]),
        ]
    )


# ── Helpers ──────────────────────────────────────────────────────────────────


_ALL_SCOPES = ["openid", "propose", "read", "decide"]


def _build_multiauth(*, oidc_url: str, airlock_url: str, extra_verifiers: list | None = None) -> MultiAuth:
    """Build MultiAuth with OIDCProxy pointing at the (already-running) mock OIDC server."""
    proxy = OIDCProxy(
        config_url=f"{oidc_url}/.well-known/openid-configuration",
        client_id="airlock-proxy",
        client_secret="test-secret",
        base_url=f"{airlock_url}/mcp",
        require_authorization_consent=False,
    )
    # OIDCProxy doesn't expose valid_scopes; patch DCR validation to accept our scopes.
    assert proxy.client_registration_options is not None
    proxy.client_registration_options.valid_scopes = _ALL_SCOPES
    return MultiAuth(server=proxy, verifiers=extra_verifiers or [])


def _build_settings(
    *, airlock_port: int, airlock_url: str, oidc_url: str, echo_port: int, db_url: str, predicate_path: Path
) -> Settings:
    return Settings(
        backends={MCPMountPrefix("test"): RemoteMCPServer(url=f"http://127.0.0.1:{echo_port}/mcp")},
        public_base_url=airlock_url,
        db_url=db_url,
        predicate_path=predicate_path,
        oidc_issuer=oidc_url,
        oidc_client_id="test",
        oidc_proxy_client_id="airlock-proxy",
        oidc_proxy_client_secret="test-secret",
        oauth=OAuthConfig(providers=[]),
        port=airlock_port,
    )


def _build_echo_backend() -> tuple[FastMCP, int, Starlette]:
    echo = FastMCP("echo")

    @echo.tool()
    async def echo_tool(text: str) -> str:
        return f"echoed: {text}"

    port = pick_free_port()
    echo_app = echo.http_app(path="/")
    starlette = Starlette(routes=[Mount("/mcp", app=echo_app)], lifespan=echo_app.lifespan)
    return echo, port, starlette


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def oidc_key_pair():
    """Generate an RSA key pair for the mock OIDC server."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


@pytest.fixture
def _mock_k8s_store():
    mock_store = MagicMock()
    mock_store.list_secrets = AsyncMock(return_value=[])
    mock_store.delete_orphaned_secrets = AsyncMock()
    with patch("airlock.app.K8sTokenStore.from_incluster", new_callable=AsyncMock, return_value=mock_store):
        yield


@pytest.fixture
def predicate_file(tmp_path: Path) -> Path:
    p = tmp_path / "predicate.py"
    p.write_text(
        dedent("""
        from airlock.predicates import NeedsHumanDecision

        def decide(server_namespace, tool_name, arguments):
            return NeedsHumanDecision()
    """).lstrip()
    )
    return p


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("_mock_k8s_store")
async def test_oauth_discovery_endpoints(oidc_key_pair, predicate_file: Path, db_url: str):
    """OIDCProxy serves well-known endpoints that MCP clients need for auth discovery."""
    private_key, public_key = oidc_key_pair
    oidc_port = pick_free_port()
    airlock_port = pick_free_port()
    oidc_url = f"http://127.0.0.1:{oidc_port}"
    airlock_url = f"http://127.0.0.1:{airlock_port}"

    mock_oidc = build_mock_oidc_app(issuer_url=oidc_url, private_key=private_key, public_key=public_key)
    _, echo_port, echo_starlette = _build_echo_backend()

    # Start mock OIDC first — OIDCProxy.__init__ does a synchronous discovery fetch.
    async with serve_app(mock_oidc, port=oidc_port):
        auth = _build_multiauth(oidc_url=oidc_url, airlock_url=airlock_url)
        settings = _build_settings(
            airlock_port=airlock_port,
            airlock_url=airlock_url,
            oidc_url=oidc_url,
            echo_port=echo_port,
            db_url=db_url,
            predicate_path=predicate_file,
        )
        app = create_app(settings, auth=auth, include_static=False)

        async with (
            serve_app(echo_starlette, port=echo_port),
            serve_app(app, port=airlock_port),
            httpx.AsyncClient(base_url=airlock_url) as http,
        ):
            # 1. Unauthenticated MCP request should get 401
            r = await http.post("/mcp/", content=b"{}", headers={"Content-Type": "application/json"})
            assert r.status_code == 401, f"Expected 401, got {r.status_code}: {r.text[:200]}"
            assert "Bearer" in r.headers.get("www-authenticate", "")

            # 2. Authorization server metadata should be available
            r = await http.get("/mcp/.well-known/oauth-authorization-server")
            assert r.status_code == 200
            asm = r.json()
            assert "authorization_endpoint" in asm
            assert "token_endpoint" in asm
            assert "registration_endpoint" in asm

            # 4. Dynamic Client Registration should work
            r = await http.post(
                asm["registration_endpoint"],
                json={
                    "client_name": "Test MCP Client",
                    "redirect_uris": ["http://localhost:9999/callback"],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                },
            )
            assert r.status_code in (200, 201), f"DCR failed ({r.status_code}): {r.text[:500]}"
            assert "client_id" in r.json()


@pytest.mark.usefixtures("_mock_k8s_store")
async def test_jwt_verifier_fallback(oidc_key_pair, predicate_file: Path, db_url: str):
    """Tokens signed directly (not via OIDCProxy) are accepted via JWTVerifier fallback.

    This verifies the OpenClaw auth-proxy sidecar path still works when MultiAuth is active.
    """
    private_key, public_key = oidc_key_pair
    oidc_port = pick_free_port()
    airlock_port = pick_free_port()
    oidc_url = f"http://127.0.0.1:{oidc_port}"
    airlock_url = f"http://127.0.0.1:{airlock_port}"

    mock_oidc = build_mock_oidc_app(issuer_url=oidc_url, private_key=private_key, public_key=public_key)
    _, echo_port, echo_starlette = _build_echo_backend()

    rsa_key_pair = RSAKeyPair.generate()
    agent_jwt = rsa_key_pair.create_token(subject="agent", scopes=["propose", "read"])

    async with serve_app(mock_oidc, port=oidc_port):
        auth = _build_multiauth(
            oidc_url=oidc_url,
            airlock_url=airlock_url,
            extra_verifiers=[JWTVerifier(public_key=rsa_key_pair.public_key)],
        )
        settings = _build_settings(
            airlock_port=airlock_port,
            airlock_url=airlock_url,
            oidc_url=oidc_url,
            echo_port=echo_port,
            db_url=db_url,
            predicate_path=predicate_file,
        )
        app = create_app(settings, auth=auth, include_static=False)

        async with serve_app(echo_starlette, port=echo_port), serve_app(app, port=airlock_port):
            async with GateClient(agent_transport(airlock_url, agent_jwt)) as client:
                action = await client.call_gate_tool(
                    "test_echo_tool",
                    {
                        "input": {"text": "hello"},
                        "justification": "test",
                        "session_key": "a0000000-0000-0000-0000-000000000001",
                    },
                )
                assert action.state.status.value == "pending"

            async with httpx.AsyncClient(base_url=airlock_url) as http:
                # The same directly-signed agent JWT (read scope) that the extra
                # JWTVerifier accepts must also authenticate the REST API.
                r = await http.get("/api/actions", headers={"Authorization": f"Bearer {agent_jwt}"})
                assert r.status_code == 200
                actions = r.json()
                assert len(actions) == 1
                assert actions[0]["call"]["tool_name"] == "echo_tool"


@pytest.mark.usefixtures("_mock_k8s_store")
async def test_full_oauth_flow_with_tool_call(oidc_key_pair, predicate_file: Path, db_url: str):
    """Walk the complete OAuth flow: DCR → authorize → token → MCP tool call.

    Exercises the OIDCProxy path end-to-end with consent disabled for test simplicity.
    """
    private_key, public_key = oidc_key_pair
    oidc_port = pick_free_port()
    airlock_port = pick_free_port()
    oidc_url = f"http://127.0.0.1:{oidc_port}"
    airlock_url = f"http://127.0.0.1:{airlock_port}"

    mock_oidc = build_mock_oidc_app(issuer_url=oidc_url, private_key=private_key, public_key=public_key)
    _, echo_port, echo_starlette = _build_echo_backend()

    async with serve_app(mock_oidc, port=oidc_port):
        auth = _build_multiauth(oidc_url=oidc_url, airlock_url=airlock_url)
        settings = _build_settings(
            airlock_port=airlock_port,
            airlock_url=airlock_url,
            oidc_url=oidc_url,
            echo_port=echo_port,
            db_url=db_url,
            predicate_path=predicate_file,
        )
        app = create_app(settings, auth=auth, include_static=False)

        async with (
            serve_app(echo_starlette, port=echo_port),
            serve_app(app, port=airlock_port),
            httpx.AsyncClient(base_url=airlock_url, follow_redirects=False) as http,
        ):
            # Step 1: Get authorization server metadata
            r = await http.get("/mcp/.well-known/oauth-authorization-server")
            assert r.status_code == 200
            asm = r.json()

            # Step 2: Dynamic Client Registration
            r = await http.post(
                asm["registration_endpoint"],
                json={
                    "client_name": "Test OAuth Client",
                    "redirect_uris": ["http://127.0.0.1:19876/callback"],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "scope": "openid propose read",
                },
            )
            assert r.status_code in (200, 201), f"DCR failed ({r.status_code}): {r.text[:500]}"
            client_id = r.json()["client_id"]

            # Step 3: Build PKCE challenge
            code_verifier = secrets.token_urlsafe(32)
            code_challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()
            )

            # Step 4: Authorization request (consent disabled → redirects through mock OIDC → back)
            auth_params = {
                "client_id": client_id,
                "redirect_uri": "http://127.0.0.1:19876/callback",
                "response_type": "code",
                "scope": "openid propose read",
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "state": "test-state",
            }
            r = await http.get(f"{asm['authorization_endpoint']}?{urlencode(auth_params)}")

            # Follow redirects: OIDCProxy → mock OIDC → OIDCProxy callback → client callback
            async with httpx.AsyncClient(follow_redirects=False, cookies=http.cookies) as redirect_client:
                while r.status_code in (301, 302, 303, 307):
                    location = r.headers["location"]
                    if location.startswith("http://127.0.0.1:19876/callback"):
                        break
                    r = await redirect_client.get(location)

            # Extract the authorization code from the final redirect
            assert r.status_code in (301, 302, 303, 307), f"Expected redirect, got {r.status_code}: {r.text}"
            callback_url = urlparse(r.headers["location"])
            callback_params = parse_qs(callback_url.query)
            assert "code" in callback_params, f"No code in callback: {r.headers['location']}"
            auth_code = callback_params["code"][0]

            # Step 5: Token exchange
            token_data = {
                "grant_type": "authorization_code",
                "code": auth_code,
                "redirect_uri": "http://127.0.0.1:19876/callback",
                "client_id": client_id,
                "code_verifier": code_verifier,
            }
            r = await http.post(asm["token_endpoint"], data=token_data)
            assert r.status_code == 200, f"Token exchange failed: {r.text}"
            tokens = r.json()
            assert "access_token" in tokens

            # Step 6: Use the token to make an authenticated MCP tool call
            access_token = tokens["access_token"]
            async with GateClient(
                RemoteMCPServer(
                    url=f"{airlock_url}/mcp", headers={"Authorization": f"Bearer {access_token}"}
                ).to_transport()
            ) as client:
                action = await client.call_gate_tool(
                    "test_echo_tool",
                    {
                        "input": {"text": "oauth-hello"},
                        "justification": "oauth test",
                        "session_key": "a0000000-0000-0000-0000-000000000002",
                    },
                )
                assert action.state.status.value == "pending"
                # Verify client_id is tracked on the action
                assert action.client_id is not None


if __name__ == "__main__":
    pytest_bazel.main()
