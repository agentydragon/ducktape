"""The page's boundary as a browser meets it: a real login round trip against a mock IdP, then the API."""

import asyncio
import json
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
    make_event,
    make_session,
    redirect_url,
)
from devinfra.claude.session_export.models import Event
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


async def _next_sse_frame(response: httpx.Response) -> dict[str, str]:
    fields: dict[str, str] = {}
    async for line in response.aiter_lines():
        if line == "" and fields:
            return fields
        name, separator, value = line.partition(":")
        if separator:
            fields[name] = value.lstrip()
    raise AssertionError("watch stream ended before sending an event")


@pytest.fixture
def serve(store: SessionStore, tmp_path: Path) -> Serve:
    """The app and a mock IdP whose login yields `subject`, on real sockets; yields the app's origin."""

    @asynccontextmanager
    async def serving(*, subject: str = OWNER, token_status: int = 200) -> AsyncIterator[str]:
        frontend_dir = tmp_path / "frontend"
        frontend_dir.mkdir()
        (frontend_dir / "index.html").write_text(
            "<!doctype html><title>session sync test shell</title>", encoding="utf-8"
        )
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
            serve_app_in_loop(
                create_app(supervisor=supervisor, settings=settings, store=store, frontend_dir=frontend_dir),
                sock=app_sock,
            ),
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
        sessions_page = await browser.get("/sessions")
        sync_page = await browser.get("/sync")
        assert (sessions_page.status_code, sessions_page.headers["location"]) == (303, "/auth/login")
        assert (sync_page.status_code, sync_page.headers["location"]) == (303, "/auth/login")

        landed = await browser.get("/auth/login", follow_redirects=True)
        assert landed.url.path == "/sessions"
        assert (await browser.get("/sync")).text == (await browser.get("/sessions")).text
        root = await browser.get("/")
        assert (root.status_code, root.headers["location"]) == (307, "/sessions")
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
        web_one = create_web_app(
            settings=web_settings, control_client=control_client, store=store, frontend_dir=tmp_path
        )
        web_two = create_web_app(
            settings=web_settings, control_client=control_client, store=store, frontend_dir=tmp_path
        )

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


async def test_claude_shaped_read_routes_page_sessions_and_events(
    owner: httpx.AsyncClient, store: SessionStore
) -> None:
    active = make_session("session_active0001", status="active")
    paused = make_session("session_paused0001", status="paused")
    archived = make_session("session_archived01", status="archived")
    await store.upsert_sessions([active, paused, archived])
    await store.append_events(active.id, [Event.model_validate(make_event(seq)) for seq in range(1, 4)])

    sessions = await owner.get("/v1/code/sessions?limit=1")
    next_page = await owner.get(f"/v1/code/sessions?limit=1&cursor={sessions.json()['next_cursor']}")
    all_sessions = await owner.get("/v1/code/sessions?statuses=active&statuses=archived&limit=10")
    detail = await owner.get(f"/v1/code/sessions/cse_{active.id.removeprefix('session_')}")
    newest = await owner.get(f"/v1/code/sessions/{active.id}/events?limit=2&sort_order=desc")
    older = await owner.get(
        f"/v1/code/sessions/{active.id}/events?limit=2&sort_order=desc&cursor={newest.json()['last_id']}"
    )

    assert sessions.status_code == next_page.status_code == all_sessions.status_code == detail.status_code == 200
    default_ids = [item["id"] for item in (sessions.json()["data"] + next_page.json()["data"])]
    assert set(default_ids) == {active.id, paused.id}
    assert {item["id"] for item in all_sessions.json()["data"]} == {active.id, archived.id}
    assert detail.json()["session"]["id"] == active.id
    assert [int(event["sequence_num"]) for event in newest.json()["data"]] == [3, 2]
    assert [int(event["sequence_num"]) for event in older.json()["data"]] == [1]
    assert newest.json()["has_more"] is True
    assert older.json()["has_more"] is False


async def test_session_watch_replays_changes_and_resumes_from_last_event_id(
    owner: httpx.AsyncClient, store: SessionStore
) -> None:
    page = await owner.get("/v1/code/sessions")
    resume_token = page.json()["resume_token"]
    session = make_session("session_watch0001", status="active")
    await store.upsert_sessions([session])

    async with owner.stream("GET", "/v1/code/sessions/watch", params={"resume_token": resume_token}) as response:
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        first = await asyncio.wait_for(_next_sse_frame(response), timeout=3)

    assert first["event"] == "changed"
    assert json.loads(first["data"]) == {"session_ids": [session.id]}

    updated = make_session(session.id, title="Updated watch session", status="active")
    async with owner.stream(
        "GET", "/v1/code/sessions/watch", params={"resume_token": resume_token}, headers={"Last-Event-ID": first["id"]}
    ) as response:
        await store.upsert_sessions([updated])
        second = await asyncio.wait_for(_next_sse_frame(response), timeout=3)

    assert second["id"] == "1:2"
    assert second["event"] == "changed"
    assert json.loads(second["data"]) == {"session_ids": [session.id]}


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
