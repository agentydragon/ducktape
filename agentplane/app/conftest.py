"""Fixtures over the fake Kubernetes in `testing/kubernetes.py`."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Generator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.postgres import PostgresContainer

from agentplane.app.action_policy import ActionPolicyInventory
from agentplane.app.agent_runtime.ingestion import Ingester, Ingestion
from agentplane.app.agent_runtime.thread.store import ThreadStore
from agentplane.app.agent_runtime.updates import ThreadUpdates
from agentplane.app.bridge import RunnerBridge
from agentplane.app.database import connect
from agentplane.app.database_migrate import RUNNER
from agentplane.app.decisions import DecisionsClient
from agentplane.app.egress import EgressInventory
from agentplane.app.identity import TokenReviewer
from agentplane.app.inventory import SandboxInventory
from agentplane.app.live import LiveIndex
from agentplane.app.operator_sessions import OperatorSessionStore
from agentplane.app.runners import Runners
from agentplane.app.testing.egress_proxy import FakeEgressAdmin
from agentplane.app.testing.kubernetes import (
    NAMESPACE,
    TEMPLATE,
    FakeAuthenticationV1Api,
    FakeCoreV1Api,
    FakeCustomObjectsApi,
)
from agentplane.app.thread.content import ContentStore
from agentplane.app.thread.event_log import EventLogStore
from agentplane.app.thread.ingestion_lease import IngestionLease
from agentplane.protocol import event_log_pb2, event_pb2
from agentplane.runner import protocol_pb2

# The per-test database is created over psycopg, which SQLAlchemy loads from the URL scheme.
# gazelle:include_dep @pypi//psycopg
# The bridge tests run one script against a local runner over both harnesses; those fixtures live
# with the runner.
from agentplane.runner.conftest import config, endpoint, harness, model, runner, spec, workspace
from util.testing.postgres import create_database_sync, force_drop_database_sync
from util.testing.postgres_fixtures import postgres_container


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[object]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Expose the call report to a fixture's teardown without changing test outcomes."""
    report = yield
    if call.when == "call":
        item.stash[_CALL_REPORT] = report
    return report


_CALL_REPORT = pytest.StashKey[pytest.TestReport]()


def migrated_database(postgres_container: PostgresContainer, name: str) -> Iterator[str]:
    """A pristine, migrated database named `name` on the shared container, as an asyncpg URL.

    Not a fixture: a module whose cases only read can override `db_url` at module scope and pay
    the creation and migration once, which is most of what a database costs here.
    """
    admin_url = (
        f"postgresql+psycopg://postgres:postgres@{postgres_container.get_container_host_ip()}"
        f":{postgres_container.get_exposed_port(5432)}/postgres"
    )
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as connection:
            connection.execute(
                text(
                    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'electric') "
                    "THEN CREATE ROLE electric LOGIN REPLICATION PASSWORD 'electric'; END IF; END $$"
                )
            )
    finally:
        admin_engine.dispose()
    db_name = re.sub(r"[^a-z0-9_]", "_", name.lower())[:45].rstrip("_")
    url = create_database_sync(admin_url, db_name)
    async_url = make_url(url).set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)
    RUNNER.apply(async_url)
    yield async_url
    force_drop_database_sync(admin_url, db_name)


@pytest.fixture
def db_url(postgres_container: PostgresContainer, request: pytest.FixtureRequest) -> Iterator[str]:
    yield from migrated_database(postgres_container, request.node.name)


@pytest.fixture
async def engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = connect(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def store(engine: AsyncEngine) -> ThreadStore:
    return ThreadStore(engine)


@pytest.fixture
def event_logs(engine: AsyncEngine) -> EventLogStore:
    return EventLogStore(engine)


@pytest.fixture
def content(engine: AsyncEngine) -> ContentStore:
    return ContentStore(engine)


@pytest.fixture
def ingestion(engine: AsyncEngine) -> Ingestion:
    return Ingestion(engine)


@pytest.fixture
async def thread_updates(engine: AsyncEngine) -> AsyncIterator[ThreadUpdates]:
    updates = ThreadUpdates(engine.url)
    await updates.start()
    try:
        yield updates
    finally:
        await updates.close()


@dataclass(frozen=True)
class Replica:
    """Another app replica's stores, over its own connection pool on the same database."""

    store: ThreadStore
    event_logs: EventLogStore
    ingestion: Ingestion


@pytest.fixture
async def replica(db_url: str) -> AsyncIterator[Replica]:
    engine = connect(db_url)
    try:
        yield Replica(ThreadStore(engine), EventLogStore(engine), Ingestion(engine))
    finally:
        await engine.dispose()


@pytest.fixture
def operator_sessions(engine: AsyncEngine) -> OperatorSessionStore:
    return OperatorSessionStore(engine)


SPEC = protocol_pb2.SessionSpec(
    harness=protocol_pb2.HARNESS_CLAUDE, cwd="/state/work", model="test-model", reasoning_effort="low"
)


@pytest.fixture
async def lease(ingestion: Ingestion) -> IngestionLease:
    lease = await ingestion.acquire("sb-1", timedelta(minutes=1))
    assert lease is not None
    return lease


def event_entry(cursor: int, **observation: object) -> event_log_pb2.EventEntry:
    """One runner event at `cursor`, timestamped from it so a thread's order is its cursor order."""
    at = Timestamp()
    at.FromDatetime(datetime(2026, 9, 2, 12, 0, tzinfo=UTC) + timedelta(seconds=cursor))
    event = event_pb2.Event(at=at, **observation)  # type: ignore[arg-type]
    return event_log_pb2.EventEntry(
        cursor=cursor, origin=event_log_pb2.EventOrigin(source_id="test-runner", sequence=cursor), event=event
    )


@pytest.fixture
def custom_objects() -> FakeCustomObjectsApi:
    return FakeCustomObjectsApi()


@pytest.fixture
def core_v1() -> FakeCoreV1Api:
    return FakeCoreV1Api()


@pytest.fixture
async def runners(live_index: LiveIndex) -> AsyncIterator[Runners]:
    """The runners `live_index` shows, dialled on a port nothing listens on."""
    runners = Runners(live_index, port=1)
    yield runners
    await runners.close()


@pytest.fixture
async def ingester(runners: Runners, event_logs: EventLogStore, ingestion: Ingestion) -> AsyncIterator[Ingester]:
    ingester = Ingester(runners=runners, event_logs=event_logs, ingestion=ingestion)
    yield ingester
    await ingester.close()


@pytest.fixture
def bridge(
    runners: Runners,
    event_logs: EventLogStore,
    content: ContentStore,
    ingester: Ingester,
    thread_updates: ThreadUpdates,
) -> RunnerBridge:
    return RunnerBridge(
        runners=runners,
        event_logs=event_logs,
        content=content,
        ingester=ingester,
        thread_changes=thread_updates.changes,
    )


@pytest.fixture
def inventory(custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api) -> SandboxInventory:
    return SandboxInventory(namespace=NAMESPACE, custom_objects=cast(Any, custom_objects), core_v1=cast(Any, core_v1))


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
def live_index() -> LiveIndex:
    """An index nothing is watching: the fixtures that need one drive it themselves."""
    return LiveIndex(stale_after_seconds=90)


@pytest.fixture
def default_policies() -> list[str]:
    """What the deployment grants every sandbox; overridden by the tests about that."""
    return []


@pytest.fixture
def egress(custom_objects: FakeCustomObjectsApi, default_policies: list[str]) -> EgressInventory:
    return EgressInventory(
        namespace=NAMESPACE, custom_objects=cast(Any, custom_objects), default_policies=default_policies
    )


@pytest.fixture
def action_policy(custom_objects: FakeCustomObjectsApi) -> ActionPolicyInventory:
    return ActionPolicyInventory(namespace=NAMESPACE, custom_objects=cast(Any, custom_objects))


@pytest.fixture
def egress_admin() -> FakeEgressAdmin:
    return FakeEgressAdmin()


@pytest.fixture
def decisions(egress_admin: FakeEgressAdmin) -> DecisionsClient:
    return DecisionsClient(httpx.AsyncClient(base_url="http://egress-admin.test", transport=egress_admin.transport()))
