"""The two ways in, walked for real: an authorization-code round trip, and a reviewed token.

No hand-minted cookie and no stub app -- the session under test is the one an actual round trip
produced through `create_app`, `OperatorSessionMiddleware` and the real routers, so what these assert is
the boundary a browser meets. The token path runs against that same app, because on staging it is
the same app: one port, one guard, two credentials.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast

import httpx
import pytest
import pytest_bazel
import uvicorn
from sqlalchemy import select, update
from starlette.responses import Response
from starlette.routing import Route

from util.net import bind_free_port
from util.testing.asgi import serve_app
from util.testing.mock_oidc import build_mock_oidc_app, generate_rsa_keypair
from x.agentplane.app.api import Provider, create_app
from x.agentplane.app.bridge import RunnerBridge
from x.agentplane.app.conftest import AGENT, AGENT_AUTH, AUDIENCE, STRANGER_AUTH
from x.agentplane.app.decisions import DecisionsClient
from x.agentplane.app.egress import EgressInventory
from x.agentplane.app.identity import TokenReviewer
from x.agentplane.app.inventory import SandboxInventory
from x.agentplane.app.live import LiveIndex
from x.agentplane.app.oidc import OIDCSettings
from x.agentplane.app.operator_sessions import BrowserSession
from x.agentplane.app.testing.kubernetes import FakeAuthenticationV1Api
from x.agentplane.app.trajectory import TrajectoryStore

OPERATOR = "agentydragon"
SUBJECT = "op-subject-1"
SESSION_SECRET = "test-session-secret"  # a test literal, not a real credential
MODELS = {Provider.CLAUDE: ["test-claude-model"], Provider.CODEX: ["test-codex-model"]}


# Serves the app accepting tokens from exactly the subjects passed, yielding its base URL.
class ServeApp(Protocol):
    def __call__(
        self, subjects: frozenset[str], *, reject_token_exchange: bool = False, id_failure: str | None = None
    ) -> AbstractAsyncContextManager[str]: ...


@pytest.fixture
def serve(
    inventory: SandboxInventory,
    bridge: RunnerBridge,
    store: TrajectoryStore,
    egress: EgressInventory,
    decisions: DecisionsClient,
    authentication: FakeAuthenticationV1Api,
    live_index: LiveIndex,
) -> ServeApp:
    """The app as staging runs it -- a login and the token path on one port -- and its IdP."""

    @asynccontextmanager
    async def serving(
        subjects: frozenset[str], *, reject_token_exchange: bool = False, id_failure: str | None = None
    ) -> AsyncIterator[str]:
        private_key, public_key = generate_rsa_keypair()
        idp_sock, app_sock = bind_free_port(), bind_free_port()
        idp_url, app_url = (f"http://127.0.0.1:{sock.getsockname()[1]}" for sock in (idp_sock, app_sock))
        extra_claims: dict[str, Any] = {"preferred_username": OPERATOR}
        if id_failure == "issuer":
            extra_claims["iss"] = "https://wrong-issuer.invalid"
        elif id_failure == "audience":
            extra_claims["aud"] = "wrong-client"
        elif id_failure == "azp":
            extra_claims["azp"] = "wrong-client"
        elif id_failure == "expired":
            extra_claims["exp"] = 1
        elif id_failure == "subject":
            extra_claims["sub"] = ""
        elif id_failure == "signature":
            _, public_key = generate_rsa_keypair()
        idp = build_mock_oidc_app(
            issuer_url=idp_url,
            private_key=private_key,
            public_key=public_key,
            subject=SUBJECT,
            extra_id_token_claims=extra_claims,
        )
        if reject_token_exchange:

            async def reject_token(_request: Any) -> Response:
                return Response(status_code=403)

            idp.routes.insert(0, Route("/token", reject_token, methods=["POST"]))
        oidc = OIDCSettings(
            issuer=idp_url,
            client_id="agentplane",
            client_secret="agentplane-secret",  # a test literal, not a real credential
            session_secret=SESSION_SECRET,
            public_base_url=app_url,
        )
        reviewer = TokenReviewer(cast(Any, authentication), audience=AUDIENCE, subjects=subjects)
        app = create_app(inventory, bridge, store, MODELS, egress, decisions, live_index, oidc, reviewer)
        # The database pool belongs to this event loop, not serve_app's dedicated thread.
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
        async with serve_app(idp, sock=idp_sock):
            serving = asyncio.create_task(server.serve(sockets=[app_sock]))
            try:
                # The socket already accepts, so a probe connection proves nothing; wait for uvicorn itself.
                while not server.started:
                    if serving.done():
                        serving.result()
                        raise RuntimeError("uvicorn exited before starting")
                    await asyncio.sleep(0.02)
                yield app_url
            finally:
                server.should_exit = True
                await serving

    return serving


@pytest.fixture
async def served(serve: ServeApp) -> AsyncIterator[str]:
    """That app, accepting a token from the one agent staging names and from nobody else."""
    async with serve(frozenset({AGENT})) as app_url:
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


async def test_a_non_json_token_exchange_failure_is_reported_as_an_upstream_error(serve: ServeApp) -> None:
    async with (
        serve(frozenset({AGENT}), reject_token_exchange=True) as app_url,
        httpx.AsyncClient(base_url=app_url, follow_redirects=True) as browser,
    ):
        refused = await browser.get("/auth/login")

    assert refused.status_code == 502
    assert refused.json() == {"detail": "Identity provider returned an invalid response; please retry."}


async def test_an_unsafe_method_from_another_origin_is_refused(browser: httpx.AsyncClient, served: str) -> None:
    """SameSite=lax still lets a cross-site form post carry the cookie; the Origin check is what does not."""
    await browser.get("/auth/login")
    body = {"slug": "demo"}

    refused = await browser.post("/sandboxes", json=body, headers={"Origin": "https://evil.test"})

    assert refused.status_code == 403
    assert "cross-origin" in refused.json()["detail"]
    assert (await browser.post("/sandboxes", json=body, headers={"Origin": served})).status_code == 201


async def test_a_kubernetes_token_reaches_the_same_app_without_a_session(served: str) -> None:
    """The agent's credential: no cookie, no login, and an identity Kubernetes vouches for that
    this app was told to accept."""
    async with httpx.AsyncClient(base_url=served, headers=AGENT_AUTH) as agent:
        assert (await agent.get("/sandboxes")).status_code == 200
        # No Origin check on this path: a token is not ambient, so no site can make a browser send it.
        created = await agent.post(
            "/sandboxes", json={"slug": "demo", "policies": []}, headers={"Origin": "https://evil.test"}
        )
        assert created.status_code == 201, created.text

    async with httpx.AsyncClient(base_url=served, headers={"Authorization": "Bearer nonsense"}) as stranger:
        assert (await stranger.get("/sandboxes")).status_code == 401


async def test_a_token_for_another_service_account_is_refused(served: str) -> None:
    """The one the audience does not catch. Minting a token picks its audience freely -- RBAC gates
    which ServiceAccount you may mint for, never which audience you ask for -- so this token is as
    valid as the agent's and reaches TokenReview the same way. 403 and not 401 is what says it got
    that far: the app authenticated it and refused the identity behind it.
    """
    async with httpx.AsyncClient(base_url=served, headers=STRANGER_AUTH) as other_account:
        refused = await other_account.get("/sandboxes")
        created = await other_account.post("/sandboxes", json={"slug": "demo", "policies": []})

    assert (refused.status_code, created.status_code) == (403, 403), refused.text


async def test_an_empty_allowlist_leaves_a_session_the_only_way_in(serve: ServeApp) -> None:
    """The default an app is deployed with, and the state naming nobody has to mean: the token path
    admits no one, and the browser's is untouched."""
    async with serve(frozenset()) as app_url:
        async with httpx.AsyncClient(base_url=app_url, headers=AGENT_AUTH) as agent:
            refused = await agent.get("/sandboxes")
        async with httpx.AsyncClient(base_url=app_url, follow_redirects=True) as browser:
            await browser.get("/auth/login")
            allowed = await browser.get("/sandboxes")

    assert (refused.status_code, allowed.status_code) == (403, 200), refused.text


async def test_session_expiry_and_server_side_oauth_state(browser: httpx.AsyncClient, store: TrajectoryStore) -> None:
    response = await browser.get("/auth/login", follow_redirects=False)
    async with store.operator_sessions.sessions() as db:
        row = (await db.scalars(select(BrowserSession))).one()
        assert "_state_" in next(iter(row.payload))
        assert "code_verifier" in str(row.payload)
        assert row.expires_at <= datetime.now(UTC) + timedelta(minutes=10)
    await browser.get(response.headers["location"])
    async with store.operator_sessions.sessions.begin() as db:
        row = (await db.scalars(select(BrowserSession))).one()
        assert row.payload["user"]["issuer"]
        assert row.payload["user"]["subject"] == SUBJECT
        assert row.payload["user"]["username"] == OPERATOR
        assert "refresh_token" not in row.payload["user"]
        assert row.payload["user"]["access_token"] is None, "disabled federation needs no retained token"
        assert list(row.payload) == ["user"], "OAuth nonce/state/PKCE are consumed at login"
        await db.execute(update(BrowserSession).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    assert (await browser.get("/auth/me")).status_code == 401
    assert (await browser.get("/sandboxes")).status_code == 401


async def test_logout_and_mutations_require_exact_origin(browser: httpx.AsyncClient, served: str) -> None:
    await browser.get("/auth/login")
    for headers in ({}, {"Origin": "https://evil.test"}, {"Origin": served + "/"}):
        assert (await browser.post("/auth/logout", headers=headers)).status_code == 403
        assert (await browser.post("/sandboxes", json={"slug": "blocked"}, headers=headers)).status_code == 403
    assert (await browser.get("/auth/me")).status_code == 200


@pytest.mark.parametrize("id_failure", ["issuer", "audience", "azp", "expired", "subject", "signature"])
async def test_invalid_signed_login_never_creates_operator_session(serve: ServeApp, id_failure: str) -> None:
    async with (
        serve(frozenset(), id_failure=id_failure) as app_url,
        httpx.AsyncClient(base_url=app_url, follow_redirects=True) as browser,
    ):
        refused = await browser.get("/auth/login")
        assert refused.status_code == 401
        assert (await browser.get("/auth/me")).status_code == 401
        assert (await browser.get("/actions")).status_code == 401


@pytest.mark.parametrize("parameter", ["state", "nonce"])
async def test_login_binds_state_and_nonce(browser: httpx.AsyncClient, parameter: str) -> None:
    login = await browser.get("/auth/login", follow_redirects=False)
    altered = httpx.URL(login.headers["location"]).copy_set_param(parameter, "test-wrong-value")
    refused = await browser.get(altered)
    assert refused.status_code == 401
    assert (await browser.get("/auth/me")).status_code == 401


async def test_callback_rotates_handle_and_cannot_be_replayed(browser: httpx.AsyncClient) -> None:
    login = await browser.get("/auth/login", follow_redirects=False)
    pending = httpx.Cookies(browser.cookies)
    authorization = await browser.get(login.headers["location"], follow_redirects=False)
    callback_url = authorization.headers["location"]
    assert (await browser.get(callback_url, follow_redirects=False)).status_code == 303
    authenticated = httpx.Cookies(browser.cookies)
    assert list(pending.values()) != list(authenticated.values())
    assert (await browser.get(callback_url, follow_redirects=False)).status_code == 401
    browser.cookies.clear()
    browser.cookies.update(pending)
    assert (await browser.get("/auth/me")).status_code == 401
    assert (await browser.get(callback_url, follow_redirects=False)).status_code == 401
    browser.cookies.clear()
    browser.cookies.update(authenticated)
    assert (await browser.get("/auth/me")).status_code == 200


async def test_expired_pending_login_cannot_finish(browser: httpx.AsyncClient, store: TrajectoryStore) -> None:
    login = await browser.get("/auth/login", follow_redirects=False)
    async with store.operator_sessions.sessions.begin() as db:
        await db.execute(update(BrowserSession).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    assert (await browser.get(login.headers["location"])).status_code == 401
    assert (await browser.get("/auth/me")).status_code == 401


async def test_callback_error_does_not_echo_untrusted_provider_text(
    browser: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    marker = "test-sensitive-provider-value"
    response = await browser.get("/auth/callback", params={"error": marker, "error_description": marker})
    assert response.status_code == 401
    assert marker not in response.text
    assert marker not in caplog.text


if __name__ == "__main__":
    pytest_bazel.main()
