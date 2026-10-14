"""Real PostgreSQL, migrated by the same image-owned runner used in deployment."""

from collections.abc import AsyncIterator, Callable
from unittest.mock import create_autospec

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agentplane.action_service.testing.fixtures import action_database_template, echo_catalog, echo_executor
from agentplane.history_service.client import HistoryServiceClient
from agentplane.history_service.testing.fixtures import service_database_template
from agentplane.notification_service.database_migrate import RUNNER
from agentplane.notification_service.dispatcher_client import SessionDispatcherClient
from agentplane.notification_service.service import Service
from agentplane.notification_service.settings import NoticeDebounceSettings, QuotaSettings
from agentplane.notification_service.sources.actions import ActionServiceClient
from agentplane.notification_service.sources.github.client import GitHubClient
from agentplane.notification_service.sources.github.source import GitHub
from agentplane.notification_service.store import Store
from agentplane.runner.testing.failure_journal import pytest_runtest_makereport
from agentplane.runner.testing.fixtures import config, endpoint, harness, model, runner, spec, workspace
from agentplane.runner_gateway.client import RunnerGatewayClient
from agentplane.sandboxes.reader import SandboxReader
from agentplane.session_dispatcher.database_migrate import RUNNER as DISPATCHER_MIGRATIONS
from util.testing.postgres import DatabaseClones, PostgresDatabaseTemplate
from util.testing.postgres_fixtures import (
    database_clones,
    database_names,
    engine,
    migrated_database_fixtures,
    postgres_admin_url,
    postgres_container,
    postgres_image,
)

# Both native harnesses, not an app-managed session or app database.
from util.testing.sqlalchemy_engines import managed_async_engine

# gazelle:include_dep @pypi//asyncpg
# gazelle:include_dep @pypi//psycopg

database_template, db_url = migrated_database_fixtures(RUNNER, drivername="postgresql+asyncpg")
dispatcher_database_template, dispatcher_db_url = migrated_database_fixtures(
    DISPATCHER_MIGRATIONS,
    drivername="postgresql+asyncpg",
    template_name="dispatcher_database_template",
    url_name="dispatcher_db_url",
)


@pytest.fixture
def sandbox_service_db_url(service_database_template: PostgresDatabaseTemplate, database_clones: DatabaseClones) -> str:
    return database_clones.clone(service_database_template, drivername="postgresql+asyncpg")


@pytest.fixture
async def store(engine: AsyncEngine) -> Store:
    return Store(engine, quotas=QuotaSettings())


@pytest.fixture
async def action_engine(
    action_database_template: PostgresDatabaseTemplate, database_clones: DatabaseClones
) -> AsyncIterator[AsyncEngine]:
    async with managed_async_engine(
        create_async_engine(database_clones.clone(action_database_template, drivername="postgresql+asyncpg"))
    ) as engine:
        yield engine


@pytest.fixture
def service(store: Store) -> Service:
    """Delivery over the real store; remote clients are autospecced and the GitHub source is off."""
    return Service(
        store,
        create_autospec(ActionServiceClient),
        create_autospec(SandboxReader, instance=True, namespace="test"),
        github=None,
        gateway=create_autospec(RunnerGatewayClient),
        history=create_autospec(HistoryServiceClient),
        dispatcher=create_autospec(SessionDispatcherClient),
        notice_debounce=NoticeDebounceSettings(),
        stale_confirmation_s=30,
        operator_reader_account=None,
    )


@pytest.fixture
def restart(engine: AsyncEngine) -> Callable[[Service], Service]:
    """Rebuild a Service with fresh store and source objects, keeping its remote clients and settings,
    so whatever the restarted service still knows came from the database."""

    def restarted(service: Service) -> Service:
        github = (
            None
            if service.github is None
            else GitHub(GitHubClient(service.github.client.http, service.github.settings))
        )
        return Service(
            Store(engine, quotas=QuotaSettings()),
            service.actions,
            service.sandboxes,
            github,
            gateway=service.gateway,
            history=service.history,
            dispatcher=service.dispatcher,
            notice_debounce=service.notice_debounce,
            stale_confirmation_s=service.stale_confirmation_s,
            operator_reader_account=service.operator_reader_account,
        )

    return restarted
