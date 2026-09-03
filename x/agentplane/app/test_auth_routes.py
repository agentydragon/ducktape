"""The login boundary, walked for real: /auth/login to a hermetic provider and back to /auth/callback.

No hand-minted cookie and no stub app -- the session under test is the one an actual
authorization-code round trip produced through `create_app`, `SessionMiddleware` and the real
routers, so what these assert is the boundary a browser meets.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_bazel
from starlette.middleware.sessions import SessionMiddleware

from util.net import pick_free_port
from util.testing.asgi import serve_app
from util.testing.mock_oidc import build_mock_oidc_app, generate_rsa_keypair
from x.agentplane.app import auth_routes
from x.agentplane.app.api import Provider, create_app
from x.agentplane.app.bridge import RunnerBridge
from x.agentplane.app.decisions import DecisionsClient
from x.agentplane.app.egress import EgressInventory
from x.agentplane.app.inventory import SandboxInventory
from x.agentplane.app.oidc import OIDCSettings, build_oauth
from x.agentplane.app.trajectory import TrajectoryStore

# SessionMiddleware signs cookies with itsdangerous, imported inside starlette;
# gazelle cannot see the dependency.
# gazelle:include_dep @pypi//itsdangerous

OPERATOR = "agentydragon"
SUBJECT = "op-subject-1"
SESSION_SECRET = "test-session-secret"  # a test literal, not a real credential
MODELS = {Provider.CLAUDE: ["test-claude-model"], Provider.CODEX: ["test-codex-model"]}


@pytest.fixture
async def logged_in(
    inventory: SandboxInventory,
    bridge: RunnerBridge,
    store: TrajectoryStore,
    egress: EgressInventory,
    decisions: DecisionsClient,
) -> AsyncIterator[tuple[httpx.AsyncClient, str]]:
    """The app with a login, an IdP behind it, and a browser that has completed the round trip."""
    private_key, public_key = generate_rsa_keypair()
    idp_port, app_port = pick_free_port(), pick_free_port()
    idp_url, app_url = f"http://127.0.0.1:{idp_port}", f"http://127.0.0.1:{app_port}"
    idp = build_mock_oidc_app(
        issuer_url=idp_url,
        private_key=private_key,
        public_key=public_key,
        subject=SUBJECT,
        extra_id_token_claims={"preferred_username": OPERATOR},
    )
    oidc = OIDCSettings(
        issuer=idp_url,
        client_id="agentplane",
        client_secret="agentplane-secret",  # a test literal, not a real credential
        session_secret=SESSION_SECRET,
        public_base_url=app_url,
    )
    app = create_app(inventory, bridge, store, MODELS, egress, decisions, oidc)
    app.add_middleware(
        SessionMiddleware,
        secret_key=oidc.session_secret,
        session_cookie=oidc.cookie_name,
        https_only=oidc.secure,
        same_site="lax",
        max_age=oidc.session_seconds,
    )
    app.state.oauth = build_oauth(oidc)
    app.include_router(auth_routes.router)
    async with (
        serve_app(idp, port=idp_port),
        serve_app(app, port=app_port),
        httpx.AsyncClient(base_url=app_url, follow_redirects=True) as browser,
    ):
        yield browser, app_url


async def test_a_request_without_a_session_is_refused_and_the_round_trip_grants_one(
    logged_in: tuple[httpx.AsyncClient, str],
) -> None:
    browser, _ = logged_in

    assert (await browser.get("/sandboxes")).status_code == 401
    assert (await browser.get("/auth/me")).status_code == 401

    landed = await browser.get("/auth/login")

    # Login ends at the app root, which only the SPA mount serves; this test builds the API alone,
    # so what it can assert is where the browser was sent, not what answered there.
    assert landed.url.path == "/"
    assert (await browser.get("/auth/me")).json() == {"username": OPERATOR}
    assert (await browser.get("/sandboxes")).status_code == 200


async def test_logout_drops_the_session(logged_in: tuple[httpx.AsyncClient, str]) -> None:
    browser, app_url = logged_in
    await browser.get("/auth/login")

    assert (await browser.post("/auth/logout", headers={"Origin": app_url})).url.path == "/"
    assert (await browser.get("/auth/me")).status_code == 401
    assert (await browser.get("/sandboxes")).status_code == 401


async def test_an_unsafe_method_from_another_origin_is_refused(logged_in: tuple[httpx.AsyncClient, str]) -> None:
    """SameSite=lax still lets a cross-site form post carry the cookie; the Origin check is what does not."""
    browser, app_url = logged_in
    await browser.get("/auth/login")
    body = {"slug": "demo"}

    refused = await browser.post("/sandboxes", json=body, headers={"Origin": "https://evil.test"})

    assert refused.status_code == 403
    assert "cross-origin" in refused.json()["detail"]
    assert (await browser.post("/sandboxes", json=body, headers={"Origin": app_url})).status_code == 201


async def test_the_unguarded_app_the_api_server_proxies_to_has_no_login(
    inventory: SandboxInventory,
    bridge: RunnerBridge,
    store: TrajectoryStore,
    egress: EgressInventory,
    decisions: DecisionsClient,
) -> None:
    """The second listener answers callers the API server has already authorized, so it asks nothing.

    It is reachable only from the control plane; that is the whole of its protection, and it is why
    it must never be the port the gateway routes to.
    """
    app = create_app(inventory, bridge, store, MODELS, egress, decisions)
    port = pick_free_port()

    async with serve_app(app, port=port), httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as agent:
        assert (await agent.get("/sandboxes")).status_code == 200
        assert (await agent.get("/auth/me")).status_code == 404


if __name__ == "__main__":
    pytest_bazel.main()
