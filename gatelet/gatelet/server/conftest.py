"""Pytest configuration and fixtures for Gatelet tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import logging
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from uuid import uuid4

from httpx import AsyncClient
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

os.environ.setdefault("GATELET_CONFIG", str(Path(__file__).resolve().parent.parent.parent / "gatelet.toml"))
# pylint: disable=wrong-import-position
# Imports must follow environment setup so modules see configured GATELET_CONFIG
from gatelet.server.app import app  # type: ignore[import] - Imports after env setup (required for config)
from gatelet.server.config import get_settings  # type: ignore[import] - Imports after env setup (required for config)
from gatelet.server.database import (
    get_db_session,  # type: ignore[import] - Imports after env setup (required for config)
)
from gatelet.server.models import (  # type: ignore[import] - Imports after env setup (required for config)
    AuthCRSession,
    AuthKey,
    Base,
)
from gatelet.server.tests.utils import persist  # type: ignore[import] - Imports after env setup (required for config)

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")


@pytest.fixture(scope="session", autouse=True)
def _postgres():
    """Start and stop a temporary PostgreSQL server if needed."""

    # In CI (GitHub Actions, etc.), use the service container
    # The service container provides PostgreSQL at localhost:5432
    if os.environ.get("CI") == "true" or os.environ.get("GITHUB_ACTIONS") == "true":
        os.environ["DATABASE_URL"] = "postgresql+asyncpg://postgres:postgres@localhost:5432/gatelet"
        get_settings().database.dsn = os.environ["DATABASE_URL"]
        yield
        return

    # In Codex environment, set up a temporary PostgreSQL server
    if os.environ.get("IS_CODEX_ENV") != "1":
        yield
        return

    datadir = tempfile.mkdtemp(prefix="pgdata-")
    subprocess.check_call(["chown", "-R", "postgres:postgres", datadir])
    bin_dir = Path(shutil.which("initdb")).parent
    initdb = bin_dir / "initdb"
    pg_ctl = bin_dir / "pg_ctl"
    createdb = bin_dir / "createdb"
    port = "55432"
    subprocess.check_call(["sudo", "-u", "postgres", str(initdb), "-D", datadir, "-A", "trust"])
    subprocess.check_call(["sudo", "-u", "postgres", str(pg_ctl), "-D", datadir, "-w", "-o", f"-p {port}", "start"])
    subprocess.check_call(["sudo", "-u", "postgres", str(createdb), "-p", port, "gatelet"])
    os.environ["DATABASE_URL"] = f"postgresql+asyncpg://postgres@localhost:{port}/gatelet"
    get_settings().database.dsn = os.environ["DATABASE_URL"]
    os.environ.setdefault("GATELET_CONFIG", str(Path(__file__).resolve().parent.parent / "gatelet.toml"))
    time.sleep(0.5)
    try:
        yield
    finally:
        subprocess.check_call(["sudo", "-u", "postgres", str(pg_ctl), "-D", datadir, "-m", "fast", "stop"])
        shutil.rmtree(datadir)


@pytest_asyncio.fixture
async def db_engine(_postgres) -> AsyncGenerator[AsyncEngine, None]:
    """Create a database engine and initialize the schema."""

    database_url = os.environ.get("DATABASE_URL", "postgresql+asyncpg://postgres@localhost/postgres")
    engine = create_async_engine(database_url, future=True)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
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
    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    monkeypatch.setattr("gatelet.server.database.get_db_session", _override)
    monkeypatch.setattr("gatelet.server.app.get_db_session", _override)


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """Get a test client connected to the test database."""

    async def override_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db_session] = override_db
    async with AsyncClient(app=app, base_url="http://testserver") as client:
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
    from gatelet.server.endpoints.webhook_view import PayloadSummary

    async def _states():
        return [{"entity_id": "sensor.test", "state": "on", "last_changed": datetime(2020, 1, 1)}]

    async def _payloads(*_args, **_kwargs):
        return [PayloadSummary(id=1, integration_name="test", received_at=datetime(2020, 1, 1))]

    monkeypatch.setattr("gatelet.server.endpoints.homeassistant.fetch_states", _states)
    monkeypatch.setattr("gatelet.server.endpoints.webhook_view.get_latest_payloads", _payloads)
    monkeypatch.setattr("gatelet.server.app.fetch_states", _states)
    monkeypatch.setattr("gatelet.server.app.get_latest_payloads", _payloads)
