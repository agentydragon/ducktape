"""The page's boundary as a browser meets it: a real login round trip against a mock IdP, then the API."""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import pytest_bazel
from starlette.types import Receive, Scope, Send

from devinfra.claude.session_export.conftest import (
    PAIRED_RESPONSE,
    TEST_ORG_UUID,
    FakeTokenEndpoint,
    authorization_state,
    redirect_url,
)
from devinfra.claude.session_export.oauth import CredentialStore
from devinfra.claude.session_export.settings import ServeSettings, WebSettings
from devinfra.claude.session_export.store import SessionStore
from devinfra.claude.session_export.supervisor import SyncSupervisor
from devinfra.claude.session_export.web import create_app, create_control_app, create_web_app
from util.net import bind_free_port
from util.testing.asgi import serve_app, serve_app_in_loop
from util.testing.mock_oidc import build_mock_oidc_app, generate_rsa_keypair

OWNER = "test-owner-subject"

Serve = Callable[..., AbstractAsyncContextManager[str]]


@pytest.fixture
def serve(store: SessionStore, tmp_path: Path) -> Serve:
    """The app and a mock IdP whose login yields `subject`, on real sockets; yields the app's origin."""

    @asynccontextmanager
    async def serving(*, subject: str = OWNER, token_status: int = 200) -> AsyncIterator[str]:
        private_key, public_key = generate_rsa_keypair()
        idp_sock, app_sock = bind_free_port(), bind_free_port()
        idp_url, app_url = (f"http://127.0.0.1:{sock.getsockname()[1]}" for sock in (idp_sock, app_sock))
        idp = build_mock_oidc_app(
            issuer_url=idp_url,
            private_key=private_key,
            public_key=public_key,
            subject=subject,
            extra_id_token_claims={"preferred_username": "test-owner"},
        )
        settings = ServeSettings(
            database_url="postgresql://unused.invalid/unused",
            credentials_file=tmp_path / "credential.json",
            public_base_url=app_url,
            oidc_issuer=idp_url,
            oidc_client_id="test-client",
            oidc_client_secret="test-client-secret",
            oidc_session_secret="test-session-secret",
            oidc_allowed_subject=OWNER,
        )
        supervisor = SyncSupervisor(
            credentials=CredentialStore(settings.credentials_file),
            store=store,
            token_client=FakeTokenEndpoint(PAIRED_RESPONSE, status=token_status).client,
            interval=3600,
            workers=1,
            live_streams=0,
            live_window=timedelta(hours=1),
        )
        # The app runs in this loop, not in `serve_app`'s thread: its `store` holds this loop's asyncpg connections.
        async with (
            serve_app(idp, sock=idp_sock),
            serve_app_in_loop(create_app(supervisor=supervisor, settings=settings), sock=app_sock),
        ):
            yield app_url

    return serving


@pytest.fixture
async def owner(serve: Serve) -> AsyncIterator[httpx.AsyncClient]:
    """A browser that has completed the login as the owner."""
    async with serve() as app_url, httpx.AsyncClient(base_url=app_url, headers={"Origin": app_url}) as browser:
        await browser.get("/auth/login", follow_redirects=True)
        yield browser


async def test_everything_but_health_needs_the_login_and_the_owner_completes_it(serve: Serve) -> None:
    async with serve() as app_url, httpx.AsyncClient(base_url=app_url) as browser:
        assert (await browser.get("/healthz")).status_code == 200
        assert (await browser.get("/api/status")).status_code == 401
        page = await browser.get("/")
        assert (page.status_code, page.headers["location"]) == (303, "/auth/login")

        landed = await browser.get("/auth/login", follow_redirects=True)
        assert landed.url.path == "/"  # the login ends at the root, which only the SPA mount serves
        status = await browser.get("/api/status")

    assert status.status_code == 200
    assert status.json()["state"] == "unpaired"
    assert status.headers["cache-control"] == "no-store"


async def test_someone_who_passes_login_but_is_not_the_owner_is_refused(serve: Serve) -> None:
    async with serve(subject="somebody-else") as app_url, httpx.AsyncClient(base_url=app_url) as browser:
        refused = await browser.get("/auth/login", follow_redirects=True)
        assert refused.status_code == 401
        assert "owner only" in refused.text
        assert (await browser.get("/api/status")).status_code == 401


async def test_the_owner_pairs_by_pasting_the_redirect_url(owner: httpx.AsyncClient) -> None:
    started = await owner.post("/api/pairing")

    paired = await owner.post(
        "/api/pairing/complete",
        json={"redirect_url": redirect_url(authorization_state(started.json()["authorization_url"]))},
    )

    assert paired.status_code == 200
    assert paired.json()["credential"]["organization_uuid"] == TEST_ORG_UUID
    assert (await owner.get("/api/status")).json()["pairing_started"] is False


async def test_pairing_can_start_on_one_web_replica_and_finish_on_another(store: SessionStore, tmp_path: Path) -> None:
    """Both public pods proxy to the single control process that owns the PKCE attempt and credential."""
    private_key, public_key = generate_rsa_keypair()
    idp_sock, app_sock = bind_free_port(), bind_free_port()
    idp_url = f"http://127.0.0.1:{idp_sock.getsockname()[1]}"
    app_url = f"http://127.0.0.1:{app_sock.getsockname()[1]}"
    idp = build_mock_oidc_app(
        issuer_url=idp_url,
        private_key=private_key,
        public_key=public_key,
        subject=OWNER,
        extra_id_token_claims={"preferred_username": "test-owner"},
    )
    web_settings = WebSettings(
        database_url="postgresql://unused.invalid/unused",
        public_base_url=app_url,
        oidc_issuer=idp_url,
        oidc_client_id="test-client",
        oidc_client_secret="test-client-secret",
        oidc_session_secret="test-session-secret",
        oidc_allowed_subject=OWNER,
        control_base_url="http://control",
    )
    control_settings = ServeSettings(
        database_url="postgresql://unused.invalid/unused",
        credentials_file=tmp_path / "credential.json",
        public_base_url=app_url,
        oidc_issuer=idp_url,
        oidc_client_id="test-client",
        oidc_client_secret="test-client-secret",
        oidc_session_secret="test-session-secret",
        oidc_allowed_subject=OWNER,
    )
    supervisor = SyncSupervisor(
        credentials=CredentialStore(control_settings.credentials_file),
        store=store,
        token_client=FakeTokenEndpoint(PAIRED_RESPONSE).client,
        interval=3600,
        workers=1,
        live_streams=0,
        live_window=timedelta(hours=1),
    )
    control_app = create_control_app(supervisor=supervisor)
    async with (
        serve_app(idp, sock=idp_sock),
        httpx.AsyncClient(base_url="http://control", transport=httpx.ASGITransport(app=control_app)) as control_client,
    ):
        web_one = create_web_app(settings=web_settings, control_client=control_client, frontend_dir=tmp_path)
        web_two = create_web_app(settings=web_settings, control_client=control_client, frontend_dir=tmp_path)

        async def route_to_replica(scope: Scope, receive: Receive, send: Send) -> None:
            app = web_two if scope["path"] == "/api/pairing/complete" else web_one
            await app(scope, receive, send)

        async with (
            serve_app_in_loop(route_to_replica, sock=app_sock),
            httpx.AsyncClient(base_url=app_url, headers={"Origin": app_url}) as owner,
        ):
            await owner.get("/auth/login", follow_redirects=True)
            started = await owner.post("/api/pairing")
            finished = await owner.post(
                "/api/pairing/complete",
                json={"redirect_url": redirect_url(authorization_state(started.json()["authorization_url"]))},
            )

    assert started.status_code == 200
    assert finished.status_code == 200
    assert finished.json()["credential"]["organization_uuid"] == TEST_ORG_UUID


async def test_a_pasted_url_from_another_attempt_is_a_client_error(owner: httpx.AsyncClient) -> None:
    await owner.post("/api/pairing")
    refused = await owner.post("/api/pairing/complete", json={"redirect_url": redirect_url("another-attempt")})
    assert refused.status_code == 400
    assert "state differs" in refused.json()["detail"]


async def test_a_code_anthropic_refuses_is_reported_without_its_body(serve: Serve) -> None:
    async with (
        serve(token_status=400) as app_url,
        httpx.AsyncClient(base_url=app_url, headers={"Origin": app_url}) as owner,
    ):
        await owner.get("/auth/login", follow_redirects=True)
        started = await owner.post("/api/pairing")
        refused = await owner.post(
            "/api/pairing/complete",
            json={"redirect_url": redirect_url(authorization_state(started.json()["authorization_url"]))},
        )
    assert refused.status_code == 502
    assert refused.json()["detail"] == "Anthropic refused the authorization code (400); start pairing again."


if __name__ == "__main__":
    pytest_bazel.main()
