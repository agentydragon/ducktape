"""Mounted decision gate for Haku-owned agent enrollment around FastMCP OAuth."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import re
import secrets
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

import httpx
import pytest_bazel
from authlib.integrations.starlette_client import OAuth
from fastapi import FastAPI, HTTPException, Request
from fastmcp import Client, FastMCP
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from fastmcp.tools import Tool, ToolResult
from key_value.aio.stores.memory import MemoryStore
from mcp import types as mcp_types
from pydantic import SecretStr
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

from haku.console.config import OperatorOidcConfig
from haku.console.mcp_agent_enrollment_spike import (
    DEFAULT_TOMBSTONE_TTL_SECONDS,
    FASTMCP_CODE_TTL_SECONDS,
    FASTMCP_TRANSACTION_TTL_SECONDS,
    ActiveGrant,
    AgentEnrollmentOIDCProxy,
    AgentGrantMiddleware,
    AuthorizationTuple,
    CanonicalOperatorMatcher,
    ClosedEnrollment,
    ClosedReason,
    GrantCore,
    InMemoryAgentEnrollmentStore,
    IssuedGrant,
    IssuerSubject,
    IssuingGrant,
    OperatorIdentity,
    RevokedGrant,
    assert_fastmcp_enrollment_compatibility,
    build_agent_enrollment_router,
)
from haku.console.operator_auth import AUTHENTIK_CLIENT_NAME, build_oauth
from mcp_infra.authentik_auth.oidc_principal import VerifiedOidcPrincipal
from util.net import pick_free_port
from util.testing.asgi import serve_app
from util.testing.mock_oidc import build_mock_oidc_app, generate_rsa_keypair

_SUBJECT = "operator-person-42"
_USERNAME = "agentydragon"
_CLIENT_CALLBACK = "https://client.example/%3Cscript%3E/oauth/callback"
_SCOPES = "openid offline_access"
_ALL_SCOPES = ["openid", "email", "profile", "offline_access"]


@dataclass
class _Clock:
    now: float = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class _ScopeControl:
    next_scope: str | None = None
    jwks_failures_remaining: int = 0
    refresh_exchanges: int = 0


class _MountedProxyTool(Tool):
    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(content=[mcp_types.TextContent(type="text", text=f"proxy:{arguments['value']}")])


class _WireScopeTranslatingEnrollmentProxy(AgentEnrollmentOIDCProxy):
    """Test a provider whose IdP-wire scopes differ from MCP-facing scopes."""

    def _translate_scopes_from_idp(self, scopes: list[str]) -> list[str]:
        translated = [scope.removeprefix("authentik:") for scope in scopes]
        return super()._translate_scopes_from_idp(translated)


class _PausingAgentEnrollmentStore(InMemoryAgentEnrollmentStore):
    """Pause the winner after its Haku CAS but before FastMCP issuance."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.exchange_claimed = threading.Event()
        self.continue_exchange = threading.Event()

    async def begin_exchange(
        self,
        *,
        correlation: AuthorizationTuple,
        client_id: str,
        principal: VerifiedOidcPrincipal,
        matcher: CanonicalOperatorMatcher,
        granted_scopes: frozenset[str],
    ) -> GrantCore:
        core = await super().begin_exchange(
            correlation=correlation,
            client_id=client_id,
            principal=principal,
            matcher=matcher,
            granted_scopes=granted_scopes,
        )
        self.exchange_claimed.set()
        await asyncio.to_thread(self.continue_exchange.wait)
        return core


@dataclass(frozen=True)
class _RegisteredClient:
    client_id: str
    client_secret: str | None
    client_name: str
    redirect_uri: str


@dataclass(frozen=True)
class _AuthorizationStart:
    client: _RegisteredClient
    verifier: str
    challenge: str
    enrollment_url: str
    state: str
    scopes: str


@dataclass(frozen=True)
class _DownstreamCode:
    start: _AuthorizationStart
    code: str


@dataclass(frozen=True)
class _IssuedToken:
    client: _RegisteredClient
    access_token: str
    refresh_token: str
    grant_id: str


@dataclass
class _Harness:
    base_url: str
    browser_issuer: str
    mcp_issuer: str
    http: httpx.AsyncClient
    second_browser: httpx.AsyncClient
    store: InMemoryAgentEnrollmentStore
    proxy: AgentEnrollmentOIDCProxy
    middleware: AgentGrantMiddleware
    matcher_anchors: dict[IssuerSubject, str]
    clock: _Clock
    scope_control: _ScopeControl

    async def register(self, *, name: str, redirect_uri: str = _CLIENT_CALLBACK) -> _RegisteredClient:
        metadata = (await self.http.get("/mcp/.well-known/oauth-authorization-server")).json()
        response = await self.http.post(
            metadata["registration_endpoint"],
            json={
                "client_name": name,
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
                "scope": " ".join(_ALL_SCOPES),
            },
        )
        assert response.status_code == 201, response.text
        registered = response.json()
        return _RegisteredClient(
            client_id=registered["client_id"],
            client_secret=registered.get("client_secret"),
            client_name=name,
            redirect_uri=redirect_uri,
        )

    async def start(
        self,
        client: _RegisteredClient,
        *,
        scopes: str = _SCOPES,
        verifier: str | None = None,
        state: str | None = None,
        redirect_uri: str | None = None,
        resource: str | None = None,
        code_challenge_method: str = "S256",
    ) -> httpx.Response | _AuthorizationStart:
        verifier = verifier or secrets.token_urlsafe(32)
        challenge = _challenge(verifier)
        state = state or secrets.token_urlsafe(12)
        redirect_uri = redirect_uri or client.redirect_uri
        response = await self.http.get(
            "/mcp/authorize",
            params={
                "response_type": "code",
                "client_id": client.client_id,
                "redirect_uri": redirect_uri,
                "scope": scopes,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": code_challenge_method,
                "resource": resource or f"{self.base_url}/mcp",
            },
        )
        if response.status_code != 302 or not response.headers["location"].startswith(
            f"{self.base_url}/agent-enrollment/"
        ):
            return response
        return _AuthorizationStart(
            client=client,
            verifier=verifier,
            challenge=challenge,
            enrollment_url=response.headers["location"],
            state=state,
            scopes=scopes,
        )

    async def page(self, start: _AuthorizationStart, *, browser: httpx.AsyncClient | None = None) -> httpx.Response:
        browser = browser or self.http
        page = await browser.get(start.enrollment_url)
        if page.status_code == 303:
            login = await browser.get(page.headers["location"])
            assert login.status_code == 302
            cookie = login.headers["set-cookie"].lower()
            assert "secure" in cookie
            assert "httponly" in cookie
            assert "samesite=lax" in cookie
            _allow_secure_cookie_over_loopback_test_transport(browser)
            browser_callback = await browser.get(login.headers["location"])
            assert browser_callback.status_code == 302
            returned = await browser.get(browser_callback.headers["location"])
            assert returned.status_code == 303, returned.text
            _allow_secure_cookie_over_loopback_test_transport(browser)
            page = await browser.get(returned.headers["location"])
            _allow_secure_cookie_over_loopback_test_transport(browser)
        return page

    async def approve(
        self,
        start: _AuthorizationStart,
        *,
        agent_name: str,
        page: httpx.Response | None = None,
        browser: httpx.AsyncClient | None = None,
        origin: str | None = None,
        csrf: str | None = None,
    ) -> httpx.Response:
        browser = browser or self.http
        page = page or await self.page(start, browser=browser)
        locator = urlparse(start.enrollment_url).path.rsplit("/", 1)[1]
        return await browser.post(
            f"/agent-enrollment/{locator}",
            headers={"Origin": origin or self.base_url},
            data={"csrf": csrf or _hidden(page.text, "csrf"), "agent_name": agent_name, "action": "approve"},
        )

    async def deny(self, start: _AuthorizationStart, page: httpx.Response) -> httpx.Response:
        locator = urlparse(start.enrollment_url).path.rsplit("/", 1)[1]
        return await self.http.post(
            f"/agent-enrollment/{locator}",
            headers={"Origin": self.base_url},
            data={"csrf": _hidden(page.text, "csrf"), "action": "deny"},
        )

    async def upstream_callback(self, approval: httpx.Response) -> httpx.Response:
        assert approval.status_code == 303, approval.text
        upstream = await self.http.get(approval.headers["location"])
        assert upstream.status_code == 302, upstream.text
        return upstream

    async def downstream_code(self, start: _AuthorizationStart, approval: httpx.Response) -> _DownstreamCode:
        upstream = await self.upstream_callback(approval)
        downstream = await self.http.get(upstream.headers["location"])
        assert downstream.status_code == 302, downstream.text
        callback = httpx.URL(downstream.headers["location"])
        assert str(callback.copy_with(query=None)) == start.client.redirect_uri
        assert callback.params["state"] == start.state
        return _DownstreamCode(start=start, code=callback.params["code"])

    async def token(self, downstream: _DownstreamCode, *, verifier: str | None = None) -> httpx.Response:
        data = {
            "grant_type": "authorization_code",
            "code": downstream.code,
            "redirect_uri": downstream.start.client.redirect_uri,
            "client_id": downstream.start.client.client_id,
            "code_verifier": verifier or downstream.start.verifier,
        }
        if downstream.start.client.client_secret is not None:
            data["client_secret"] = downstream.start.client.client_secret
        return await self.http.post("/mcp/token", data=data)

    async def issue(self, *, client_name: str, agent_name: str) -> _IssuedToken:
        client = await self.register(name=client_name)
        started = await self.start(client)
        assert isinstance(started, _AuthorizationStart)
        approval = await self.approve(started, agent_name=agent_name)
        response = await self.token(await self.downstream_code(started, approval))
        assert response.status_code == 200, response.text
        body = response.json()
        claims = self.proxy.jwt_issuer.verify_token(body["access_token"], expected_token_use="access")
        return _IssuedToken(
            client=client,
            access_token=body["access_token"],
            refresh_token=body["refresh_token"],
            grant_id=claims["upstream_claims"]["grant_id"],
        )

    async def login_second_browser(self) -> None:
        login = await self.second_browser.get("/operator/login", params={"return_to": f"{self.base_url}/after-login"})
        _allow_secure_cookie_over_loopback_test_transport(self.second_browser)
        oidc = await self.second_browser.get(login.headers["location"])
        callback = await self.second_browser.get(oidc.headers["location"])
        _allow_secure_cookie_over_loopback_test_transport(self.second_browser)
        assert callback.status_code == 303


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def _hidden(page: str, name: str) -> str:
    match = re.search(rf'<input[^>]+name="{name}"[^>]+value="([^"]+)"', page)
    if match is None:
        raise AssertionError(f"missing hidden input {name}")
    return match.group(1)


def _allow_secure_cookie_over_loopback_test_transport(client: httpx.AsyncClient) -> None:
    """The mounted server is HTTP-only; retain production cookie flags while exercising it."""
    for cookie in client.cookies.jar:
        cookie.secure = False


def _operator_from_request(request: Request, *, issuer: str) -> OperatorIdentity | None:
    value = request.session.get("operator")
    if not isinstance(value, dict):
        return None
    subject = value.get("subject")
    username = value.get("username")
    session_id = value.get("session_id")
    if not isinstance(subject, str) or not isinstance(username, str) or not isinstance(session_id, str):
        return None
    return OperatorIdentity(issuer=issuer, subject=subject, username=username, session_id=session_id)


def _controlled_oidc_app(*, issuer: str, control: _ScopeControl):
    private_key, public_key = generate_rsa_keypair()
    original = build_mock_oidc_app(
        issuer_url=issuer,
        private_key=private_key,
        public_key=public_key,
        subject=_SUBJECT,
        extra_id_token_claims={"preferred_username": _USERNAME},
        authentik_compatible=True,
    )
    routes = []
    for route in original.routes:
        if not isinstance(route, Route):
            routes.append(route)
            continue
        if route.path.endswith("/token/"):
            routes.append(Route(route.path, _controlled_token(route.endpoint, control), methods=["POST"]))
        elif route.path.endswith("/jwks/"):
            routes.append(Route(route.path, _controlled_jwks(route.endpoint, control), methods=["GET"]))
        else:
            routes.append(route)
    return FastAPI(routes=routes)


def _controlled_token(endpoint: Any, control: _ScopeControl):
    async def handle(request: Request):
        body = parse_qs((await request.body()).decode())
        is_refresh = body.get("grant_type") == ["refresh_token"]
        response = await endpoint(request)
        if is_refresh:
            control.refresh_exchanges += 1
        if control.next_scope is None or response.status_code != 200:
            return response
        payload = json.loads(response.body)
        payload["scope"] = control.next_scope
        control.next_scope = None
        return JSONResponse(payload)

    return handle


def _controlled_jwks(endpoint: Any, control: _ScopeControl):
    async def handle(request: Request):
        if control.jwks_failures_remaining > 0:
            control.jwks_failures_remaining -= 1
            return JSONResponse({"error": "temporarily_unavailable"}, status_code=503)
        return await endpoint(request)

    return handle


@asynccontextmanager
async def _running_harness(
    *, clock: _Clock | None = None, pause_after_exchange_claim: bool = False
) -> AsyncIterator[_Harness]:
    browser_port, mcp_idp_port, console_port = (pick_free_port() for _ in range(3))
    browser_issuer = f"http://127.0.0.1:{browser_port}/application/o/operator/"
    mcp_issuer = f"http://127.0.0.1:{mcp_idp_port}/application/o/mcp-agent/"
    base_url = f"http://127.0.0.1:{console_port}"
    browser_private, browser_public = generate_rsa_keypair()
    browser_idp = build_mock_oidc_app(
        issuer_url=browser_issuer,
        private_key=browser_private,
        public_key=browser_public,
        subject=_SUBJECT,
        extra_id_token_claims={"preferred_username": _USERNAME},
        authentik_compatible=True,
    )
    scope_control = _ScopeControl()
    mcp_idp = _controlled_oidc_app(issuer=mcp_issuer, control=scope_control)
    clock = clock or _Clock()

    async with serve_app(browser_idp, port=browser_port), serve_app(mcp_idp, port=mcp_idp_port):
        store = (
            _PausingAgentEnrollmentStore(public_origin=base_url, clock=clock)
            if pause_after_exchange_claim
            else InMemoryAgentEnrollmentStore(public_origin=base_url, clock=clock)
        )
        anchors = {
            IssuerSubject(browser_issuer, _SUBJECT): "operator-anchor-42",
            IssuerSubject(mcp_issuer, _SUBJECT): "operator-anchor-42",
        }
        proxy = _WireScopeTranslatingEnrollmentProxy(
            config_url=f"{mcp_issuer}.well-known/openid-configuration",
            client_id="haku-agent-facing",
            client_secret="haku-agent-secret",
            base_url=f"{base_url}/mcp",
            client_storage=MemoryStore(),
            expected_issuer=mcp_issuer,
            enrollment_store=store,
            operator_matcher=CanonicalOperatorMatcher(anchors=anchors),
        )
        proxy.update_default_scopes(_ALL_SCOPES)
        mcp = FastMCP("Agent enrollment integration", auth=proxy)

        @mcp.tool
        async def function_echo(value: str) -> str:
            return f"function:{value}"

        mcp.add_tool(
            _MountedProxyTool(
                name="proxy_echo",
                description="custom ProxyTool-shaped test double",
                parameters={"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
            )
        )
        middleware = AgentGrantMiddleware(store)
        mcp.add_middleware(middleware)
        mcp_app = mcp.http_app(path="/")
        app = FastAPI(lifespan=mcp_app.lifespan)
        app.add_middleware(
            SessionMiddleware, secret_key="mounted-enrollment-session-secret", https_only=True, same_site="lax"
        )
        oauth: OAuth = build_oauth(
            OperatorOidcConfig(
                issuer=browser_issuer,
                client_id="haku-browser",
                client_secret=SecretStr("haku-browser-secret"),
                session_secret=SecretStr("unused-by-test-parent"),
            )
        )

        @app.get("/operator/login")
        async def operator_login(request: Request, return_to: str) -> RedirectResponse:
            if not return_to.startswith(f"{base_url}/"):
                raise AssertionError("test login received an external return URL")
            request.session["operator_return_to"] = return_to
            client = oauth.create_client(AUTHENTIK_CLIENT_NAME)
            return cast(RedirectResponse, await client.authorize_redirect(request, f"{base_url}/operator/callback"))

        @app.get("/operator/callback")
        async def operator_callback(request: Request) -> RedirectResponse:
            client = oauth.create_client(AUTHENTIK_CLIENT_NAME)
            token = await client.authorize_access_token(request)
            userinfo = token["userinfo"]
            request.session["operator"] = {
                "subject": userinfo["sub"],
                "username": userinfo["preferred_username"],
                "session_id": secrets.token_urlsafe(24),
            }
            return RedirectResponse(request.session.pop("operator_return_to"), status_code=303)

        app.include_router(
            build_agent_enrollment_router(
                store=store,
                operator_from_request=lambda request: _operator_from_request(request, issuer=browser_issuer),
                login_path="/operator/login",
            )
        )

        @app.api_route("/mcp/consent", methods=["GET", "POST"])
        async def retired_fastmcp_consent_surface() -> None:
            raise HTTPException(status_code=404, detail="external enrollment owns consent")

        # FastMCP advertises resource metadata at the RFC 9728 root path even
        # when its operational OAuth surface is mounted. Reuse its own route
        # object in the parent rather than reproducing the metadata handler.
        app.router.routes.extend(
            route
            for route in mcp_app.routes
            if isinstance(route, Route) and route.path.startswith("/.well-known/oauth-protected-resource")
        )
        app.mount("/mcp", mcp_app)

        async with (
            serve_app(app, port=console_port),
            httpx.AsyncClient(base_url=base_url, follow_redirects=False) as http,
            httpx.AsyncClient(base_url=base_url, follow_redirects=False) as second_browser,
        ):
            yield _Harness(
                base_url=base_url,
                browser_issuer=browser_issuer,
                mcp_issuer=mcp_issuer,
                http=http,
                second_browser=second_browser,
                store=store,
                proxy=proxy,
                middleware=middleware,
                matcher_anchors=anchors,
                clock=clock,
                scope_control=scope_control,
            )


async def test_mounted_full_flow_refresh_and_tool_activation() -> None:
    async with _running_harness() as harness:
        metadata = await harness.http.get("/mcp/.well-known/oauth-authorization-server")
        assert metadata.status_code == 200
        assert metadata.json()["registration_endpoint"] == f"{harness.base_url}/mcp/register"
        challenge = await harness.http.get("/mcp/")
        assert challenge.status_code == 401
        resource_metadata = re.search(r'resource_metadata="([^"]+)"', challenge.headers["www-authenticate"])
        assert resource_metadata is not None
        protected = await harness.http.get(resource_metadata.group(1))
        assert protected.status_code == 200

        client = await harness.register(name="<script>alert('client')</script>")
        started = await harness.start(client)
        assert isinstance(started, _AuthorizationStart)
        assert harness.store.agents() == ()
        page = await harness.page(started)
        assert page.status_code == 200
        assert "&lt;script&gt;alert(&#39;client&#39;)&lt;/script&gt;" in page.text
        assert "<script>alert('client')</script>" not in page.text
        assert "Redirect host: <strong>client.example</strong>" in page.text
        assert "%3Cscript%3E" not in page.text
        assert _USERNAME in page.text
        assert page.headers["cache-control"] == "no-store"
        assert page.headers["referrer-policy"] == "no-referrer"
        csp = page.headers["content-security-policy"]
        assert "form-action 'self'" in csp
        assert "script-src 'none'" in csp
        assert "object-src 'none'" in csp
        assert "base-uri 'none'" in csp
        assert harness.store.agents() == ()

        # The one-time browser nonce cannot reopen the approval UI.
        assert (await harness.http.get(started.enrollment_url)).status_code == 403
        approval = await harness.approve(started, agent_name="  My   Agent  ", page=page)
        assert approval.status_code == 303
        assert harness.store.agents() == ()
        harness.scope_control.next_scope = "authentik:openid authentik:offline_access"
        downstream = await harness.downstream_code(started, approval)

        wrong_pkce = await harness.token(downstream, verifier="wrong-verifier")
        assert wrong_pkce.status_code == 401
        assert wrong_pkce.json()["error"] == "invalid_grant"
        assert harness.store.agents() == ()

        # A transient P1 verifier outage crosses the mounted token route as a
        # retryable service error and does not consume the downstream code.
        harness.scope_control.jwks_failures_remaining = 1
        unavailable = await harness.token(downstream)
        assert unavailable.status_code == 503
        assert unavailable.headers["retry-after"] == "60"
        assert harness.store.agents() == ()
        assert harness.store.grants() == ()
        exchanged = await harness.token(downstream)
        assert exchanged.status_code == 200, exchanged.text
        body = exchanged.json()
        access_claims = harness.proxy.jwt_issuer.verify_token(body["access_token"], expected_token_use="access")
        refresh_claims = harness.proxy.jwt_issuer.verify_token(body["refresh_token"], expected_token_use="refresh")
        assert access_claims["upstream_claims"].keys() == {"grant_id"}
        assert refresh_claims["upstream_claims"] == access_claims["upstream_claims"]
        grant_id = access_claims["upstream_claims"]["grant_id"]
        assert access_claims["scope"] == _SCOPES
        assert refresh_claims["scope"] == _SCOPES
        assert harness.store.agents()[0].display_name == "My Agent"
        issued_grant = harness.store.grant(grant_id)
        assert isinstance(issued_grant, IssuedGrant)
        assert issued_grant.core.allowed_scopes == frozenset(_SCOPES.split())
        assert all(not scope.startswith("authentik:") for scope in issued_grant.core.allowed_scopes)
        issuance_evidence = issued_grant.evidence

        async with Client(f"{harness.base_url}/mcp", auth=body["access_token"]) as client_session:
            names = {tool.name for tool in await client_session.list_tools()}
            assert {"function_echo", "proxy_echo"} <= names
            assert isinstance(harness.store.grant(grant_id), IssuedGrant)
            result = await client_session.call_tool("function_echo", {"value": "one"})
            assert result.data == "function:one"
            assert isinstance(harness.store.grant(grant_id), ActiveGrant)
        assert harness.middleware.dispatched_tools == ["function_echo"]

        harness.scope_control.next_scope = "authentik:openid authentik:offline_access"
        refreshed = await harness.http.post(
            "/mcp/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": body["refresh_token"],
                "client_id": client.client_id,
                "scope": _SCOPES,
            },
        )
        assert refreshed.status_code == 200, refreshed.text
        refreshed_claims = harness.proxy.jwt_issuer.verify_token(
            refreshed.json()["access_token"], expected_token_use="access"
        )
        assert refreshed_claims["upstream_claims"] == {"grant_id": grant_id}
        rotated_refresh_claims = harness.proxy.jwt_issuer.verify_token(
            refreshed.json()["refresh_token"], expected_token_use="refresh"
        )
        assert rotated_refresh_claims["jti"] != issuance_evidence.refresh_jti
        refreshed_grant = harness.store.grant(grant_id)
        assert isinstance(refreshed_grant, ActiveGrant)
        assert refreshed_grant.evidence == issuance_evidence
        broadened = await harness.http.post(
            "/mcp/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refreshed.json()["refresh_token"],
                "client_id": client.client_id,
                "scope": "openid profile",
            },
        )
        assert broadened.status_code == 400
        assert broadened.json()["error"] == "invalid_scope"

        second = await harness.issue(client_name="custom tool caller", agent_name="Proxy Agent")
        async with Client(f"{harness.base_url}/mcp", auth=second.access_token) as second_session:
            await second_session.list_tools()
            assert isinstance(harness.store.grant(second.grant_id), IssuedGrant)
            result = await second_session.call_tool("proxy_echo", {"value": "two"})
            assert result.content[0].text == "proxy:two"
        assert harness.middleware.dispatched_tools == ["function_echo", "proxy_echo"]

        await harness.store.revoke(second.grant_id)
        assert isinstance(harness.store.grant(second.grant_id), RevokedGrant)
        refresh_exchanges_before_revoke = harness.scope_control.refresh_exchanges
        revoked_refresh = await harness.http.post(
            "/mcp/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": second.refresh_token,
                "client_id": second.client.client_id,
                "scope": _SCOPES,
            },
        )
        assert revoked_refresh.status_code == 401
        assert revoked_refresh.json()["error"] == "invalid_grant"
        assert harness.scope_control.refresh_exchanges == refresh_exchanges_before_revoke
        async with Client(f"{harness.base_url}/mcp", auth=second.access_token) as revoked_session:
            rejected = await revoked_session.call_tool("proxy_echo", {"value": "blocked"}, raise_on_error=False)
            assert rejected.is_error
            assert "agent grant is not active" in rejected.content[0].text


async def test_mounted_enrollment_boundary_rejects_cross_binding_and_duplicates() -> None:
    clock = _Clock()
    async with _running_harness(clock=clock) as harness:
        client = await harness.register(name="boundary client")
        interaction_count = len(harness.store.interactions())
        for kwargs in (
            {"redirect_uri": "https://evil.example/callback"},
            {"scopes": "openid administrator"},
            {"resource": "https://other.example/mcp"},
            {"code_challenge_method": "plain"},
        ):
            response = await harness.start(client, **kwargs)
            assert isinstance(response, httpx.Response)
            assert response.status_code in {302, 400}
        assert len(harness.store.interactions()) == interaction_count

        verifier = secrets.token_urlsafe(32)
        first, duplicate = await asyncio.gather(
            harness.start(client, verifier=verifier, state="winner"),
            harness.start(client, verifier=verifier, state="loser"),
        )
        results = (first, duplicate)
        winners = [result for result in results if isinstance(result, _AuthorizationStart)]
        losers = [result for result in results if isinstance(result, httpx.Response)]
        assert len(winners) == len(losers) == 1
        assert losers[0].status_code == 302
        assert losers[0].headers["location"].startswith(_CLIENT_CALLBACK)
        assert "temporarily_unavailable" in losers[0].headers["location"]
        assert winners[0].enrollment_url not in losers[0].headers["location"]

        other = await harness.start(client)
        assert isinstance(other, _AuthorizationStart)
        winner_page = await harness.page(winners[0])
        other_page = await harness.page(other)
        cross = await harness.approve(
            other, agent_name="Cross Bound", page=other_page, csrf=_hidden(winner_page.text, "csrf")
        )
        assert cross.status_code == 403
        wrong_origin = await harness.approve(
            winners[0], agent_name="Wrong Origin", page=winner_page, origin="https://evil.example"
        )
        assert wrong_origin.status_code == 403

        await harness.login_second_browser()
        wrong_session = await harness.approve(
            winners[0], agent_name="Wrong Browser", page=winner_page, browser=harness.second_browser
        )
        assert wrong_session.status_code == 403

        for invalid_name in ("", "line\nbreak", "direction\u202eevil", "x" * 81):
            invalid = await harness.approve(winners[0], agent_name=invalid_name, page=winner_page)
            assert invalid.status_code in {400, 422}

        first_approval = await harness.approve(winners[0], agent_name="Cafe\u0301", page=winner_page)
        assert first_approval.status_code == 303
        duplicate_name = await harness.approve(other, agent_name="CAFÉ", page=other_page)
        assert duplicate_name.status_code == 409

        deny_start = await harness.start(client)
        assert isinstance(deny_start, _AuthorizationStart)
        deny_page = await harness.page(deny_start)
        assert (await harness.deny(deny_start, deny_page)).status_code == 200
        denied = harness.store.interactions()[-1]
        assert isinstance(denied.phase, ClosedEnrollment)
        assert denied.phase.reason == ClosedReason.DENIED

        expiring = await harness.start(client)
        assert isinstance(expiring, _AuthorizationStart)
        clock.advance(11 * 60)
        expired = await harness.page(expiring)
        assert expired.status_code == 410

        # A closed tuple stays reserved past FastMCP's transaction + code lifetime.
        retried = await harness.start(client, verifier=verifier)
        assert isinstance(retried, httpx.Response)
        assert retried.status_code == 302
        assert "temporarily_unavailable" in retried.headers["location"]
        assert DEFAULT_TOMBSTONE_TTL_SECONDS > FASTMCP_TRANSACTION_TTL_SECONDS + FASTMCP_CODE_TTL_SECONDS

        # FastMCP still mounts /consent, but external mode does not route any
        # authorization there and an uncorrelated request is harmless.
        consent = await harness.http.get("/mcp/consent", params={"txn_id": "not-a-fastmcp-transaction"})
        assert consent.status_code == 404
        assert all("/mcp/consent" not in interaction.upstream_url for interaction in harness.store.interactions())

        # Reusing one S256 key across a different client and redirect is not an
        # exact-tuple collision, and neither downstream code can bind to the other.
        cross_verifier = secrets.token_urlsafe(32)
        cross_client_a = await harness.register(name="cross client a")
        cross_client_b = await harness.register(
            name="cross client b", redirect_uri="https://other-client.example/oauth/callback"
        )
        cross_a = await harness.start(cross_client_a, verifier=cross_verifier)
        cross_b = await harness.start(cross_client_b, verifier=cross_verifier)
        assert isinstance(cross_a, _AuthorizationStart)
        assert isinstance(cross_b, _AuthorizationStart)
        assert cross_a.challenge == cross_b.challenge
        assert cross_a.client.client_id != cross_b.client.client_id
        assert cross_a.client.redirect_uri != cross_b.client.redirect_uri
        cross_a_code = await harness.downstream_code(
            cross_a, await harness.approve(cross_a, agent_name="Cross Client A")
        )
        cross_b_code = await harness.downstream_code(
            cross_b, await harness.approve(cross_b, agent_name="Cross Client B")
        )
        swapped = await harness.http.post(
            "/mcp/token",
            data={
                "grant_type": "authorization_code",
                "code": cross_a_code.code,
                "redirect_uri": cross_client_b.redirect_uri,
                "client_id": cross_client_b.client_id,
                "code_verifier": cross_verifier,
            },
        )
        assert swapped.status_code == 401
        assert (await harness.token(cross_a_code)).status_code == 200
        assert (await harness.token(cross_b_code)).status_code == 200


async def test_concurrent_token_exchange_loser_cannot_delete_winners_code() -> None:
    async with _running_harness(pause_after_exchange_claim=True) as harness:
        store = harness.store
        assert isinstance(store, _PausingAgentEnrollmentStore)
        client = await harness.register(name="concurrent token exchange")
        started = await harness.start(client)
        assert isinstance(started, _AuthorizationStart)
        downstream = await harness.downstream_code(
            started, await harness.approve(started, agent_name="One Exchange Winner")
        )

        winner_task = asyncio.create_task(harness.token(downstream))
        try:
            await asyncio.wait_for(asyncio.to_thread(store.exchange_claimed.wait), timeout=5)
            loser = await harness.token(downstream)
        finally:
            store.continue_exchange.set()
        winner = await winner_task

        assert loser.status_code == 401
        assert loser.json()["error"] == "invalid_grant"
        assert winner.status_code == 200, winner.text
        assert len(store.agents()) == 1
        assert len(store.grants()) == 1
        assert isinstance(store.grants()[0], IssuedGrant)


async def test_identity_scope_and_issue_faults_fail_closed_without_private_callback_hooks() -> None:
    async with _running_harness() as harness:
        matcher = CanonicalOperatorMatcher(anchors=harness.matcher_anchors)
        same_raw_subject_elsewhere = OperatorIdentity(
            issuer="https://untrusted.example/", subject=_SUBJECT, username="display-only", session_id="session"
        )
        principal = VerifiedOidcPrincipal(issuer=harness.mcp_issuer, subject=_SUBJECT)
        assert matcher.same_operator(same_raw_subject_elsewhere, principal) is None
        renamed = OperatorIdentity(
            issuer=harness.browser_issuer,
            subject=_SUBJECT,
            username="a mutable new username",
            session_id="other-session",
        )
        assert matcher.same_operator(renamed, principal) == "operator-anchor-42"

        mismatch_client = await harness.register(name="identity mismatch")
        mismatch_start = await harness.start(mismatch_client)
        assert isinstance(mismatch_start, _AuthorizationStart)
        mismatch_approval = await harness.approve(mismatch_start, agent_name="Must Not Exist")
        mismatch_code = await harness.downstream_code(mismatch_start, mismatch_approval)
        harness.matcher_anchors[IssuerSubject(harness.mcp_issuer, _SUBJECT)] = "different-operator-anchor"
        mismatch = await harness.token(mismatch_code)
        assert mismatch.status_code == 401
        assert mismatch.json()["error"] == "invalid_grant"
        assert harness.store.agents() == ()
        harness.matcher_anchors[IssuerSubject(harness.mcp_issuer, _SUBJECT)] = "operator-anchor-42"

        narrowed_client = await harness.register(name="scope narrowing")
        narrowed_start = await harness.start(narrowed_client)
        assert isinstance(narrowed_start, _AuthorizationStart)
        narrowed_approval = await harness.approve(narrowed_start, agent_name="Narrowed Agent")
        harness.scope_control.next_scope = "openid"
        narrowed_code = await harness.downstream_code(narrowed_start, narrowed_approval)
        narrowed = await harness.token(narrowed_code)
        assert narrowed.status_code == 200, narrowed.text
        narrowed_claims = harness.proxy.jwt_issuer.verify_token(
            narrowed.json()["access_token"], expected_token_use="access"
        )
        narrowed_grant = harness.store.grant(narrowed_claims["upstream_claims"]["grant_id"])
        assert isinstance(narrowed_grant, IssuedGrant)
        assert narrowed_grant.core.allowed_scopes == frozenset({"openid"})

        agents_before_broadening = {agent.agent_id for agent in harness.store.agents()}
        broaden_client = await harness.register(name="scope broadening")
        broaden_start = await harness.start(broaden_client)
        assert isinstance(broaden_start, _AuthorizationStart)
        broaden_approval = await harness.approve(broaden_start, agent_name="Scope Must Not Exist")
        harness.scope_control.next_scope = "openid offline_access profile"
        broaden_code = await harness.downstream_code(broaden_start, broaden_approval)
        broadened = await harness.token(broaden_code)
        assert broadened.status_code == 401
        assert broadened.json()["error"] == "invalid_grant"
        assert {agent.agent_id for agent in harness.store.agents()} == agents_before_broadening

        grants_before_fault = {grant.core.grant_id for grant in harness.store.grants()}
        fault_client = await harness.register(name="issue transition fault")
        fault_start = await harness.start(fault_client)
        assert isinstance(fault_start, _AuthorizationStart)
        fault_approval = await harness.approve(fault_start, agent_name="Recoverable Agent")
        fault_code = await harness.downstream_code(fault_start, fault_approval)
        harness.store.fail_next_issue_completion = True
        failed = await harness.token(fault_code)
        assert failed.status_code == 500
        new_grants = [grant for grant in harness.store.grants() if grant.core.grant_id not in grants_before_fault]
        assert len(new_grants) == 1
        issuing = new_grants[0]
        assert isinstance(issuing, IssuingGrant)
        assert issuing.evidence is not None
        await harness.store.reconcile_issuing(issuing.core.grant_id)
        assert isinstance(harness.store.grant(issuing.core.grant_id), IssuedGrant)

        # A successfully persisted family remains issued if the client loses the response.
        lost_response = await harness.issue(client_name="lost response", agent_name="Lost Response Agent")
        assert isinstance(harness.store.grant(lost_response.grant_id), IssuedGrant)

        assert_fastmcp_enrollment_compatibility()
        source = inspect.getsource(AgentEnrollmentOIDCProxy)
        assert source.count("_code_store") == 2
        assert "_transaction_store" not in source
        assert "_handle_idp_callback" not in source
        assert "load_authorization_code" not in source
        assert "get_routes" not in source
        assert "state=" not in source
        assert list(inspect.signature(OAuthProxy.authorize).parameters) == ["self", "client", "params"]
        assert list(inspect.signature(OAuthProxy._extract_upstream_claims).parameters) == ["self", "idp_tokens"]
        assert list(inspect.signature(OAuthProxy._translate_scopes_from_idp).parameters) == ["self", "scopes"]


if __name__ == "__main__":
    pytest_bazel.main()
