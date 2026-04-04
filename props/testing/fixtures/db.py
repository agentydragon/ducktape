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
from opentelemetry import trace
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer

from props.db.config import DatabaseConfig
from props.db.database import Database
from props.db.setup import ensure_database_exists
from props.db.sync.model_metadata import sync_model_metadata_with_session
from props.db.sync.sync import SpecimenBundle, refresh_examples_matview, sync_specimen
from third_party.containers.rlocations import POSTGRES_18, RYUK
from util.bazel.runfiles import get_required_path
from util.oci import load_oci_image

tracer = trace.get_tracer(__name__)

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
    with tracer.start_as_current_span("postgres_container fixture"):
        load_oci_image(RYUK)
        load_oci_image(POSTGRES_18)

        with tracer.start_as_current_span("PostgresContainer startup"):
            container = PostgresContainer(
                image=POSTGRES_18.tag, username="postgres", password="postgres", dbname="postgres"
            )
            container.start()

    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="session")
def postgres_base_config(postgres_container: PostgresContainer) -> DatabaseConfig:
    """Session-scoped base database config from the testcontainer.

    Provides connection parameters for the containerized PostgreSQL instance.
    Agent containers reach postgres via host.docker.internal (same mapped port).
    """
    host = postgres_container.get_container_host_ip()
    port = int(postgres_container.get_exposed_port(5432))

    return DatabaseConfig(host=host, port=port, database="postgres", user="postgres", password="postgres")


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


def _setup_test_database(postgres_base_config: DatabaseConfig, db_name: str) -> tuple[Database, Engine]:
    """Drop (if exists), create, and migrate a test database. Returns (database, postgres_engine)."""
    postgres_config = postgres_base_config.with_database("postgres")
    postgres_engine = create_engine(postgres_config.url, isolation_level="AUTOCOMMIT")
    _terminate_and_drop_db(postgres_engine, db_name)
    ensure_database_exists(postgres_base_config, db_name)
    database = Database(postgres_base_config.with_database(db_name))
    database.recreate()
    return database, postgres_engine


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
def db(request: pytest.FixtureRequest, postgres_base_config: DatabaseConfig) -> Generator[Database]:
    """Create isolated Database for each test.

    Creates a unique database per test, initializes schema, and drops it after.
    Safe for parallel pytest-xdist execution - each test gets its own database.
    """
    test_node_id = request.node.nodeid
    sanitized_id = _sanitize_test_id(test_node_id)
    db_name = f"props_test_{sanitized_id}"

    database, postgres_engine = _setup_test_database(postgres_base_config, db_name)

    try:
        yield database
    finally:
        database.dispose()
        keep_db = request.config.getoption("--keep-db") or os.environ.get("KEEP_TEST_DB") == "1"
        if keep_db:
            test_config = postgres_base_config.with_database(db_name)
            print(f"\n\n=== KEEPING TEST DATABASE: {db_name} ===")
            print(f"Database config: {test_config}")
            print(f"Connect with: psql {test_config.url}")
        else:
            _terminate_and_drop_db(postgres_engine, db_name)
        postgres_engine.dispose()


@pytest.fixture
def engine(db: Database) -> Engine:
    """Engine from the db fixture."""
    return db.engine


def _sync_test_fixtures(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sync test fixtures to the current database using bundle workflow."""
    fixture_slugs = ["test-fixtures/test1", "test-fixtures/train1", "test-fixtures/valid1", "test-fixtures/valid2"]

    with db.session() as session:
        sync_model_metadata_with_session(session)
        for slug in fixture_slugs:
            base_path = f"props/testing/fixtures/testdata/specimens/{slug}"
            code_tar = get_required_path(f"_main/{base_path}/specimen_code.tar")
            data_yaml = get_required_path(f"_main/{base_path}/specimen_data.yaml")
            bundle = SpecimenBundle.from_paths(code_tar, data_yaml)
            sync_specimen(session, bundle)
        session.commit()
        refresh_examples_matview(session)


@pytest.fixture(scope="session")
def session_monkeypatch() -> Generator[pytest.MonkeyPatch]:
    """Session-scoped monkeypatch for environment variable overrides."""
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def _session_synced_db(
    postgres_base_config: DatabaseConfig, session_monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[Database]:
    """Internal: Session-scoped synced database.

    Use synced_readonly_session instead of this directly.
    Uses the session-scoped postgres container.
    """
    db_name = "props_test_session_shared"

    database, postgres_engine = _setup_test_database(postgres_base_config, db_name)
    _sync_test_fixtures(database, session_monkeypatch)

    try:
        yield database
    finally:
        database.dispose()
        _terminate_and_drop_db(postgres_engine, db_name)
        postgres_engine.dispose()


@pytest.fixture(scope="session")
def synced_readonly_session(_session_synced_db: Database) -> Generator[Session]:
    """Session-scoped SQLAlchemy Session for READ-ONLY tests.

    WARNING: Do not commit/write via this session - use synced_db for write tests.
    """
    with _session_synced_db.session() as session:
        yield session


@pytest.fixture
def synced_db(db: Database, monkeypatch: pytest.MonkeyPatch) -> Database:
    """Test database with test fixture specimens synced."""
    _sync_test_fixtures(db, monkeypatch)
    return db


@pytest.fixture
def synced_test_session(synced_db: Database) -> Generator[Session]:
    """Function-scoped session over synced test database (read-write)."""
    with synced_db.session() as session:
        yield session
