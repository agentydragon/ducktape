"""The app's collaborators in its tests: a database of the test's own, the Go History Service, and a
namespace of the test's own on the target's Kubernetes API server."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import grpc
import httpx
import pytest
from kubernetes_asyncio.client import AuthenticationV1Api
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.action_service import action_service_pb2_grpc
from agentplane.app.action_policy import ActionPolicyInventory
from agentplane.app.database import connect
from agentplane.app.database_migrate import RUNNER
from agentplane.app.database_updates import Channel, create_database_updates
from agentplane.app.egress_access import EgressAccess
from agentplane.app.electric import ElectricProxy
from agentplane.app.grpc_stream import OperatorGrpcClient
from agentplane.app.identity import TokenReviewer
from agentplane.app.live import LiveIndex
from agentplane.app.notifications_operator_client import NotificationsOperatorClient
from agentplane.app.operator_sessions import BrowserSession, OperatorSession, OperatorSessionStore, SessionRow
from agentplane.app.sandbox_access import SandboxAccess
from agentplane.app.testing.dispatcher import served_dispatcher
from agentplane.app.testing.egress_proxy import FakeEgressAdmin
from agentplane.app.testing.fake_action_service import FakeActionService, served_action_service
from agentplane.app.testing.history import SeededEventLogStore
from agentplane.app.testing.notifications_operator import FakeNotificationsOperator
from agentplane.app.testing.runner_gateway import (
    app_token_file,
    dispatcher_database_url,
    gateway_service,
    runner,
    sandbox,
    sandbox_control,
    session_dispatcher,
)
from agentplane.app.testing.thread_test_support import SPEC, Replica, event_entry
from agentplane.app.threads.bridge import RunnerBridge
from agentplane.app.threads.dispatcher_client import SessionDispatcherClient
from agentplane.app.threads.history_watch import HistoryWatch
from agentplane.app.threads.runner_directory import RunnerDirectory
from agentplane.app.threads.store import ThreadStore
from agentplane.app.threads.summaries import Summaries
from agentplane.app.threads.view.content import ContentStore
from agentplane.egress import egress_admin_pb2_grpc
from agentplane.egress.decisions_client import DecisionsClient
from agentplane.grpc.calls import dial
from agentplane.grpc.serving import serving
from agentplane.history_service.client import HistoryServiceClient
from agentplane.history_service.testing.fixtures import (
    history_on,
    history_service,
    service_database_template,
    service_history_db_url,
)
from agentplane.history_service.testing.go_service import WRITER, GoHistoryService
from agentplane.history_service.testing.history import History
from agentplane.kubernetes_watch import Freshness
from agentplane.notification_service import notifications_operator_pb2_grpc
from agentplane.postgres.wakeups import WakeupListener
from agentplane.runner.testing.failure_journal import pytest_runtest_makereport

# The per-test database is created over psycopg, which SQLAlchemy loads from the URL scheme.
# gazelle:include_dep @pypi//psycopg
# Generated gRPC messages need their protobuf stubs as a direct mypy dependency.
# gazelle:include_dep @pypi//protobuf
# The bridge tests run one script against a local runner over both harnesses; those fixtures live
# with the runner.
from agentplane.runner.testing.fixtures import config, endpoint, harness, model, runner_gateway, spec, workspace
from agentplane.runner_gateway.client import SandboxControlClient
from agentplane.runner_gateway.dispatcher import session_dispatcher_pb2_grpc
from agentplane.sandboxes.inventory import SandboxInventory
from agentplane.sandboxes.testing.kubernetes import SandboxCluster
from agentplane.sandboxes.testing.sandbox_namespace import SandboxNamespace
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.electric_role import ensure_electric_role
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer
from agentplane.testing.kubernetes_apiserver_fixtures import api_server, kubeconfig
from util.net import refusing_tcp_endpoint
from util.testing.postgres import DatabaseClones, PostgresDatabaseTemplate, migrated_database_template
from util.testing.postgres_fixtures import (
    database_clones,
    database_names,
    postgres_admin_url,
    postgres_container,
    postgres_image,
)
from util.testing.sqlalchemy_engines import managed_async_engine


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
def register_attached_sessions() -> bool:
    return False


@pytest.fixture
def history_writer_namespaces(sandbox: SandboxCluster, namespace: SandboxNamespace) -> tuple[str, ...]:
    """Allow real runners and scripted writers for this test's Sandbox fixtures."""
    return (sandbox.namespace, namespace.name, WRITER.namespace)


@pytest.fixture
async def engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    async with managed_async_engine(connect(db_url)) as engine:
        yield engine


@pytest.fixture
async def history(history_service: GoHistoryService) -> AsyncIterator[History]:
    async with History(history_service) as history:
        yield history


@pytest.fixture
def history_client(history_service: GoHistoryService) -> HistoryServiceClient:
    return history_service.reader


@pytest.fixture
def summaries(history_client: HistoryServiceClient) -> Summaries:
    return Summaries(history_client)


@pytest.fixture
def store(engine: AsyncEngine, summaries: Summaries) -> ThreadStore:
    return ThreadStore(engine, summaries)


@pytest.fixture
def event_logs(
    engine: AsyncEngine,
    history: History,
    history_client: HistoryServiceClient,
    summaries: Summaries,
    register_attached_sessions: bool,
    namespace: SandboxNamespace,
    sandbox: SandboxCluster,
) -> SeededEventLogStore:
    return SeededEventLogStore(
        engine,
        history=None if register_attached_sessions else history,
        history_reader=history_client,
        summaries=summaries,
        sandbox_namespaces={sandbox.name: sandbox.namespace, "live": namespace.name},
    )


@pytest.fixture
def content(engine: AsyncEngine, history_client: HistoryServiceClient) -> ContentStore:
    return ContentStore(engine, history_reader=history_client)


@asynccontextmanager
async def listening(engine: AsyncEngine, history_client: HistoryServiceClient) -> AsyncIterator[WakeupListener]:
    """A replica's notifications, woken by the History Service's fold commits as `main.py` wires them."""
    updates = create_database_updates(engine.url)
    async with history_client.new_client() as watch_client, updates.listener.listen():
        watch = HistoryWatch(watch_client, updates.changes[Channel.THREADS].notify, retry_interval_s=0.05)
        task = asyncio.create_task(watch.run())
        try:
            yield updates
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.fixture
async def database_updates(engine: AsyncEngine, history_client: HistoryServiceClient) -> AsyncIterator[WakeupListener]:
    async with listening(engine, history_client) as updates:
        yield updates


@pytest.fixture
async def replica(
    db_url: str, event_logs: SeededEventLogStore, history_client: HistoryServiceClient
) -> AsyncIterator[Replica]:
    async with managed_async_engine(connect(db_url)) as engine:
        summaries = Summaries(history_client)
        yield Replica(
            ThreadStore(engine, summaries),
            SeededEventLogStore(
                engine,
                history=event_logs.history,
                history_reader=history_client,
                summaries=summaries,
                sandbox_namespaces=event_logs.sandbox_namespaces,
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


@pytest.fixture
async def namespace(api_server: KubernetesApiServer) -> SandboxNamespace:
    return await SandboxNamespace.new(api_server)


@pytest.fixture
def runners(live_index: LiveIndex, sandbox_control: SandboxControlClient) -> RunnerDirectory:
    return RunnerDirectory(live_index, sandbox_control)


@pytest.fixture
async def dispatcher(tmp_path: Path) -> AsyncIterator[SessionDispatcherClient]:
    """A Dispatcher implementing nothing, for app tests that never reach one."""
    async with served_dispatcher(
        session_dispatcher_pb2_grpc.SessionDispatcherServicer(), tmp_path / "dispatcher-token", request_timeout_s=5
    ) as client:
        yield client


@pytest.fixture
async def action_service() -> AsyncIterator[
    tuple[FakeActionService, action_service_pb2_grpc.ActionServiceStub, OperatorGrpcClient]
]:
    async with served_action_service(FakeActionService()) as service:
        yield service


@pytest.fixture
def bridge(
    runners: RunnerDirectory,
    dispatcher: SessionDispatcherClient,
    event_logs: SeededEventLogStore,
    content: ContentStore,
    database_updates: WakeupListener,
) -> RunnerBridge:
    return RunnerBridge(runners=runners, event_logs=event_logs, content=content, dispatcher=dispatcher)


@pytest.fixture
def inventory(namespace: SandboxNamespace) -> SandboxAccess:
    return SandboxAccess(
        SandboxInventory(namespace=namespace.name, custom_objects=namespace.custom_objects, core_v1=namespace.core_v1),
        default_policy_sets=frozenset(),
        caller=namespace.account("app"),
    )


# The agent's credential, and what a test client carries: the app's other one is an OIDC session
# only an authorization-code round trip produces (`test_auth_routes.py`).
AUDIENCE = "agentplane-test"
_AGENT = "test-agent"
# A second ServiceAccount, whose tokens are every bit as valid as the agent's and which the app
# accepts nothing from: what naming the subjects it does accept is for.
_STRANGER = "test-stranger"


async def _bearer(api_server: KubernetesApiServer, account: ServiceAccountRef) -> dict[str, str]:
    """Headers carrying a Pod-bound token of `account`'s for the app's audience, which TokenReview accepts."""
    return {"Authorization": f"Bearer {await api_server.workload_token(account, [AUDIENCE])}"}


@pytest.fixture
def agent(namespace: SandboxNamespace) -> str:
    """The username the agent's tokens review as, which the app is told to accept."""
    return f"system:serviceaccount:{namespace.name}:{_AGENT}"


@pytest.fixture
async def agent_auth(api_server: KubernetesApiServer, namespace: SandboxNamespace) -> dict[str, str]:
    return await _bearer(api_server, namespace.account(_AGENT))


@pytest.fixture
async def stranger_auth(api_server: KubernetesApiServer, namespace: SandboxNamespace) -> dict[str, str]:
    return await _bearer(api_server, namespace.account(_STRANGER))


@pytest.fixture
def reviewer(api_server: KubernetesApiServer, agent: str) -> TokenReviewer:
    return TokenReviewer(AuthenticationV1Api(api_server.api), audience=AUDIENCE, subjects=(agent,))


@pytest.fixture
def live_index(namespace: SandboxNamespace) -> LiveIndex:
    """An index nothing is watching: the fixtures that need one drive it themselves."""
    return LiveIndex(freshness=Freshness(stale_after_seconds=90), core_v1=namespace.core_v1)


@pytest.fixture
def egress(namespace: SandboxNamespace, action_policy: ActionPolicyInventory) -> EgressAccess:
    return EgressAccess(namespace=namespace.name, custom_objects=namespace.custom_objects, action_policy=action_policy)


@pytest.fixture
def action_policy(namespace: SandboxNamespace) -> ActionPolicyInventory:
    return ActionPolicyInventory(namespace=namespace.name, custom_objects=namespace.custom_objects)


@pytest.fixture
async def _egress_admin_server() -> AsyncIterator[tuple[FakeEgressAdmin, str]]:
    admin = FakeEgressAdmin()
    server = grpc.aio.server()
    egress_admin_pb2_grpc.add_EgressAdminServicer_to_server(admin, server)
    async with serving(server, "127.0.0.1:0", grace_s=0) as port:
        yield admin, f"127.0.0.1:{port}"


@pytest.fixture
def egress_admin(_egress_admin_server: tuple[FakeEgressAdmin, str]) -> FakeEgressAdmin:
    return _egress_admin_server[0]


@pytest.fixture
def egress_token_file(tmp_path: Path) -> Path:
    token_file = tmp_path / "egress-token"
    token_file.write_text("fake-token")
    return token_file


@pytest.fixture
async def decisions(
    _egress_admin_server: tuple[FakeEgressAdmin, str], egress_token_file: Path
) -> AsyncIterator[DecisionsClient]:
    async with dial(_egress_admin_server[1]) as channel:
        yield DecisionsClient(channel, token_file=egress_token_file, request_timeout_s=5)


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
async def _notifications_operator_server() -> AsyncIterator[tuple[FakeNotificationsOperator, str]]:
    operator = FakeNotificationsOperator()
    server = grpc.aio.server()
    notifications_operator_pb2_grpc.add_NotificationsOperatorServicer_to_server(operator, server)
    async with serving(server, "127.0.0.1:0", grace_s=0) as port:
        yield operator, f"127.0.0.1:{port}"


@pytest.fixture
def notifications_operator(
    _notifications_operator_server: tuple[FakeNotificationsOperator, str],
) -> FakeNotificationsOperator:
    return _notifications_operator_server[0]


@pytest.fixture
async def notifications_channel(
    _notifications_operator_server: tuple[FakeNotificationsOperator, str],
) -> AsyncIterator[grpc.aio.Channel]:
    async with dial(_notifications_operator_server[1]) as channel:
        yield channel


@pytest.fixture
def notifications(
    notifications_channel: grpc.aio.Channel, notifications_token_file: Path
) -> NotificationsOperatorClient:
    return NotificationsOperatorClient(notifications_channel, token_file=notifications_token_file, request_timeout_s=5)


@pytest.fixture
def notifications_token_file(tmp_path: Path) -> Path:
    token_file = tmp_path / "notifications-token"
    token_file.write_text("fake-notifications-token")
    return token_file


@pytest.fixture
def refused_address() -> Iterator[str]:
    """A loopback endpoint held bound without listening for tests that expect a connection refusal."""
    with refusing_tcp_endpoint() as (host, port):
        yield f"{host}:{port}"
