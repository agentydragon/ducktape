"""Pytest configuration and fixtures for Gatelet tests.

Uses Testcontainers for hermetic PostgreSQL instances. Each test session gets
a fresh PostgreSQL container.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer

from gatelet.server.app import app
from gatelet.server.config import (
    AdminSettings,
    AuthSettings,
    ChallengeResponseAuthSettings,
    DatabaseSettings,
    HomeAssistantSettings,
    KeyInUrlAuthSettings,
    SecuritySettings,
    ServerSettings,
    Settings,
    WebhookSettings,
)
from gatelet.server.database import get_db_session
from gatelet.server.endpoints.webhook_view import PayloadSummary
from gatelet.server.models import AuthCRSession, AuthKey, Base
from gatelet.server.tests.utils import persist

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest-asyncio auto mode and other test settings."""
    config.option.asyncio_mode = "auto"
    config.option.asyncio_default_fixture_loop_scope = "function"

    # Disable anyio plugin which can conflict with pytest-asyncio
    config.pluginmanager.set_blocked("anyio")


@dataclass
class PostgresConfig:
    """PostgreSQL connection configuration from testcontainer."""

    host: str
    port: int
    user: str
    password: str
    database: str

    def url(self, driver: str = "asyncpg") -> str:
        """Build SQLAlchemy connection URL."""
        scheme = f"postgresql+{driver}" if driver else "postgresql"
        return f"{scheme}://{self.user}:{self.password}@{self.host}:{self.port}/{self.database}"


@pytest.fixture(scope="session")
def postgres_container() -> Generator[PostgresContainer]:
    """Session-scoped PostgreSQL container.

    Starts a fresh PostgreSQL 16 container for the entire test session.
    """
    with PostgresContainer(image="postgres:16", username="postgres", password="postgres", dbname="gatelet") as postgres:
        yield postgres


@pytest.fixture(scope="session")
def postgres_config(postgres_container: PostgresContainer) -> PostgresConfig:
    """Session-scoped database config from the testcontainer."""
    return PostgresConfig(
        host=postgres_container.get_container_host_ip(),
        port=int(postgres_container.get_exposed_port(5432)),
        user="postgres",
        password="postgres",
        database="gatelet",
    )


@pytest_asyncio.fixture
async def db_engine(postgres_config: PostgresConfig) -> AsyncGenerator[AsyncEngine]:
    """Create a database engine and initialize the schema."""
    engine = create_async_engine(postgres_config.url(), future=True)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncGenerator[AsyncSession]:
    """Provide a database session wrapped in a transaction."""

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)

    async with session_factory() as session:
        trans = await session.begin()
        try:
            yield session
            if trans.is_active:
                await trans.commit()
            await session.execute(text("DELETE FROM admin_sessions"))
            await session.commit()
        finally:
            if trans.is_active:
                await trans.rollback()


@pytest.fixture(autouse=True)
def _patch_get_db_session(monkeypatch, db_session: AsyncSession) -> None:
    """Override ``get_db_session`` globally for tests."""

    @asynccontextmanager
    async def _override() -> AsyncGenerator[AsyncSession]:
        yield db_session

    monkeypatch.setattr("gatelet.server.database.get_db_session", _override)
    monkeypatch.setattr("gatelet.server.app.get_db_session", _override)


@pytest.fixture
def test_settings(tmp_path: Path, postgres_config: PostgresConfig) -> Settings:
    """Create test settings with explicit test values.

    Tests should use this fixture and override specific values as needed.
    This ensures tests don't depend on production config.
    """
    return Settings(
        database=DatabaseSettings(dsn=postgres_config.url()),  # Uses asyncpg by default
        server=ServerSettings(log_file=str(tmp_path / "test.log")),
        auth=AuthSettings(
            key_in_url=KeyInUrlAuthSettings(enabled=True, key_valid_days=365),
            challenge_response=ChallengeResponseAuthSettings(enabled=True, num_options=16),
        ),
        home_assistant=HomeAssistantSettings(api_url="http://test:8123", api_token="test-token"),
        webhook=WebhookSettings(),
        admin=AdminSettings(
            password_hash="$argon2id$v=19$m=65536,t=3,p=4$RCjF2HuPkbI2htBaK8X4/w$ZaY5qRPTqw/wMjAVnxaK9cneVhAsRBQ0Ru1oZW09Mx8"  # argon2 hash for "gatelet"
        ),
        security=SecuritySettings(csrf_secret="test-csrf-secret"),
    )


@pytest.fixture(autouse=True)
def _patch_get_settings(monkeypatch, test_settings: Settings) -> None:
    """Override ``get_settings`` globally for tests.

    This patches both the original location and where it's imported,
    ensuring lifespan and route handlers both use test settings.
    """
    monkeypatch.setattr("gatelet.server.config.get_settings", lambda: test_settings)
    monkeypatch.setattr("gatelet.server.lifespan.get_settings", lambda: test_settings)


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    """Get a test client connected to the test database with test settings."""

    async def override_db() -> AsyncGenerator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = override_db

    # Use LifespanManager to properly trigger app startup (which registers auth routes)
    async with (
        LifespanManager(app) as manager,
        AsyncClient(transport=ASGITransport(app=manager.app), base_url="http://testserver") as client,
    ):
        yield client

    app.dependency_overrides.pop(get_db_session, None)


@pytest_asyncio.fixture
async def test_auth_key(db_session: AsyncSession) -> AuthKey:
    """Create a temporary authentication key."""

    unique_id = uuid4().hex[:8]
    key = AuthKey(
        key_value=f"test-key-{unique_id}", description=f"Test auth key {unique_id}", created_at=datetime.now()
    )
    return await persist(db_session, key)


@pytest_asyncio.fixture
async def test_auth_session(db_session: AsyncSession, test_auth_key: AuthKey) -> AuthCRSession:
    """Create a temporary session bound to ``test_auth_key``."""

    unique_id = uuid4().hex[:8]
    session = AuthCRSession(
        session_token=f"test-session-{unique_id}",
        auth_key_id=test_auth_key.id,
        created_at=datetime.now(),
        expires_at=datetime.now() + timedelta(hours=1),
        last_activity_at=datetime.now(),
    )
    return await persist(db_session, session)


@pytest.fixture(autouse=True)
async def _stub_data(monkeypatch):
    """Stub external data fetchers for all tests."""

    async def _states(*_args, **_kwargs):
        return [{"entity_id": "sensor.test", "state": "on", "last_changed": datetime(2020, 1, 1)}]

    async def _payloads(*_args, **_kwargs):
        return [PayloadSummary(id=1, integration_name="test", received_at=datetime(2020, 1, 1))]

    monkeypatch.setattr("gatelet.server.endpoints.homeassistant.fetch_states", _states)
    monkeypatch.setattr("gatelet.server.endpoints.webhook_view.get_latest_payloads", _payloads)
    # Also patch in lifespan where these are imported for the authenticated_root_handler
    monkeypatch.setattr("gatelet.server.lifespan.fetch_states", _states)
    monkeypatch.setattr("gatelet.server.lifespan.get_latest_payloads", _payloads)
