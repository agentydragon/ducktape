"""Real PostgreSQL, migrated by the same image-owned runner used in deployment."""

from collections.abc import AsyncIterator, Generator, Iterator
from uuid import uuid4

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.postgres import PostgresContainer

# Both native harnesses, not an app-managed session or app database.
from agentplane.action_service.database_migrate import RUNNER as ACTIONS_MIGRATIONS
from agentplane.action_service.testing.fixtures import echo_catalog, echo_executor
from agentplane.notification_service.database_migrate import RUNNER
from agentplane.notification_service.store import Store
from agentplane.runner.testing.diagnostics import (
    AdmissionTrace,
    best_effort_write_admission_diagnostics,
    native_journal_evidence,
    service_log_evidence,
)
from agentplane.runner.testing.fixtures import RunnerHandle, config, endpoint, harness, model, runner, spec, workspace
from util.testing.postgres import create_database_sync, force_drop_database_sync
from util.testing.postgres_fixtures import postgres_container
from util.testing.undeclared_outputs import undeclared_outputs_dir

# gazelle:include_dep @pypi//asyncpg
# gazelle:include_dep @pypi//psycopg


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
async def admission_diagnostics(
    request: pytest.FixtureRequest, runner: RunnerHandle, caplog: pytest.LogCaptureFixture
) -> AsyncIterator[AdmissionTrace]:
    trace = AdmissionTrace()
    yield trace
    report = request.node.stash.get(_CALL_REPORT, None)
    if report is None or not report.failed:
        return
    native_sessions: list[dict[str, object]] = []
    capture_errors: list[str] = []
    try:
        native_sessions = await native_journal_evidence(runner.runner)
    except Exception as error:
        capture_errors.append(f"native_journal:{type(error).__name__}")
    best_effort_write_admission_diagnostics(
        undeclared_outputs_dir(),
        nodeid=request.node.nodeid,
        attempts=trace.attempts,
        client_responses=trace.client_responses,
        service_logs=service_log_evidence([*caplog.get_records("setup"), *caplog.get_records("call")]),
        native_sessions=native_sessions,
        capture_errors=capture_errors,
        trigger="test_call_failed",
    )


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
