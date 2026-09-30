"""The page's boundary as a browser meets it: a real login round trip against a mock IdP, then the API."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_bazel
import uvicorn

from devinfra.claude.session_export.conftest import PAIRED_RESPONSE, FakeTokenEndpoint
from devinfra.claude.session_export.oauth import CredentialStore
from devinfra.claude.session_export.settings import ServeSettings
from devinfra.claude.session_export.store import SessionStore
from devinfra.claude.session_export.supervisor import SyncSupervisor
from devinfra.claude.session_export.web import create_app
from util.net import bind_free_port
from util.testing.asgi import serve_app
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
        )
        server = uvicorn.Server(
            uvicorn.Config(create_app(supervisor=supervisor, settings=settings), log_level="warning")
        )
        async with serve_app(idp, sock=idp_sock):
            serving = asyncio.create_task(server.serve(sockets=[app_sock]))
            try:
                while not server.started:  # a pre-bound socket accepts before uvicorn does
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
    state = parse_qs(urlsplit(started.json()["authorization_url"]).query)["state"][0]

    paired = await owner.post(
        "/api/pairing/complete", json={"redirect_url": f"http://localhost:54545/callback?code=test-code&state={state}"}
    )

    assert paired.status_code == 200
    assert paired.json()["credential"]["organization_uuid"] == "test-org-uuid"
    assert (await owner.get("/api/status")).json()["pairing_started"] is False


async def test_a_pasted_url_from_another_attempt_is_a_client_error(owner: httpx.AsyncClient) -> None:
    await owner.post("/api/pairing")
    refused = await owner.post(
        "/api/pairing/complete",
        json={"redirect_url": "http://localhost:54545/callback?code=test-code&state=another-attempt"},
    )
    assert refused.status_code == 400
    assert "state differs" in refused.json()["detail"]


async def test_a_code_anthropic_refuses_is_reported_without_its_body(serve: Serve) -> None:
    async with (
        serve(token_status=400) as app_url,
        httpx.AsyncClient(base_url=app_url, headers={"Origin": app_url}) as owner,
    ):
        await owner.get("/auth/login", follow_redirects=True)
        state = parse_qs(urlsplit((await owner.post("/api/pairing")).json()["authorization_url"]).query)["state"][0]
        refused = await owner.post(
            "/api/pairing/complete",
            json={"redirect_url": f"http://localhost:54545/callback?code=test-code&state={state}"},
        )
    assert refused.status_code == 502
    assert refused.json()["detail"] == "Anthropic refused the authorization code (400); start pairing again."


async def test_a_write_from_another_origin_is_refused(owner: httpx.AsyncClient) -> None:
    """SameSite=lax still lets a cross-site form post carry the cookie; the Origin check is what does not."""
    refused = await owner.post("/api/pairing", headers={"Origin": "https://evil.test"})
    assert refused.status_code == 403
    assert (await owner.post("/api/pairing")).status_code == 200


if __name__ == "__main__":
    pytest_bazel.main()
