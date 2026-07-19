"""End-to-end auth boundary for the console's `/api/*` split, exercised through the real
``create_app`` — real routers, real router-level guards, real SessionMiddleware, and a real Authentik
authorization-code login against a hermetic mock OIDC provider (``util.testing.mock_oidc``). No stub
app, no hand-minted session: the operator credential is obtained by actually walking
``/auth/login`` → provider → ``/auth/callback``.

The browser API requires an operator session. Static Agent bearers authenticate only to `/mcp`.
OIDC configuration is mandatory; there is no unauthenticated development mode.
"""

from __future__ import annotations

import time
from pathlib import Path
from uuid import UUID

import httpx
import pytest
import pytest_bazel
import yaml
from pydantic import SecretStr, ValidationError
from sqlalchemy import create_engine, func, select

from haku.console import operator_auth
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
                    }
                ]
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


def test_guards_reject_anonymous_requests_with_test_oidc(make_client) -> None:
    with make_client() as client:
        assert client.get("/api/tool-calls").status_code == 401
        assert client.get("/api/config").status_code == 401


def test_signed_operator_session_has_an_absolute_reauthentication_deadline(make_operator_client) -> None:
    with make_operator_client(operator_session_expires_at=int(time.time()) - 1) as client:
        assert client.get("/auth/me").status_code == 401
        assert client.get("/api/config").status_code == 401


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


def test_state_mismatch_renders_a_retryable_html_error(make_client) -> None:
    with make_client() as client:
        client.app.state.operator_oauth = _MismatchingStateOAuth()
        response = client.get("/auth/callback")

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["Cache-Control"] == "no-store"
    assert "expired or was superseded" in response.text
    assert '<a href="/auth/login">Retry login</a>' in response.text


def test_callback_returns_to_exact_local_agent_enrollment_interaction(make_operator_client) -> None:
    interaction_id = UUID("d9377996-7f17-4dcb-a746-3f401e0b1413")
    return_to = f"/auth/agent-enrollment/{interaction_id}?browser_nonce=opaque-value"
    with make_operator_client(operator_return_to=return_to) as client:
        client.app.state.operator_oauth = _MatchingIssuerOAuth()
        response = client.get("/auth/callback", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == return_to


@pytest.mark.parametrize(
    "return_to",
    [
        "https://attacker.example/",
        "//attacker.example/auth/agent-enrollment/d9377996-7f17-4dcb-a746-3f401e0b1413",
        "/auth/agent-enrollment/not-a-uuid",
        "/api/config",
        "/auth/agent-enrollment/d9377996-7f17-4dcb-a746-3f401e0b1413#fragment",
    ],
)
def test_login_rejects_non_enrollment_continuations(make_client, return_to: str) -> None:
    with make_client() as client:
        response = client.get("/auth/login", params={"return_to": return_to})

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid operator login continuation"


@pytest.mark.parametrize("oauth", [_MismatchedIssuerOAuth(), _MissingIssuerOAuth()])
def test_callback_rejects_wrong_or_missing_verified_issuer_claim(
    oauth: object, make_client, migrated_db_url: str
) -> None:
    with make_client() as client:
        client.app.state.operator_oauth = oauth
        response = client.get("/auth/callback")
        me = client.get("/auth/me")

    assert response.status_code == 401
    assert response.json()["detail"] == "OIDC token issuer does not match configured issuer"
    assert me.status_code == 401
    engine = create_engine(migrated_db_url)
    try:
        with engine.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(OidcIdentity)) == 0
    finally:
        engine.dispose()


@pytest.mark.parametrize("path", ["/api/tool-calls", "/api/config", "/api/capabilities/csrf"])
def test_retired_authentik_headers_cannot_authenticate_an_operator(make_client, path: str) -> None:
    """The app-owned OIDC session is the only operator identity boundary.

    These headers were trustworthy only behind the retired Authentik forward-auth
    outpost. Public requests reach Haku directly now, so accepting either value
    would let an anonymous caller enter operator-only routes and mint the matching
    double-submit CSRF token.
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
