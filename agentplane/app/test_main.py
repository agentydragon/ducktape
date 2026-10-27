"""The settings models fed by the Deployment and the shutdown its grace period is sized for."""

from __future__ import annotations

import asyncio
import json
import signal
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
import pytest_bazel
import uvicorn
from pydantic import AnyHttpUrl, ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.app.action_policy import ActionPolicyInventory
from agentplane.app.api import create_app
from agentplane.app.database_updates import create_database_updates
from agentplane.app.egress_access import EgressAccess
from agentplane.app.identity import TokenReviewer
from agentplane.app.live import LiveIndex
from agentplane.app.main import AppServer, serve_then_close
from agentplane.app.notifications_operator_client import NotificationsOperatorClient
from agentplane.app.oidc import OIDCSettings
from agentplane.app.operator_sessions import OperatorSessionStore
from agentplane.app.sandbox_access import SandboxAccess
from agentplane.app.settings import FrontendConfig, LangfuseSearchSettings, Settings, TraceSearchSettings
from agentplane.app.shutdown import drain_of
from agentplane.app.testing.history import SeededEventLogStore
from agentplane.app.testing.model_test_data import TEST_MODELS, TEST_REASONING_EFFORTS
from agentplane.app.testing.system_status import answering_system_status
from agentplane.app.threads.bridge import RunnerBridge
from agentplane.app.threads.dispatcher_client import SessionDispatcherClient
from agentplane.app.threads.history_watch import SessionOpenings
from agentplane.app.threads.runner_directory import RunnerDirectory
from agentplane.app.threads.store import ThreadStore
from agentplane.app.threads.view.content import ContentStore
from agentplane.egress.decisions_client import DecisionsClient
from agentplane.history_service.client import HistoryServiceClient
from agentplane.postgres.wakeups import WakeupListener
from agentplane.sandboxes.testing.sandbox_namespace import SandboxNamespace
from util.net import Listener, listen_tcp

APP_ENVIRONMENT = {
    "AGENTPLANE_SANDBOX_NAMESPACE": "test-sandbox-namespace",
    "AGENTPLANE_RUNNER_GATEWAY_TARGET": "runner-gateway.test:8080",
    "AGENTPLANE_SESSION_DISPATCHER_TARGET": "session-dispatcher.test:8080",
    "AGENTPLANE_SESSION_DISPATCHER_HTTP_URL": "http://session-dispatcher.test.invalid:8081",
    "AGENTPLANE_RUNNER_GATEWAY_CONTROL_TARGET": "{sandbox}-control.{namespace}.test:8080",
    "AGENTPLANE_RUNNER_GATEWAY_TOKEN_FILE": "/tokens/runner-gateway",
    "AGENTPLANE_HISTORY_SERVICE_TARGET": "history-service.test:8080",
    "AGENTPLANE_DATABASE_URL": "postgresql://test@test.invalid/test",
    "AGENTPLANE_MODELS": json.dumps(
        {
            "models": [
                {"model": model, "display_name": display, "reasoning_efforts": list(TEST_REASONING_EFFORTS)}
                for model, display in (
                    ("test-claude-model", "Test Claude Model"),
                    ("test-codex-model", "Test Codex Model"),
                )
            ],
            "harnesses": {"HARNESS_CLAUDE": ["test-claude-model"], "HARNESS_CODEX": ["test-codex-model"]},
        }
    ),
    "AGENTPLANE_EGRESS_ADMIN_URL": "http://egress.test.invalid:8081",
    "AGENTPLANE_CONNECTION_SERVICE_URL": "http://connection-service.test.invalid:8080",
    "AGENTPLANE_NOTIFICATIONS_URL": "http://notifications.test.invalid:8080",
    "AGENTPLANE_NOTIFICATIONS_TOKEN_FILE": "/test/notifications-token",
    "AGENTPLANE_ELECTRIC_URL": "http://electric.test.invalid:3000",
    "AGENTPLANE_ACTION_FEDERATION": json.dumps(
        {
            "mode": "direct",
            "service_url": "http://actions.test.invalid:8080",
            "login_jwks_uri": "https://auth.test.invalid/jwks/",
            "target": {
                "issuer": "https://auth.test.invalid/application/o/test-app/",
                "audience": "test-client",
                "jwks_uri": "https://auth.test.invalid/jwks/",
            },
            "scope": "openid profile",
        }
    ),
}
OIDC_ENVIRONMENT = {
    "AGENTPLANE_OIDC_ISSUER": "https://auth.test.invalid/application/o/test-app/",
    "AGENTPLANE_OIDC_CLIENT_ID": "test-client",
    "AGENTPLANE_OIDC_CLIENT_SECRET": "test-client-secret",  # a test literal, not a real credential
    "AGENTPLANE_OIDC_SESSION_SECRET": "test-session-secret",  # a test literal, not a real credential
    "AGENTPLANE_OIDC_PUBLIC_BASE_URL": "https://app.test.invalid",
}


def set_app_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name, value in APP_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    config = tmp_path / "config.yaml"
    config.write_text("default_policy_sets: [test-basic]\n")
    monkeypatch.setenv("AGENTPLANE_CONFIG_FILE", str(config))


def test_the_two_settings_models_read_environment_and_config_without_colliding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`AGENTPLANE_OIDC_` sits inside `AGENTPLANE_`, and the staging Deployment sets both.

    A model that claimed the other's variables, or refused to parse alongside them, would take the
    app down at rollout rather than here.
    """
    set_app_environment(monkeypatch, tmp_path)
    for name, value in OIDC_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)

    settings = Settings(_cli_parse_args=[])
    oidc = OIDCSettings()

    assert settings.sandbox_namespace == "test-sandbox-namespace"
    assert settings.runner_gateway_request_timeout_s == 20
    assert settings.runner_gateway_lifecycle_timeout_s == 310
    assert (settings.port, settings.token_audience) == (8080, "agentplane")
    assert settings.session_dispatcher_target == "session-dispatcher.test:8080"
    assert str(settings.session_dispatcher_token_file) == "/var/run/secrets/agentplane-session-dispatcher/token"
    assert oidc.redirect_uri == "https://app.test.invalid/auth/callback"
    assert (
        oidc.server_metadata_url == "https://auth.test.invalid/application/o/test-app/.well-known/openid-configuration"
    )
    # https, so the cookie takes the __Host- prefix that binds it to this exact origin.
    assert oidc.cookie_name.startswith("__Host-")


def test_rest_api_settings_do_not_require_electric(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    set_app_environment(monkeypatch, tmp_path)
    monkeypatch.delenv("AGENTPLANE_ELECTRIC_URL")
    settings = Settings(_cli_parse_args=[])
    assert settings.electric_url is None
    assert settings.frontend_config == FrontendConfig()


def test_frontend_config_loads_from_the_deployment_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    set_app_environment(monkeypatch, tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(
        "default_policy_sets: [test-basic]\n"
        "frontend_config:\n"
        "  trace_search:\n"
        "    grafana_base_url: https://grafana.test.invalid\n"
        "    tempo_datasource_uid: tempo\n"
        "    deployment_environment: agentplane-staging\n"
        "  langfuse_search:\n"
        "    base_url: https://langfuse.test.invalid\n"
        "    project_id: test-project\n"
        "    sandbox_namespace_metadata_key: attributes.agentplane.namespace\n"
        "    sandbox_service_account_metadata_key: attributes.agentplane.service_account\n"
    )
    monkeypatch.setenv("AGENTPLANE_CONFIG_FILE", str(config))

    assert Settings(_cli_parse_args=[]).frontend_config == FrontendConfig(
        trace_search=TraceSearchSettings(
            grafana_base_url=AnyHttpUrl("https://grafana.test.invalid"),
            tempo_datasource_uid="tempo",
            deployment_environment="agentplane-staging",
        ),
        langfuse_search=LangfuseSearchSettings(
            base_url=AnyHttpUrl("https://langfuse.test.invalid"),
            project_id="test-project",
            sandbox_namespace_metadata_key="attributes.agentplane.namespace",
            sandbox_service_account_metadata_key="attributes.agentplane.service_account",
        ),
    )


@pytest.mark.parametrize("default_policy_sets", [None, [], ["test-basic"]])
def test_default_policy_sets_are_required_for_direct_lifecycle(
    default_policy_sets: list[str] | None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name, value in APP_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("AGENTPLANE_DEFAULT_POLICY_SETS", raising=False)
    # This value is deployed through the YAML ConfigMap, not an environment string.
    config = tmp_path / "config.yaml"
    config.write_text(
        "" if default_policy_sets is None else f"default_policy_sets: {json.dumps(default_policy_sets)}\n"
    )
    monkeypatch.setenv("AGENTPLANE_CONFIG_FILE", str(config))

    if default_policy_sets is None:
        with pytest.raises(ValidationError, match="default_policy_sets"):
            Settings(_cli_parse_args=[])
    else:
        settings = Settings(_cli_parse_args=[])
        assert settings.default_policy_sets == set(default_policy_sets)


@pytest.mark.parametrize(
    "field",
    [
        "AGENTPLANE_EGRESS_ADMIN_URL",
        "AGENTPLANE_CONNECTION_SERVICE_URL",
        "AGENTPLANE_NOTIFICATIONS_URL",
        "AGENTPLANE_ELECTRIC_URL",
    ],
)
def test_endpoint_settings_reject_query_components(field: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    set_app_environment(monkeypatch, tmp_path)
    monkeypatch.setenv(field, "http://service.test.invalid/?tenant=one")

    with pytest.raises(ValidationError):
        Settings(_cli_parse_args=[])


SANDBOX = "shutdown-test-sandbox"


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
def bound_listener() -> Iterator[Listener]:
    with listen_tcp() as listener:
        yield listener


@pytest.fixture
def database_updates(engine: AsyncEngine) -> WakeupListener:
    # Unlike the route-only fixtures, this test runs the production owner of the listener scope.
    return create_database_updates(engine.url)


@pytest.fixture
async def database(db_url: str) -> AsyncIterator[AsyncEngine]:
    """The database as another replica sees it: who is connected."""
    engine = create_async_engine(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


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
async def test_sigterm_ends_open_streams_fails_readiness_and_closes_session_openings_and_database(
    inventory: SandboxAccess,
    runners: RunnerDirectory,
    store: ThreadStore,
    database_updates: WakeupListener,
    engine: AsyncEngine,
    operator_sessions: OperatorSessionStore,
    egress: EgressAccess,
    decisions: DecisionsClient,
    live_index: LiveIndex,
    action_policy: ActionPolicyInventory,
    reviewer: TokenReviewer,
    database: AsyncEngine,
    event_logs: SeededEventLogStore,
    content: ContentStore,
    namespace: SandboxNamespace,
    agent_auth: dict[str, str],
    notifications: NotificationsOperatorClient,
    history_client: HistoryServiceClient,
    dispatcher: SessionDispatcherClient,
    bound_listener: Listener,
) -> None:
    """A tab holding `/live/sandboxes` open used to hold Uvicorn's shutdown open with it. The stream
    now ends at the signal -- cleanly, which a stream cancelled at the budget would not -- readiness
    fails while the drain is on, and the unwind then closes every connection the app held."""
    live_index.sandboxes[SANDBOX], live_index.pods[SANDBOX] = await namespace.runner(SANDBOX)
    await event_logs.seed(SANDBOX, "shutdown-session")
    app = create_app(
        inventory,
        RunnerBridge(runners=runners, event_logs=event_logs, content=content, dispatcher=dispatcher),
        store,
        TEST_MODELS,
        egress,
        decisions,
        live_index,
        action_policy,
        reviewer=reviewer,
        event_logs=event_logs,
        content=content,
        database_updates=database_updates,
        operator_sessions=operator_sessions,
        electric=None,
        notifications=notifications,
        system_status=answering_system_status(),
    )
    # A budget the test would never wait out: the stream has to end because of the drain, not this.
    server = AppServer(uvicorn.Config(app, log_level="warning", timeout_graceful_shutdown=60), drain_of(app))
    async with history_client.new_client() as openings_client:
        serving = asyncio.create_task(
            serve_then_close(
                server,
                session_openings=SessionOpenings(openings_client, event_logs, retry_interval_s=0.05),
                database_updates=database_updates,
                engine=engine,
                sockets=[bound_listener.socket],
            )
        )
        while not server.started:
            if serving.done():
                serving.result()
                raise RuntimeError("uvicorn exited before starting")
            await asyncio.sleep(0.02)
        assert await _other_connections(database) > 0

        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{bound_listener.port}", headers=agent_auth) as client:
            assert (await client.get("/readyz")).status_code == 204
            assert (await client.get("/threads/00000000-0000-0000-0000-000000000000/sync/scope")).status_code == 404
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

    # A closed connection's backend leaves pg_stat_activity a moment after the client let go of it.
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(10), wait=wait_fixed(0.02), retry=retry_if_exception_type(AssertionError), reraise=True
    ):
        with attempt:
            assert await _other_connections(database) == 0


if __name__ == "__main__":
    pytest_bazel.main()
