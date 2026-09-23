"""The SPA mount's cache contract, the two settings models the Deployment's environment feeds, and
the shutdown the Deployment's grace period is sized for."""

from __future__ import annotations

import asyncio
import signal
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
import pytest_bazel
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.app.action_policy import ActionPolicyInventory
from agentplane.app.agent_runtime.events.event_log import EventLogStore
from agentplane.app.api import create_app
from agentplane.app.bridge import RunnerBridge
from agentplane.app.conftest import AGENT_AUTH
from agentplane.app.decisions import DecisionsClient
from agentplane.app.egress import EgressInventory
from agentplane.app.identity import TokenReviewer
from agentplane.app.ingestion import Ingester
from agentplane.app.inventory import SandboxInventory
from agentplane.app.live import LiveIndex
from agentplane.app.main import AppServer, Settings, SpaFiles, resolved_agent_instructions, serve_then_close
from agentplane.app.oidc import load_settings
from agentplane.app.operator_sessions import OperatorSessionStore
from agentplane.app.presets import Harness
from agentplane.app.runners import Runners
from agentplane.app.shutdown import drain_of
from agentplane.app.testing.kubernetes import pod, sandbox
from agentplane.app.thread.content import ContentStore
from agentplane.app.thread.models import SandboxIngestion
from agentplane.app.thread.store import ThreadStore
from agentplane.app.thread.updates import ThreadUpdates
from util.net import pick_free_port

APP_ENVIRONMENT = {
    "AGENTPLANE_NAMESPACE": "test-namespace",
    "AGENTPLANE_SANDBOX_NAMESPACE": "test-sandbox-namespace",
    "AGENTPLANE_RUNNER_PORT": "7000",
    "AGENTPLANE_DATABASE_URL": "postgresql+asyncpg://test@test.invalid/test",
    "AGENTPLANE_MODELS": '{"HARNESS_CLAUDE": ["test-claude-model"], "HARNESS_CODEX": ["test-codex-model"]}',
    "AGENTPLANE_EGRESS_ADMIN_URL": "http://egress.test.invalid:8081",
}
OIDC_ENVIRONMENT = {
    "AGENTPLANE_OIDC_ISSUER": "https://auth.test.invalid/application/o/test-app/",
    "AGENTPLANE_OIDC_CLIENT_ID": "test-client",
    "AGENTPLANE_OIDC_CLIENT_SECRET": "test-client-secret",  # a test literal, not a real credential
    "AGENTPLANE_OIDC_SESSION_SECRET": "test-session-secret",  # a test literal, not a real credential
    "AGENTPLANE_OIDC_PUBLIC_BASE_URL": "https://app.test.invalid",
}


def test_spa_files_are_never_reused_from_a_browser_cache(tmp_path: Path) -> None:
    """Bazel's fixed mtimes would otherwise validate a stale bundle: no-store, and no 304."""
    (tmp_path / "index.html").write_text("<!doctype html>")
    (tmp_path / "main.js").write_text("console.log(1)")
    app = FastAPI()
    app.mount("/", SpaFiles(directory=tmp_path, html=True), name="frontend")
    client = TestClient(app)

    first = client.get("/main.js")
    again = client.get("/main.js", headers={"If-None-Match": first.headers.get("etag", "*")})
    shell = client.get("/")

    assert first.headers["cache-control"] == "no-store"
    assert "etag" not in first.headers
    assert again.status_code == 200
    assert (shell.status_code, shell.headers["cache-control"], shell.headers["content-type"]) == (
        200,
        "no-store",
        "text/html; charset=utf-8",
    )


def test_the_two_settings_models_read_one_environment_without_colliding(monkeypatch: pytest.MonkeyPatch) -> None:
    """`AGENTPLANE_OIDC_` sits inside `AGENTPLANE_`, and the staging Deployment sets both.

    A model that claimed the other's variables, or refused to parse alongside them, would take the
    app down at rollout rather than here.
    """
    for name, value in (APP_ENVIRONMENT | OIDC_ENVIRONMENT).items():
        monkeypatch.setenv(name, value)

    settings = Settings(_cli_parse_args=[])
    oidc = load_settings()

    assert (settings.namespace, settings.sandbox_namespace) == ("test-namespace", "test-sandbox-namespace")
    assert (settings.port, settings.token_audience) == (8080, "agentplane")
    assert oidc is not None
    assert oidc.redirect_uri == "https://app.test.invalid/auth/callback"
    assert (
        oidc.server_metadata_url == "https://auth.test.invalid/application/o/test-app/.well-known/openid-configuration"
    )
    # https, so the cookie takes the __Host- prefix that binds it to this exact origin.
    assert oidc.cookie_name.startswith("__Host-")


def test_image_owned_agent_instructions_render_deployment_service_urls() -> None:
    instructions = resolved_agent_instructions(
        None, egress_api_url="http://egress.test.invalid", actions_service_url="http://actions.test.invalid:8080"
    )

    assert "http://egress.test.invalid/v1/rules" in instructions
    assert "http://egress.test.invalid/openapi.json" in instructions
    assert "http://actions.test.invalid:8080/openapi.json" in instructions


def test_without_an_issuer_there_is_no_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """The app is guarded either way; unset, what is missing is the browser's way to get a session."""
    for name in OIDC_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)

    assert load_settings() is None


SANDBOX = "shutdown-test-sandbox"
MODELS = {Harness.CLAUDE: ["test-claude-model"], Harness.CODEX: ["test-codex-model"]}


@pytest.fixture
def sigterm_is_survivable() -> Iterator[None]:
    """Uvicorn re-raises a captured signal to the previous handler once it has stopped, and the
    default one would take pytest with it."""
    original = signal.signal(signal.SIGTERM, lambda _sig, _frame: None)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, original)


@pytest.fixture
async def database(db_url: str) -> AsyncIterator[AsyncEngine]:
    """The database as another replica sees it: the lease table, and who is connected."""
    engine = create_async_engine(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _leases(database: AsyncEngine) -> int:
    async with database.connect() as connection:
        return (
            await connection.scalar(
                select(func.count()).select_from(SandboxIngestion).where(SandboxIngestion.sandbox == SANDBOX)
            )
        ) or 0


async def _other_connections(database: AsyncEngine) -> int:
    async with database.connect() as connection:
        return (
            await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()"
                )
            )
        ) or 0


@pytest.mark.usefixtures("sigterm_is_survivable")
async def test_sigterm_ends_open_streams_fails_readiness_and_closes_the_ingester_and_database(
    inventory: SandboxInventory,
    bridge: RunnerBridge,
    store: ThreadStore,
    thread_updates: ThreadUpdates,
    engine: AsyncEngine,
    operator_sessions: OperatorSessionStore,
    egress: EgressInventory,
    decisions: DecisionsClient,
    live_index: LiveIndex,
    action_policy: ActionPolicyInventory,
    reviewer: TokenReviewer,
    database: AsyncEngine,
    event_logs: EventLogStore,
    content: ContentStore,
    runners: Runners,
    ingester: Ingester,
) -> None:
    """A tab holding `/live/sandboxes` open used to hold Uvicorn's shutdown open with it. The stream
    now ends at the signal -- cleanly, which a stream cancelled at the budget would not -- readiness
    fails while the drain is on, and the unwind then releases the ingestion lease and every
    connection the app held."""
    live_index.sandboxes[SANDBOX] = sandbox(SANDBOX)
    live_index.pods[SANDBOX] = pod(SANDBOX, phase="Running", ready=True, ip="127.0.0.1")
    app = create_app(
        inventory,
        bridge,
        store,
        MODELS,
        egress,
        decisions,
        live_index,
        action_policy,
        reviewer=reviewer,
        event_logs=event_logs,
        content=content,
        thread_updates=thread_updates,
        operator_sessions=operator_sessions,
    )
    port = pick_free_port()
    # A budget the test would never wait out: the stream has to end because of the drain, not this.
    server = AppServer(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", timeout_graceful_shutdown=60),
        drain_of(app),
    )
    serving = asyncio.create_task(
        serve_then_close(server, ingester=ingester, runners=runners, thread_updates=thread_updates, engine=engine)
    )
    # The ingester leases the sandbox it cannot dial; the socket already accepts, so wait for uvicorn itself.
    while not server.started or await _leases(database) == 0:
        if serving.done():
            serving.result()
            raise RuntimeError("uvicorn exited before starting")
        await asyncio.sleep(0.02)
    assert await _other_connections(database) > 0

    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers=AGENT_AUTH) as client:
        assert (await client.get("/readyz")).status_code == 204
        async with client.stream("GET", "/live/sandboxes") as stream:
            lines = stream.aiter_lines()
            async for line in lines:
                if line == "event: snapshot":
                    break
            signal.raise_signal(signal.SIGTERM)
            # To the end of the body: a stream cancelled at the budget raises RemoteProtocolError here.
            async for _line in lines:
                pass
    assert drain_of(app).draining
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app.test") as probes:
        assert (await probes.get("/readyz")).status_code == 503
        assert (await probes.get("/healthz")).status_code == 204

    await serving

    assert await _leases(database) == 0
    # A closed connection's backend leaves pg_stat_activity a moment after the client let go of it.
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(10), wait=wait_fixed(0.02), retry=retry_if_exception_type(AssertionError), reraise=True
    ):
        with attempt:
            assert await _other_connections(database) == 0


if __name__ == "__main__":
    pytest_bazel.main()
