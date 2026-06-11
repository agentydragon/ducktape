"""Database fixtures for props tests.

Uses Testcontainers for hermetic PostgreSQL instances. Each test session gets
a fresh PostgreSQL container, and each test gets its own isolated database.
"""

import hashlib
import os
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer

from props.db.config import DatabaseConfig
from props.db.session import dispose_db, get_session, init_db, recreate_database
from props.db.setup import ensure_database_exists
from props.db.sync.sync import sync_all

# Path to test specimens (git-tracked fixtures)
TEST_FIXTURES_PATH = Path(__file__).parent / "testdata" / "specimens"


@pytest.fixture
def test_specimens_base() -> Path:
    """Path to test specimens directory (git-tracked fixtures)."""
    return TEST_FIXTURES_PATH


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add custom pytest command-line options."""
    parser.addoption(
        "--keep-db",
        action="store_true",
        default=False,
        help="Preserve test database on failure for debugging (does not drop test database after test)",
    )


@pytest.fixture(scope="session")
def postgres_container() -> Generator[PostgresContainer]:
    """Session-scoped PostgreSQL container.

    Starts a fresh PostgreSQL 16 container for the entire test session.
    All tests share this container but get isolated databases.
    """
    with PostgresContainer(
        image="postgres:16", username="postgres", password="postgres", dbname="postgres"
    ) as postgres:
        yield postgres


@pytest.fixture(scope="session")
def postgres_base_config(postgres_container: PostgresContainer) -> DatabaseConfig:
    """Session-scoped base database config from the testcontainer.

    Provides connection parameters for the containerized PostgreSQL instance.
    Agent containers can reach postgres via host.docker.internal since
    testcontainers maps the port to the host.
    """
    host = postgres_container.get_container_host_ip()
    port = int(postgres_container.get_exposed_port(5432))

    # Container routing: agent containers reach postgres via host.docker.internal
    # since testcontainers exposes the port on the host
    container_host = os.environ.get("PROPS_E2E_HOST_HOSTNAME", "host.docker.internal")

    return DatabaseConfig(
        host=host,
        port=port,
        database="postgres",
        container_name=container_host,
        container_port=port,  # Same mapped port, accessible via host.docker.internal
        user="postgres",
        password="postgres",
    )


def _terminate_and_drop_db(postgres_engine, db_name: str) -> None:
    """Terminate all connections and drop a database.

    Used for test cleanup to ensure databases can be dropped even if
    connections are still open.
    """
    with postgres_engine.connect() as conn:
        conn.execute(
            text(
                f"""
                SELECT pg_terminate_backend(pg_stat_activity.pid)
                FROM pg_stat_activity
                WHERE pg_stat_activity.datname = '{db_name}'
                  AND pid <> pg_backend_pid()
            """
            )
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))


def _sanitize_test_id(test_id: str, max_length: int = 63) -> str:
    """Sanitize pytest node ID for use in PostgreSQL database name."""
    # Keep only alphanumeric and underscore; replace other chars with underscore
    sanitized = "".join(c if c.isalnum() or c == "_" else "_" for c in test_id)
    # Collapse consecutive underscores
    while "__" in sanitized:
        sanitized = sanitized.replace("__", "_")
    # Trim leading/trailing underscores
    sanitized = sanitized.strip("_")
    # Ensure it fits PostgreSQL's 63-character limit (including 'props_test_' prefix)
    prefix = "props_test_"
    available_length = max_length - len(prefix)
    if len(sanitized) > available_length:
        hash_suffix = hashlib.sha256(test_id.encode()).hexdigest()[:8]
        prefix_length = available_length - len(hash_suffix) - 1
        sanitized = f"{sanitized[:prefix_length]}_{hash_suffix}"
    return sanitized


@pytest.fixture
def test_db(request: pytest.FixtureRequest, postgres_base_config: DatabaseConfig) -> Generator[DatabaseConfig]:
    """Create isolated database for each test.

    Creates a unique database per test, initializes schema, and drops it after.
    Safe for parallel pytest-xdist execution - each test gets its own database.
    Uses the session-scoped postgres container via postgres_base_config.
    """
    test_node_id = request.node.nodeid
    sanitized_id = _sanitize_test_id(test_node_id)
    db_name = f"props_test_{sanitized_id}"

    ensure_database_exists(postgres_base_config, db_name, drop_existing=True)
    test_config = postgres_base_config.with_database(db_name)

    postgres_config = postgres_base_config.with_database("postgres")
    postgres_engine = create_engine(postgres_config.admin_url(), isolation_level="AUTOCOMMIT")

    dispose_db()
    init_db(test_config)
    recreate_database()

    try:
        yield test_config
    finally:
        keep_db = request.config.getoption("--keep-db") or os.environ.get("KEEP_TEST_DB") == "1"
        if keep_db:
            print(f"\n\n=== KEEPING TEST DATABASE: {db_name} ===")
            print(f"Database config: {test_config}")
            print(f"Connect with: psql {test_config.admin_url()}")
        else:
            _terminate_and_drop_db(postgres_engine, db_name)
        postgres_engine.dispose()


@pytest.fixture
def admin_engine(test_db: DatabaseConfig) -> Generator:
    """Create admin engine for test database with proper disposal."""
    engine = create_engine(test_db.admin_url())
    try:
        yield engine
    finally:
        engine.dispose()


def _sync_test_fixtures(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sync test fixtures to the current database."""
    monkeypatch.setenv("ADGN_PROPS_SPECIMENS_ROOT", str(TEST_FIXTURES_PATH))
    with get_session() as session:
        sync_all(session, use_staged=True)


@pytest.fixture(scope="session")
def session_monkeypatch() -> Generator[pytest.MonkeyPatch]:
    """Session-scoped monkeypatch for environment variable overrides."""
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def _session_synced_db(
    postgres_base_config: DatabaseConfig, session_monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[DatabaseConfig]:
    """Internal: Session-scoped synced database.

    Use synced_readonly_session instead of this directly.
    Uses the session-scoped postgres container.
    """
    db_name = "props_test_session_shared"
    ensure_database_exists(postgres_base_config, db_name, drop_existing=True)
    test_config = postgres_base_config.with_database(db_name)

    postgres_config = postgres_base_config.with_database("postgres")
    postgres_engine = create_engine(postgres_config.admin_url(), isolation_level="AUTOCOMMIT")

    dispose_db()
    init_db(test_config)
    recreate_database()

    _sync_test_fixtures(session_monkeypatch)

    try:
        yield test_config
    finally:
        _terminate_and_drop_db(postgres_engine, db_name)
        postgres_engine.dispose()


@pytest.fixture(scope="session")
def synced_readonly_session(_session_synced_db: DatabaseConfig) -> Generator[Session]:
    """Session-scoped SQLAlchemy Session for READ-ONLY tests.

    WARNING: Do not commit/write via this session - use synced_test_db for write tests.
    """
    with get_session() as session:
        yield session


@pytest.fixture
def synced_test_db(test_db: DatabaseConfig, monkeypatch: pytest.MonkeyPatch) -> DatabaseConfig:
    """Test database with test fixture specimens synced."""
    _sync_test_fixtures(monkeypatch)
    return test_db


@pytest.fixture
def synced_test_session(synced_test_db: DatabaseConfig) -> Generator[Session]:
    """Function-scoped session over synced test database (read-write)."""
    with get_session() as session:
        yield session
