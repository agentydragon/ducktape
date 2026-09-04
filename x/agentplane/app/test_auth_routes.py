"""The two ways in, walked for real: an authorization-code round trip, and a reviewed token.

No hand-minted cookie and no stub app -- the session under test is the one an actual round trip
produced through `create_app`, `SessionMiddleware` and the real routers, so what these assert is
the boundary a browser meets. The token path runs against that same app, because on staging it is
the same app: one port, one guard, two credentials.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_bazel

from util.net import pick_free_port
from util.testing.asgi import serve_app
from util.testing.mock_oidc import build_mock_oidc_app, generate_rsa_keypair
from x.agentplane.app.api import Provider, create_app
from x.agentplane.app.bridge import RunnerBridge
from x.agentplane.app.conftest import AGENT_AUTH
from x.agentplane.app.decisions import DecisionsClient
from x.agentplane.app.egress import EgressInventory
from x.agentplane.app.identity import TokenReviewer
from x.agentplane.app.inventory import SandboxInventory
from x.agentplane.app.live import LiveIndex
from x.agentplane.app.oidc import OIDCSettings
from x.agentplane.app.trajectory import TrajectoryStore

# SessionMiddleware signs cookies with itsdangerous, imported inside starlette;
# gazelle cannot see the dependency.
# gazelle:include_dep @pypi//itsdangerous

OPERATOR = "agentydragon"
SUBJECT = "op-subject-1"
SESSION_SECRET = "test-session-secret"  # a test literal, not a real credential
MODELS = {Provider.CLAUDE: ["test-claude-model"], Provider.CODEX: ["test-codex-model"]}


@pytest.fixture
async def served(
    inventory: SandboxInventory,
    bridge: RunnerBridge,
    store: TrajectoryStore,
    egress: EgressInventory,
    decisions: DecisionsClient,
    live_index: LiveIndex,
    reviewer: TokenReviewer,
) -> AsyncIterator[str]:
    """The app as staging runs it -- a login and the token path on one port -- and its IdP."""
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
    app = create_app(inventory, bridge, store, MODELS, egress, decisions, live_index, oidc, reviewer)
    async with serve_app(idp, port=idp_port), serve_app(app, port=app_port):
        yield app_url


@pytest.fixture
async def browser(served: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=served, follow_redirects=True) as client:
        yield client


async def test_a_request_without_a_session_is_refused_and_the_round_trip_grants_one(browser: httpx.AsyncClient) -> None:
    assert (await browser.get("/sandboxes")).status_code == 401
    assert (await browser.get("/auth/me")).status_code == 401

    landed = await browser.get("/auth/login")

    # Login ends at the app root, which only the SPA mount serves; this test builds the API alone,
    # so what it can assert is where the browser was sent, not what answered there.
    assert landed.url.path == "/"
    assert (await browser.get("/auth/me")).json() == {"username": OPERATOR}
    assert (await browser.get("/sandboxes")).status_code == 200


async def test_logout_drops_the_session(browser: httpx.AsyncClient, served: str) -> None:
    await browser.get("/auth/login")

    assert (await browser.post("/auth/logout", headers={"Origin": served})).url.path == "/"
    assert (await browser.get("/auth/me")).status_code == 401
    assert (await browser.get("/sandboxes")).status_code == 401


async def test_an_unsafe_method_from_another_origin_is_refused(browser: httpx.AsyncClient, served: str) -> None:
    """SameSite=lax still lets a cross-site form post carry the cookie; the Origin check is what does not."""
    await browser.get("/auth/login")
    body = {"slug": "demo"}

    refused = await browser.post("/sandboxes", json=body, headers={"Origin": "https://evil.test"})

    assert refused.status_code == 403
    assert "cross-origin" in refused.json()["detail"]
    assert (await browser.post("/sandboxes", json=body, headers={"Origin": served})).status_code == 201


async def test_a_kubernetes_token_reaches_the_same_app_without_a_session(served: str) -> None:
    """The agent's credential: no cookie, no login, and the identity Kubernetes vouches for."""
    async with httpx.AsyncClient(base_url=served, headers=AGENT_AUTH) as agent:
        assert (await agent.get("/sandboxes")).status_code == 200
        # No Origin check on this path: a token is not ambient, so no site can make a browser send it.
        created = await agent.post(
            "/sandboxes", json={"slug": "demo", "policies": []}, headers={"Origin": "https://evil.test"}
        )
        assert created.status_code == 201, created.text

    async with httpx.AsyncClient(base_url=served, headers={"Authorization": "Bearer nonsense"}) as stranger:
        assert (await stranger.get("/sandboxes")).status_code == 401


if __name__ == "__main__":
    pytest_bazel.main()
