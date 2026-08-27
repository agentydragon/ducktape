"""End-to-end auth boundary for the console's `/api/*` split, exercised through the real
``create_app`` — real routers, real router-level guards, real SessionMiddleware, and a real Authentik
authorization-code login against a hermetic mock OIDC provider (``util.testing.mock_oidc``). No stub
app, no hand-minted session: the operator credential is obtained by actually walking
``/auth/login`` → provider → ``/auth/callback``.

The browser API requires an operator session. Static Agent bearers authenticate only to `/mcp`.
OIDC configuration is mandatory; there is no unauthenticated development mode.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
from pathlib import Path

import httpx
import pytest
import pytest_bazel
import yaml
from fastapi.routing import APIRoute, RouteContext, iter_route_contexts
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select

from haku.console import operator_auth, operator_login_flow
from haku.console.app import create_app
from haku.console.config import OperatorOidcConfig
from haku.console.conftest import TEST_OPERATOR_OIDC, console_settings
from haku.console.database_schema import OidcIdentity
from util.net import pick_free_port
from util.testing.asgi import serve_app
from util.testing.mock_oidc import build_mock_oidc_app, generate_rsa_keypair

AGENT_TOKEN = "agent-secret"  # a test literal, not a real credential
_AGENT_TOKEN_ENV = "HAKU_CONSOLE_OPERATOR_AUTH_TEST_TOKEN"
_AGENT_OPERATOR_ENV = "HAKU_CONSOLE_OPERATOR_AUTH_TEST_OPERATOR"
_OPERATOR_SUBJECT = "op-subject-1"  # the opaque Authentik `sub` the mock IdP issues
_OPERATOR_USERNAME = "agentydragon"
_UNSAFE_HTTP_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _dependency_calls(route: RouteContext) -> set[object]:
    pending = list(route.dependant.dependencies)
    calls: set[object] = set()
    while pending:
        dependency = pending.pop()
        if dependency.call is not None:
            calls.add(dependency.call)
        pending.extend(dependency.dependencies)
    return calls


class _MismatchedIssuerClient:
    async def authorize_access_token(self, _request: object) -> dict[str, object]:
        return {
            "userinfo": {
                "iss": "https://different-issuer.test/",
                "sub": _OPERATOR_SUBJECT,
                "preferred_username": _OPERATOR_USERNAME,
            }
        }


class _MismatchedIssuerOAuth:
    def create_client(self, _name: str) -> _MismatchedIssuerClient:
        return _MismatchedIssuerClient()


class _MissingIssuerClient:
    async def authorize_access_token(self, _request: object) -> dict[str, object]:
        return {"userinfo": {"sub": _OPERATOR_SUBJECT, "preferred_username": _OPERATOR_USERNAME}}


class _MissingIssuerOAuth:
    def create_client(self, _name: str) -> _MissingIssuerClient:
        return _MissingIssuerClient()


class _MatchingIssuerClient:
    async def authorize_access_token(self, _request: object) -> dict[str, object]:
        return {
            "userinfo": {
                "iss": TEST_OPERATOR_OIDC.issuer,
                "sub": _OPERATOR_SUBJECT,
                "preferred_username": _OPERATOR_USERNAME,
            }
        }


class _MatchingIssuerOAuth:
    def create_client(self, _name: str) -> _MatchingIssuerClient:
        return _MatchingIssuerClient()


class _MismatchingStateClient:
    async def authorize_access_token(self, _request: object) -> dict[str, object]:
        raise operator_auth.OAuthError(error="mismatching_state")


class _MismatchingStateOAuth:
    def create_client(self, _name: str) -> _MismatchingStateClient:
        return _MismatchingStateClient()


def _static_agent_config(tmp_path: Path) -> Path:
    config = tmp_path / "console.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "static_agents": [
                    {
                        "agent_id": "10000000-0000-4000-8000-000000000001",
                        "display_name": "Haku",
                        "token_env_var": _AGENT_TOKEN_ENV,
                        "operator_subject_env": _AGENT_OPERATOR_ENV,
                        "access_profile_id": "no_auto_approval",
                    }
                ],
                "auto_approval_policies": [{"id": "no_auto_approval", "type": "never"}],
                "access_profiles": [{"id": "no_auto_approval", "auto_approval_policy": "no_auto_approval"}],
                "default_access_profile_id": "no_auto_approval",
            }
        ),
        encoding="utf-8",
    )
    return config


async def test_credential_matrix_through_real_app(
    migrated_db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The browser API requires an Operator session; a static Agent bearer is MCP-only."""
    monkeypatch.setenv(_AGENT_TOKEN_ENV, AGENT_TOKEN)
    monkeypatch.setenv(_AGENT_OPERATOR_ENV, _OPERATOR_SUBJECT)

    private_key, public_key = generate_rsa_keypair()
    idp_port, console_port = pick_free_port(), pick_free_port()
    idp_url, console_url = f"http://127.0.0.1:{idp_port}", f"http://127.0.0.1:{console_port}"
    idp = build_mock_oidc_app(
        issuer_url=idp_url,
        private_key=private_key,
        public_key=public_key,
        subject=_OPERATOR_SUBJECT,
        extra_id_token_claims={"preferred_username": _OPERATOR_USERNAME},
    )
    settings = console_settings(
        migrated_db_url,
        haku_ui_url="about:blank",
        config_file=_static_agent_config(tmp_path),
        public_base_url=console_url,
        operator_oidc=OperatorOidcConfig(
            issuer=idp_url, client_id="console", client_secret=SecretStr("secret"), session_secret=SecretStr("sess")
        ),
    )
    app = create_app(settings)

    async with serve_app(idp, port=idp_port), serve_app(app, port=console_port):
        # No credential: every `/api/*` route rejects; the health probe stays open.
        async with httpx.AsyncClient(base_url=console_url) as anon:
            assert (await anon.get("/api/tool-calls")).status_code == 401
            assert (await anon.get("/api/config")).status_code == 401
            assert (await anon.get("/healthz")).status_code == 200

        # A static Agent bearer grants no browser API access. The retired submission route is absent.
        async with httpx.AsyncClient(base_url=console_url, headers={"Authorization": f"Bearer {AGENT_TOKEN}"}) as agent:
            assert (await agent.get("/api/tool-calls")).status_code == 401
            assert (await agent.post("/api/tool-calls", json={})).status_code == 405
            assert (await agent.get("/api/config")).status_code == 401

        # A real operator login (authorization-code flow through the mock IdP) reaches everything.
        async with httpx.AsyncClient(base_url=console_url, follow_redirects=True) as operator:
            await operator.get("/auth/login")  # → IdP authorize → /auth/callback → operator session cookie
            me = await operator.get("/auth/me")
            assert me.status_code == 200, me.text
            assert me.json()["username"] == _OPERATOR_USERNAME
            assert (await operator.get("/api/tool-calls")).status_code == 200
            assert (await operator.post("/api/tool-calls", json={})).status_code == 405
            assert (await operator.get("/api/config")).status_code == 200


async def test_two_console_tabs_can_log_in_at_once(
    migrated_db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every open tab loses its session at the same moment (one absolute deadline), so they bounce
    to /auth/login together. Neither attempt may strand the other: with authlib's session-backed
    state each new authorization request evicts the last, and the loser's callback dies on
    "expired or was superseded"."""
    monkeypatch.setenv(_AGENT_TOKEN_ENV, AGENT_TOKEN)
    monkeypatch.setenv(_AGENT_OPERATOR_ENV, _OPERATOR_SUBJECT)

    private_key, public_key = generate_rsa_keypair()
    idp_port, console_port = pick_free_port(), pick_free_port()
    idp_url, console_url = f"http://127.0.0.1:{idp_port}", f"http://127.0.0.1:{console_port}"
    idp = build_mock_oidc_app(
        issuer_url=idp_url,
        private_key=private_key,
        public_key=public_key,
        subject=_OPERATOR_SUBJECT,
        extra_id_token_claims={"preferred_username": _OPERATOR_USERNAME},
    )
    settings = console_settings(
        migrated_db_url,
        haku_ui_url="about:blank",
        config_file=_static_agent_config(tmp_path),
        public_base_url=console_url,
        operator_oidc=OperatorOidcConfig(
            issuer=idp_url, client_id="console", client_secret=SecretStr("secret"), session_secret=SecretStr("sess")
        ),
    )
    app = create_app(settings)

    async with (
        serve_app(idp, port=idp_port),
        serve_app(app, port=console_port),
        # One client is one browser: both tabs share a cookie jar, and both requests are built
        # from the same snapshot of it.
        httpx.AsyncClient(base_url=console_url) as browser,
    ):
        settings_tab, history_tab = await asyncio.gather(
            browser.get("/auth/login", params={"return_to": "/_console/settings"}),
            browser.get("/auth/login", params={"return_to": "/_console/tool-calls"}),
        )

        for tab, expected_return_to in ((settings_tab, "/_console/settings"), (history_tab, "/_console/tool-calls")):
            landed = await browser.get(tab.headers["location"], follow_redirects=False)
            finished = await browser.get(landed.headers["location"], follow_redirects=False)
            assert finished.status_code == 303, finished.text
            assert finished.headers["location"] == expected_return_to
        assert (await browser.get("/auth/me")).status_code == 200


def test_guards_reject_anonymous_requests_with_test_oidc(make_client) -> None:
    with make_client() as client:
        assert client.get("/api/tool-calls").status_code == 401
        assert client.get("/api/config").status_code == 401


def test_every_unsafe_api_route_has_an_explicit_admission_boundary(make_client) -> None:
    """Adding an unsafe browser route cannot silently omit exact-Origin admission."""
    with make_client() as client:
        unsafe_routes: list[RouteContext] = [
            route
            for route in iter_route_contexts(client.app.routes)
            if isinstance(route.original_route, APIRoute) and _UNSAFE_HTTP_METHODS.intersection(route.methods or ())
        ]

    assert unsafe_routes
    for route in unsafe_routes:
        path = route.path
        assert path is not None
        calls = _dependency_calls(route)
        if path.startswith("/api/node-daemons/v1/"):
            assert operator_auth.require_operator not in calls, route.path
            assert operator_auth.require_operator_mutation_origin not in calls, route.path
        elif path in {"/api/internal/kubernetes/authorize", "/api/internal/http/decide"}:
            # These deny-only, bearer-authenticated machine routes are covered by
            # test_kube_proxy_authorization and test_http_decide_routes; browser session /
            # Origin guards do not apply.
            assert operator_auth.require_operator not in calls, route.path
            assert operator_auth.require_operator_mutation_origin not in calls, route.path
        elif path == "/auth/logout":
            assert operator_auth.require_operator not in calls, route.path
            assert operator_auth.require_operator_mutation_origin in calls, route.path
        else:
            assert operator_auth.require_operator in calls, route.path
            assert operator_auth.require_operator_mutation_origin in calls, route.path


def test_signed_operator_session_has_an_absolute_reauthentication_deadline(make_operator_client) -> None:
    with make_operator_client(operator_session_expires_at=int(time.time()) - 1) as client:
        assert client.get("/auth/me").status_code == 401
        assert client.get("/api/config").status_code == 401


def test_session_rejections_are_logged_with_distinguishing_reasons(
    make_client, make_operator_client, caplog: pytest.LogCaptureFixture
) -> None:
    """A blown absolute deadline and a cookie the browser never sent are both a bare 401 to the
    caller. The server log is the only place they can be told apart, which is what an operator
    reporting a failed account reconnect needs."""
    with caplog.at_level(logging.INFO, logger="haku.console.operator_auth"):
        with make_client() as client:
            assert client.get("/api/config").status_code == 401
        anonymous = [record.getMessage() for record in caplog.records]

        caplog.clear()
        with make_operator_client(operator_session_expires_at=int(time.time()) - 90) as client:
            assert client.get("/api/config").status_code == 401
        expired = [record.getMessage() for record in caplog.records]

    assert any("reason=no_session_cookie" in message for message in anonymous)
    assert not any("reason=expired" in message for message in anonymous)
    assert any("reason=expired" in message and "path=/api/config" in message for message in expired)
    # The elapsed time since the deadline is what identifies the absolute-deadline case on sight.
    elapsed_text = next(
        message.partition("expired_for=")[2].split()[0] for message in expired if "expired_for=" in message
    )
    hours, minutes, seconds = (int(part) for part in elapsed_text.split(":"))
    elapsed = datetime.timedelta(hours=hours, minutes=minutes, seconds=seconds)
    # Constructing the client can cross a wall-clock second (and can take longer on a loaded CI
    # worker), so assert the diagnostic's meaningful range rather than scheduler-perfect timing.
    assert datetime.timedelta(seconds=90) <= elapsed < datetime.timedelta(minutes=2)


def test_logout_is_an_exact_origin_post_that_clears_the_session(make_operator_client) -> None:
    with make_operator_client() as client:
        assert client.get("/auth/logout", follow_redirects=False).status_code == 405
        rejected = client.post("/auth/logout", headers={"Origin": "https://attacker.test"}, follow_redirects=False)
        assert rejected.status_code == 403
        assert client.get("/auth/me").status_code == 200

        logged_out = client.post("/auth/logout", follow_redirects=False)
        assert logged_out.status_code == 303
        assert logged_out.headers["location"] == "/"
        deletion_cookie = logged_out.headers["set-cookie"]
        assert "session=null" in deletion_cookie
        assert "expires=Thu, 01 Jan 1970 00:00:00 GMT" in deletion_cookie


def test_continuous_use_cannot_slide_operator_session_past_absolute_deadline(
    make_operator_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    deadline = int(time.time()) + 300
    with make_operator_client(operator_session_expires_at=deadline) as client:
        before_expiry = client.get("/auth/me")
        assert before_expiry.status_code == 200

        # Advance less than the middleware max_age but past the independently signed payload
        # deadline: repeated successful use must not extend the absolute authorization lifetime.
        monkeypatch.setattr(operator_auth.time, "time", lambda: deadline + 1)
        assert client.get("/auth/me").status_code == 401


def test_successful_login_cookie_has_one_hour_max_age(make_client) -> None:
    with make_client() as client:
        client.app.state.operator_oauth = _MatchingIssuerOAuth()
        response = client.get("/auth/callback", follow_redirects=False)

    assert response.status_code == 303
    assert "Max-Age=3600" in response.headers["set-cookie"]


def _seed_login_flow(client, *, return_to: str | None, binding: str = "test-browser-binding") -> str:
    """A pending flow with its binding cookie — the stubbed OAuth clients never create one."""
    state = "seeded-login-state"
    client.portal.call(
        lambda: client.app.state.operator_login_flows.start(
            state=state,
            browser_binding=binding,
            return_to=return_to,
            data={"redirect_uri": "https://haku.test/auth/callback"},
        )
    )
    client.cookies.set(operator_login_flow.binding_cookie_name(state), binding)
    return state


@pytest.mark.parametrize(
    "return_to",
    [
        "/auth/agent-enrollment/d9377996-7f17-4dcb-a746-3f401e0b1413?browser_nonce=opaque-value",
        "/_console/tool-calls",
        "/_console/settings",
        "/threads/42?reply=1",
        "/",
    ],
)
def test_callback_returns_to_the_page_the_login_started_from(make_operator_client, return_to: str) -> None:
    with make_operator_client() as client:
        state = _seed_login_flow(client, return_to=return_to)
        client.app.state.operator_oauth = _MatchingIssuerOAuth()
        response = client.get("/auth/callback", params={"state": state}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == return_to


async def test_callback_without_a_continuation_returns_to_the_root(make_operator_client) -> None:
    with make_operator_client() as client:
        state = _seed_login_flow(client, return_to=None)
        client.app.state.operator_oauth = _MatchingIssuerOAuth()
        response = client.get("/auth/callback", params={"state": state}, follow_redirects=False)

    assert response.headers["location"] == "/"


@pytest.mark.parametrize(
    "return_to",
    [
        "https://attacker.example/",
        "//attacker.example/auth/agent-enrollment/d9377996-7f17-4dcb-a746-3f401e0b1413",
        "/auth/agent-enrollment/not-a-uuid",
        "/auth/login",
        "/api/config",
        "/mcp",
        "/healthz",
        "/.well-known/oauth-authorization-server",
        # A browser normalizes the backslash to a slash, making this protocol-relative.
        "/\\attacker.example/",
        "/auth/agent-enrollment/d9377996-7f17-4dcb-a746-3f401e0b1413#fragment",
    ],
)
async def test_login_rejects_continuations_that_are_not_console_pages(make_client, return_to: str) -> None:
    with make_client() as client:
        response = client.get("/auth/login", params={"return_to": return_to})

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid operator login continuation"


async def test_a_login_started_by_another_browser_is_refused(make_operator_client) -> None:
    """RFC 6749 §10.12: possessing a `state` is not enough — the browser must have started it.

    Restarting cannot fix this outcome, so it explains rather than bouncing."""
    with make_operator_client() as client:
        state = _seed_login_flow(client, return_to="/_console/settings", binding="the-secret-that-started-it")
        client.cookies.set(operator_login_flow.binding_cookie_name(state), "some-other-browsers-guess")
        client.app.state.operator_oauth = _MatchingIssuerOAuth()
        response = client.get("/auth/callback", params={"state": state}, follow_redirects=False)

        assert response.status_code == 401
        assert "started in a different browser" in response.text
        # The flow is spent either way, so it cannot be replayed.
        assert client.portal.call(lambda: client.app.state.operator_login_flows.pending_login(state)) is None


async def test_a_stale_attempt_restarts_the_login_once_then_explains(make_client) -> None:
    """A superseded or expired attempt is ordinary — every tab bounces to login at the same time —
    so the first one restarts itself instead of dead-ending on a page the operator must click."""
    with make_client() as client:
        client.app.state.operator_oauth = _MismatchingStateOAuth()

        restarted = client.get("/auth/callback", follow_redirects=False)
        assert restarted.status_code == 303
        assert restarted.headers["location"] == "/auth/login"

        # The marker cookie the restart set is what stops the second failure from looping.
        gave_up = client.get("/auth/callback", follow_redirects=False)
        assert gave_up.status_code == 401
        assert gave_up.headers["content-type"].startswith("text/html")
        assert gave_up.headers["Cache-Control"] == "no-store"
        assert "expired or was superseded" in gave_up.text
        assert '<a href="/auth/login">Retry login</a>' in gave_up.text


async def test_me_reports_the_absolute_reauthentication_deadline(make_operator_client) -> None:
    deadline = int(time.time()) + 900
    with make_operator_client(operator_session_expires_at=deadline) as client:
        me = client.get("/auth/me")

    assert me.status_code == 200
    assert datetime.datetime.fromisoformat(me.json()["expires_at"]) == datetime.datetime.fromtimestamp(
        deadline, tz=datetime.UTC
    )


@pytest.mark.parametrize("oauth", [_MismatchedIssuerOAuth(), _MissingIssuerOAuth()])
async def test_callback_rejects_wrong_or_missing_verified_issuer_claim(
    oauth: object, make_client, migrated_engine
) -> None:
    with make_client() as client:
        client.app.state.operator_oauth = oauth
        response = client.get("/auth/callback")
        me = client.get("/auth/me")

    assert response.status_code == 401
    assert response.json()["detail"] == "OIDC token issuer does not match configured issuer"
    assert me.status_code == 401
    async with migrated_engine.connect() as connection:
        assert (await connection.scalar(select(func.count()).select_from(OidcIdentity))) == 0


@pytest.mark.parametrize("path", ["/api/tool-calls", "/api/config"])
def test_retired_authentik_headers_cannot_authenticate_an_operator(make_client, path: str) -> None:
    """The app-owned OIDC session is the only operator identity boundary.

    These headers were trustworthy only behind the retired Authentik forward-auth
    outpost. Public requests reach Haku directly now, so accepting either value
    would let an anonymous caller enter operator-only routes.
    """
    with make_client() as client:
        response = client.get(
            path, headers={"X-Authentik-Uid": "forged-operator-subject", "X-Authentik-Username": "forged-operator"}
        )

    assert response.status_code == 401


def test_operator_oidc_is_required(migrated_db_url: str) -> None:
    with pytest.raises(ValidationError, match="operator_oidc"):
        console_settings(migrated_db_url, operator_oidc=None)


def test_operator_oidc_requires_canonical_public_origin(migrated_db_url: str) -> None:
    oidc = OperatorOidcConfig(
        issuer="https://auth.test/application/o/haku-console/",
        client_id="console",
        client_secret=SecretStr("secret"),
        session_secret=SecretStr("session"),
    )
    with pytest.raises(ValidationError, match="public_base_url"):
        console_settings(migrated_db_url, operator_oidc=oidc, public_base_url=None)
    with pytest.raises(ValidationError, match="canonical http"):
        console_settings(migrated_db_url, operator_oidc=oidc, public_base_url="https://haku.test/a/path")


if __name__ == "__main__":
    pytest_bazel.main()
