"""Pytest configuration and fixtures for Gatelet tests."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi.templating import Jinja2Templates
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from gatelet.server.endpoints.webhook_view import PayloadSummary

os.environ.setdefault("GATELET_CONFIG", str(Path(__file__).resolve().parent.parent / "gatelet.toml"))
# pylint: disable=wrong-import-position
# Imports must follow environment setup so modules see configured GATELET_CONFIG
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
    get_settings,
)
from gatelet.server.database import get_db_session
from gatelet.server.lifespan import BASE_DIR, _init_csrf_config
from gatelet.server.models import AuthCRSession, AuthKey, Base
from gatelet.server.tests.utils import persist

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")


def _build_database_url(driver: str = "asyncpg") -> str:
    """Build DATABASE_URL from standard PG* environment variables.

    Expected env vars (set by CI workflow or local devenv):
        PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE

    Args:
        driver: SQLAlchemy driver suffix (e.g., "asyncpg" for async, empty for sync)
    """
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5432")
    user = os.environ.get("PGUSER", "postgres")
    password = os.environ.get("PGPASSWORD", "postgres")
    database = os.environ.get("PGDATABASE", "gatelet")

    scheme = f"postgresql+{driver}" if driver else "postgresql"
    if password:
        return f"{scheme}://{user}:{password}@{host}:{port}/{database}"
    return f"{scheme}://{user}@{host}:{port}/{database}"


@pytest_asyncio.fixture
async def db_engine() -> AsyncGenerator[AsyncEngine]:
    """Create a database engine and initialize the schema."""

    database_url = _build_database_url()
    engine = create_async_engine(database_url, future=True)

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
def test_settings(tmp_path: Path) -> Settings:
    """Create test settings with explicit test values.

    Tests should use this fixture and override specific values as needed.
    This ensures tests don't depend on production config.
    """
    return Settings(
        database=DatabaseSettings(dsn=_build_database_url(driver="")),
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


@pytest_asyncio.fixture
async def client(db_session: AsyncSession, test_settings: Settings) -> AsyncGenerator[AsyncClient]:
    """Get a test client connected to the test database with test settings."""

    async def override_db() -> AsyncGenerator[AsyncSession]:
        yield db_session

    def override_settings() -> Settings:
        return test_settings

    # Initialize CSRF protection for tests (normally done in lifespan)
    _init_csrf_config(test_settings.security.csrf_secret)

    # Initialize templates (normally done in lifespan)
    app.state.templates = Jinja2Templates(directory=BASE_DIR / "templates")
    app.state.templates.env.globals.update({"max": max, "min": min})

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[get_settings] = override_settings
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        yield client
    app.dependency_overrides.pop(get_db_session, None)
    app.dependency_overrides.pop(get_settings, None)


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
