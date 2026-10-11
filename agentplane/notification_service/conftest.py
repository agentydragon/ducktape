"""Real PostgreSQL, migrated by the same image-owned runner used in deployment."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Generator

import pytest
from google.protobuf.json_format import MessageToDict
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

# Both native harnesses, not an app-managed session or app database.
from agentplane.action_service.testing.fixtures import action_database_template, echo_catalog, echo_executor
from agentplane.notification_service.database_migrate import RUNNER
from agentplane.notification_service.store import Store
from agentplane.runner.testing.fixtures import RunnerHandle, config, endpoint, harness, model, runner, spec, workspace
from agentplane.sandbox_service.testing.fixtures import service_database_template
from util.testing.postgres import DatabaseClones, PostgresDatabaseTemplate
from util.testing.postgres_fixtures import (
    database_clones,
    database_names,
    migrated_database_fixtures,
    postgres_admin_url,
    postgres_container,
    postgres_image,
)
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


database_template, db_url = migrated_database_fixtures(RUNNER, drivername="postgresql+asyncpg")


@pytest.fixture
def sandbox_service_db_url(service_database_template: PostgresDatabaseTemplate, database_clones: DatabaseClones) -> str:
    return database_clones.clone(service_database_template, drivername="postgresql+asyncpg")


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
async def action_engine(
    action_database_template: PostgresDatabaseTemplate, database_clones: DatabaseClones
) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(database_clones.clone(action_database_template, drivername="postgresql+asyncpg"))
    try:
        yield engine
    finally:
        await engine.dispose()
