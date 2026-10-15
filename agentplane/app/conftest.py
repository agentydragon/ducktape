"""Fixtures over the fake Kubernetes in `testing/kubernetes.py`."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import grpc
import httpx
import pytest
from kubernetes_asyncio.client import CoreV1Api
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.app.action_policy import ActionPolicyInventory
from agentplane.app.database import connect
from agentplane.app.database_migrate import RUNNER
from agentplane.app.database_updates import Channel, create_database_updates
from agentplane.app.egress_access import EgressAccess
from agentplane.app.electric import ElectricProxy
from agentplane.app.identity import TokenReviewer
from agentplane.app.live import LiveIndex
from agentplane.app.operator_sessions import BrowserSession, OperatorSession, OperatorSessionStore, SessionRow
from agentplane.app.sandbox_access import SandboxAccess
from agentplane.app.testing.egress_proxy import FakeEgressAdmin
from agentplane.app.testing.history import SeededEventLogStore
from agentplane.app.testing.replication_source import Placements
from agentplane.app.testing.thread_test_support import SPEC, Replica, event_entry
from agentplane.app.threads.bridge import RunnerBridge
from agentplane.app.threads.history_watch import HistoryWatch
from agentplane.app.threads.runner_directory import RunnerDirectory
from agentplane.app.threads.store import ThreadStore
from agentplane.app.threads.summaries import Summaries
from agentplane.app.threads.view.content import ContentStore
from agentplane.egress.decisions_client import DecisionsClient
from agentplane.grpc_calls import dial
from agentplane.history_service.client import HistoryServiceClient
from agentplane.history_service.testing.backend import history_service
from agentplane.history_service.testing.fixtures import service_database_template
from agentplane.history_service.testing.history import History
from agentplane.kubernetes_watch import Freshness
from agentplane.postgres.wakeups import WakeupListener
from agentplane.runner.testing.failure_journal import pytest_runtest_makereport

# The per-test database is created over psycopg, which SQLAlchemy loads from the URL scheme.
# gazelle:include_dep @pypi//psycopg
# The bridge tests run one script against a local runner over both harnesses; those fixtures live
# with the runner.
from agentplane.runner.testing.fixtures import config, endpoint, harness, model, runner, spec, workspace
from agentplane.runner_gateway.client import RunnerGatewayClient
from agentplane.runner_gateway.testing.backend import Endpoint, backend
from agentplane.sandboxes.inventory import SandboxInventory
from agentplane.sandboxes.testing.fake_inventory import (
    NAMESPACE,
    TEMPLATE,
    FakeAuthenticationV1Api,
    FakeCoreV1Api,
    FakeCustomObjectsApi,
    close_fake_api_clients,
)
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.electric_role import ensure_electric_role
from util.testing.postgres import DatabaseClones, PostgresDatabaseTemplate, migrated_database_template
from util.testing.postgres_fixtures import (
    database_clones,
    database_names,
    postgres_admin_url,
    postgres_container,
    postgres_image,
)
from util.testing.sqlalchemy_engines import managed_async_engine


@pytest.fixture(autouse=True)
async def close_fake_clients() -> AsyncIterator[None]:
    yield
    await close_fake_api_clients()


@pytest.fixture(scope="session")
def app_database_template(postgres_admin_url: str) -> Iterator[PostgresDatabaseTemplate]:
    """The app schema migrated once; each default `db_url` is a fresh clone of this template."""
    ensure_electric_role(postgres_admin_url)
    with migrated_database_template(postgres_admin_url, "agentplane_app_test_template", RUNNER) as template:
        yield template


@pytest.fixture
def db_url(app_database_template: PostgresDatabaseTemplate, database_clones: DatabaseClones) -> str:
    return database_clones.clone(app_database_template, drivername="postgresql+asyncpg")


@pytest.fixture
def service_history_db_url(service_database_template: PostgresDatabaseTemplate, database_clones: DatabaseClones) -> str:
    return database_clones.clone(service_database_template, drivername="postgresql+asyncpg")


@pytest.fixture
def register_attached_sessions() -> bool:
    return False


@pytest.fixture
async def engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    async with managed_async_engine(connect(db_url)) as engine:
        yield engine


@pytest.fixture
async def history(service_history_db_url: str) -> AsyncIterator[History]:
    async with History(service_history_db_url) as history, history.folding():
        yield history


@pytest.fixture
def history_token_file(tmp_path: Path) -> Path:
    return tmp_path / "history-service-token"


@pytest.fixture
async def history_client(service_history_db_url: str, history_token_file: Path) -> AsyncIterator[HistoryServiceClient]:
    async with history_service(service_history_db_url, history_token_file) as client:
        yield client


@pytest.fixture
def summaries(history_client: HistoryServiceClient) -> Summaries:
    return Summaries(history_client)


@pytest.fixture
def store(engine: AsyncEngine, summaries: Summaries, gateway: RunnerGatewayClient) -> ThreadStore:
    return ThreadStore(engine, summaries)


@pytest.fixture
def gateway_registry(history: History) -> AsyncEngine | Placements:
    """Where fixture Sessions are registered with the Runner Gateway: the service database here, which
    the gateway shares; browser tests answer from a fake gateway's map instead."""
    return history.engine


@pytest.fixture
def event_logs(
    engine: AsyncEngine,
    history: History,
    history_client: HistoryServiceClient,
    summaries: Summaries,
    register_attached_sessions: bool,
    gateway_registry: AsyncEngine | Placements,
) -> SeededEventLogStore:
    return SeededEventLogStore(
        engine,
        history=None if register_attached_sessions else history,
        gateway=gateway_registry,
        history_reader=history_client,
        summaries=summaries,
    )


@pytest.fixture
def content(engine: AsyncEngine, history_client: HistoryServiceClient) -> ContentStore:
    return ContentStore(engine, history_reader=history_client)


@asynccontextmanager
async def listening(
    engine: AsyncEngine, history_client: HistoryServiceClient, history_token_file: Path
) -> AsyncIterator[WakeupListener]:
    """A replica's notifications, woken by the History Service's fold commits as `main.py` wires them."""
    updates = create_database_updates(engine.url)
    async with (
        HistoryServiceClient(
            history_client.target, token_file=history_token_file, request_timeout_s=20
        ) as watch_client,
        updates.listener.listen(),
    ):
        watch = HistoryWatch(watch_client, updates.changes[Channel.THREADS].notify, retry_interval_s=0.05)
        task = asyncio.create_task(watch.run())
        try:
            yield updates
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.fixture
async def database_updates(
    engine: AsyncEngine, history_client: HistoryServiceClient, history_token_file: Path
) -> AsyncIterator[WakeupListener]:
    async with listening(engine, history_client, history_token_file) as updates:
        yield updates


@pytest.fixture
async def replica(
    db_url: str, event_logs: SeededEventLogStore, history_client: HistoryServiceClient, gateway: RunnerGatewayClient
) -> AsyncIterator[Replica]:
    async with managed_async_engine(connect(db_url)) as engine:
        summaries = Summaries(history_client)
        yield Replica(
            ThreadStore(engine, summaries),
            SeededEventLogStore(
                engine,
                history=event_logs.history,
                gateway=event_logs.gateway,
                history_reader=history_client,
                summaries=summaries,
            ),
            OperatorSessionStore(engine),
        )


@pytest.fixture
def operator_sessions(engine: AsyncEngine) -> OperatorSessionStore:
    return OperatorSessionStore(engine)


async def stored_login(store: OperatorSessionStore, login: OperatorSession) -> SessionRow:
    """`login` in a session row, as a callback leaves one, reached as a request reaches it."""
    now = datetime.now(UTC)
    row = BrowserSession(
        id=secrets.token_hex(32),
        expires_at=now + timedelta(hours=1),
        absolute_expires_at=now + timedelta(days=1),
        payload={},
    )
    row.login = login
    async with store.sessions.begin() as db:
        db.add(row)
    return SessionRow(store, row.id, idle=timedelta(hours=1), step=timedelta(minutes=5))


OPERATOR_TOKEN = "test-operator-token"  # a test literal, not a real credential


async def operator_token() -> str:
    """The bearer token provider an `OperatorActionServiceClient` under test is given."""
    return OPERATOR_TOKEN


@pytest.fixture
def custom_objects() -> FakeCustomObjectsApi:
    return FakeCustomObjectsApi()


@pytest.fixture
def core_v1() -> FakeCoreV1Api:
    return FakeCoreV1Api()


@pytest.fixture
def runners(live_index: LiveIndex, gateway: RunnerGatewayClient) -> RunnerDirectory:
    """The runners `live_index` shows, dialled on a port nothing listens on."""
    return RunnerDirectory(live_index, gateway)


@pytest.fixture
def bridge(
    runners: RunnerDirectory, event_logs: SeededEventLogStore, content: ContentStore, database_updates: WakeupListener
) -> RunnerBridge:
    return RunnerBridge(runners=runners, event_logs=event_logs, content=content)


@pytest.fixture
def gateway_endpoint(
    custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api, tmp_path: Path, service_history_db_url: str
) -> Iterator[Endpoint]:
    with backend(custom_objects, core_v1, tmp_path / "sandbox-token", database_url=service_history_db_url) as endpoint:
        yield endpoint


@pytest.fixture
async def gateway_channel(gateway_endpoint: Endpoint) -> AsyncIterator[grpc.aio.Channel]:
    async with dial(gateway_endpoint.target) as channel:
        yield channel


@pytest.fixture
def inventory(custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api) -> SandboxAccess:
    return SandboxAccess(
        SandboxInventory(
            namespace=NAMESPACE, custom_objects=cast(Any, custom_objects), core_v1=cast(CoreV1Api, core_v1)
        ),
        default_policy_sets=frozenset(),
        caller=ServiceAccountRef(namespace=NAMESPACE, name="agentplane-app"),
    )


@pytest.fixture
def gateway(gateway_endpoint: Endpoint, gateway_channel: grpc.aio.Channel) -> RunnerGatewayClient:
    return gateway_endpoint.gateway(gateway_channel)


# The agent's credential, and what a test client carries: the app's other one is an OIDC session
# only an authorization-code round trip produces (`test_auth_routes.py`).
AUDIENCE = "agentplane-test"
AGENT = f"system:serviceaccount:{NAMESPACE}:test-agent"
AGENT_TOKEN = "test-agent-token"  # a test literal, not a real credential
AGENT_AUTH = {"Authorization": f"Bearer {AGENT_TOKEN}"}
# A second ServiceAccount, whose tokens are every bit as valid as the agent's and which the app
# accepts nothing from: what naming the subjects it does accept is for.
STRANGER = f"system:serviceaccount:{NAMESPACE}:test-stranger"
STRANGER_TOKEN = "test-stranger-token"  # a test literal, not a real credential
STRANGER_AUTH = {"Authorization": f"Bearer {STRANGER_TOKEN}"}


@pytest.fixture
def authentication() -> FakeAuthenticationV1Api:
    api = FakeAuthenticationV1Api()
    api.issue(AGENT_TOKEN, username=AGENT, audiences=[AUDIENCE])
    api.issue(STRANGER_TOKEN, username=STRANGER, audiences=[AUDIENCE])
    return api


@pytest.fixture
def reviewer(authentication: FakeAuthenticationV1Api) -> TokenReviewer:
    return TokenReviewer(cast(Any, authentication), audience=AUDIENCE, subjects=(AGENT,))


@pytest.fixture
def live_index(core_v1: FakeCoreV1Api) -> LiveIndex:
    """An index nothing is watching: the fixtures that need one drive it themselves."""
    return LiveIndex(freshness=Freshness(stale_after_seconds=90), core_v1=cast(CoreV1Api, core_v1))


@pytest.fixture
def egress(custom_objects: FakeCustomObjectsApi, action_policy: ActionPolicyInventory) -> EgressAccess:
    return EgressAccess(namespace=NAMESPACE, custom_objects=cast(Any, custom_objects), action_policy=action_policy)


@pytest.fixture
def action_policy(custom_objects: FakeCustomObjectsApi) -> ActionPolicyInventory:
    return ActionPolicyInventory(namespace=NAMESPACE, custom_objects=cast(Any, custom_objects))


@pytest.fixture
def egress_admin() -> FakeEgressAdmin:
    return FakeEgressAdmin()


@pytest.fixture
def decisions(egress_admin: FakeEgressAdmin) -> DecisionsClient:
    return DecisionsClient(httpx.AsyncClient(base_url="http://egress-admin.test", transport=egress_admin.transport()))


def _refusing(service: str) -> httpx.MockTransport:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"this test calls no {service}, yet requested {request.url}")

    return httpx.MockTransport(refuse)


@pytest.fixture
async def electric_http() -> AsyncIterator[httpx.AsyncClient]:
    """Electric for a test that syncs no thread; the sync tests run a real one."""
    async with httpx.AsyncClient(base_url="http://electric.test", transport=_refusing("Electric")) as client:
        yield client


@pytest.fixture
def electric_proxy(
    electric_http: httpx.AsyncClient,
    content: ContentStore,
    event_logs: SeededEventLogStore,
    database_updates: WakeupListener,
) -> ElectricProxy:
    return ElectricProxy(
        electric_http, content, event_logs=event_logs, thread_changes=database_updates.changes[Channel.THREADS]
    )


@pytest.fixture
async def notifications_http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url="http://notifications.test", transport=_refusing("Notification Service")
    ) as client:
        yield client


@pytest.fixture
def notifications_token_file(tmp_path: Path) -> Path:
    """Read only on a call into `notifications_http`, which these tests never make."""
    return tmp_path / "notifications-token"
