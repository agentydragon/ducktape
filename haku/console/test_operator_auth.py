"""End-to-end auth boundary for the console's `/api/*` split, exercised through the real
``create_app`` — real routers, real router-level guards, real SessionMiddleware, and a real Authentik
authorization-code login against a hermetic mock OIDC provider (``util.testing.mock_oidc``). No stub
app, no hand-minted session: the operator credential is obtained by actually walking
``/auth/login`` → provider → ``/auth/callback``.

Two guards enforce the split (`operator_auth`): the agent-facing tool-call routes accept an operator
session OR a static agent's bearer; the operator-only surfaces (approvals, account linking) require an
operator session. When ``operator_oidc`` is unset (dev/test) both guards no-op.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import pytest_bazel
import yaml
from fastapi.testclient import TestClient
from pydantic import SecretStr

from haku.console.app import create_app
from haku.console.config import OperatorOidcConfig
from haku.console.conftest import console_settings
from util.net import pick_free_port
from util.testing.asgi import serve_app
from util.testing.mock_oidc import build_mock_oidc_app, generate_rsa_keypair

AGENT_TOKEN = "agent-secret"  # a test literal, not a real credential
_AGENT_TOKEN_ENV = "HAKU_CONSOLE_OPERATOR_AUTH_TEST_TOKEN"
_AGENT_OPERATOR_ENV = "HAKU_CONSOLE_OPERATOR_AUTH_TEST_OPERATOR"
_OPERATOR_SUBJECT = "op-subject-1"  # the opaque Authentik `sub` the mock IdP issues
_OPERATOR_USERNAME = "agentydragon"


def _static_agent_config(tmp_path: Path) -> Path:
    config = tmp_path / "console.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "static_agents": [
                    {"agent": "haku", "token_env_var": _AGENT_TOKEN_ENV, "operator_subject_env": _AGENT_OPERATOR_ENV}
                ]
            }
        ),
        encoding="utf-8",
    )
    return config


async def test_credential_matrix_through_real_app(
    migrated_db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No credential is rejected on `/api/*`; a static agent's bearer reaches only the agent-facing
    routes; a real operator login reaches everything — all through the real routers and guards."""
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
            assert (await anon.get("/api/tool-calls")).status_code == 401  # agent-facing read
            assert (await anon.get("/api/mcp/operator-auth")).status_code == 401  # operator-only
            assert (await anon.get("/healthz")).status_code == 200

        # A static agent's bearer reaches the agent-facing tool-call routes only.
        async with httpx.AsyncClient(base_url=console_url, headers={"Authorization": f"Bearer {AGENT_TOKEN}"}) as agent:
            assert (await agent.get("/api/tool-calls")).status_code == 200
            assert (await agent.get("/api/mcp/operator-auth")).status_code == 401  # operator-only, forbidden

        # A real operator login (authorization-code flow through the mock IdP) reaches everything.
        async with httpx.AsyncClient(base_url=console_url, follow_redirects=True) as operator:
            await operator.get("/auth/login")  # → IdP authorize → /auth/callback → operator session cookie
            me = await operator.get("/auth/me")
            assert me.status_code == 200, me.text
            assert me.json()["username"] == _OPERATOR_USERNAME
            assert (await operator.get("/api/tool-calls")).status_code == 200  # agent-facing
            assert (await operator.get("/api/mcp/operator-auth")).status_code == 200  # operator-only


def test_guards_noop_without_operator_oidc(client: TestClient) -> None:
    # operator_oidc unset (dev/test / outpost mode): the in-app guards do not run, so `/api/*` is open
    # without a credential — both the agent-facing and operator-only surfaces.
    assert client.get("/api/tool-calls").status_code == 200  # agent-facing
    assert client.get("/api/config").status_code == 200  # operator-only


if __name__ == "__main__":
    pytest_bazel.main()
