"""Real PostgreSQL, migrated by the same image-owned runner used in deployment."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Generator, Iterator
from uuid import uuid4

import pytest
from google.protobuf.json_format import MessageToDict
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.postgres import PostgresContainer

# Both native harnesses, not an app-managed session or app database.
from agentplane.action_service.database_migrate import RUNNER as ACTIONS_MIGRATIONS
from agentplane.action_service.testing.fixtures import echo_catalog, echo_executor
from agentplane.notification_service.database_migrate import RUNNER
from agentplane.notification_service.store import Store
from agentplane.runner.testing.fixtures import RunnerHandle, config, endpoint, harness, model, runner, spec, workspace
from util.testing.postgres import create_database_sync, force_drop_database_sync
from util.testing.postgres_fixtures import postgres_container
from util.testing.undeclared_outputs import undeclared_outputs_dir

# gazelle:include_dep @pypi//asyncpg
# gazelle:include_dep @pypi//psycopg

logger = logging.getLogger(__name__)
_CALL_REPORT = pytest.StashKey[pytest.TestReport]()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[object]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    if call.when == "call":
        item.stash[_CALL_REPORT] = report
    return report


@pytest.fixture
async def failed_native_journal(request: pytest.FixtureRequest, runner: RunnerHandle) -> AsyncIterator[None]:
    yield
    report = request.node.stash.get(_CALL_REPORT, None)
    if report is None or not report.failed:
        return
    try:
        async with asyncio.timeout(3):
            sessions: dict[str, list[dict[str, object]]] = {}
            for session_id, session in runner.runner.sessions.items():
                entries = await session.journal.since(0, limit=512)
                sessions[session_id] = [MessageToDict(entry, preserving_proto_field_name=True) for entry in entries]
            (undeclared_outputs_dir() / f"{request.node.name}-native-journal.json").write_text(
                json.dumps(sessions, indent=2, sort_keys=True)
            )
    except Exception as error:
        logger.warning("native journal capture failed: %s", type(error).__name__)


@pytest.fixture
def db_url(postgres_container: PostgresContainer) -> Iterator[str]:
    admin = f"postgresql+psycopg://postgres:postgres@{postgres_container.get_container_host_ip()}:{postgres_container.get_exposed_port(5432)}/postgres"
    name = f"notifications_{uuid4().hex}"
    url = (
        make_url(create_database_sync(admin, name))
        .set(drivername="postgresql+asyncpg")
        .render_as_string(hide_password=False)
    )
    RUNNER.apply(url)
    yield url
    force_drop_database_sync(admin, name)


@pytest.fixture
async def engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def store(engine: AsyncEngine) -> Store:
    return Store(engine)


@pytest.fixture
async def action_engine(postgres_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    admin = f"postgresql+psycopg://postgres:postgres@{postgres_container.get_container_host_ip()}:{postgres_container.get_exposed_port(5432)}/postgres"
    name = f"actions_{uuid4().hex}"
    url = (
        make_url(create_database_sync(admin, name))
        .set(drivername="postgresql+asyncpg")
        .render_as_string(hide_password=False)
    )
    ACTIONS_MIGRATIONS.apply(url)
    engine = create_async_engine(url)
    try:
        yield engine
    finally:
        await engine.dispose()
        force_drop_database_sync(admin, name)
