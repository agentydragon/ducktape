"""Database fixtures for props tests."""

import hashlib
import inspect
import os
from collections.abc import AsyncGenerator, Callable, Generator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from props.core.db.config import DatabaseConfig, get_database_config
from props.core.db.session import dispose_db, get_session, init_db, recreate_database
from props.core.db.setup import ensure_database_exists
from props.core.db.sync.sync import sync_all

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


@pytest.fixture(autouse=True)
def block_production_config_in_tests(monkeypatch: pytest.MonkeyPatch) -> Callable:
    """Prevent test functions from accidentally using production database.

    Tests should use the test_db fixture, which creates isolated test databases.
    Calling get_database_config() from test code is a bug - it returns production
    database credentials instead of the test-specific isolated database.

    This fixture blocks ALL calls to get_database_config() from test files.
    Production code (like database session management, Alembic offline mode) can
    still call it normally.
    """

    original = get_database_config

    def _block_from_tests(*args, **kwargs):
        # Check the immediate caller (frame 1)
        stack = inspect.stack()
        if len(stack) > 1:
            caller_frame = stack[1]
            caller_file = caller_frame.filename
            # If called from a test file, fail
            if "/tests/" in caller_file and caller_file.endswith(".py"):
                raise RuntimeError(
                    f"Tests must use test_db fixture, not get_database_config()!\n"
                    f"Called from: {caller_file}:{caller_frame.lineno}\n"
                    f"Fix: Use 'config = test_db' instead of 'get_database_config()'."
                )
        # Called from production code - allow it
        return original(*args, **kwargs)

    monkeypatch.setattr("props_core.db.config.get_database_config", _block_from_tests)

    # Return original for test_db fixture to use
    return original


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
def test_db(
    request: pytest.FixtureRequest, block_production_config_in_tests: Callable
) -> Generator[DatabaseConfig]:
    """Create isolated database for each test.

    Creates a unique database per test, initializes schema, and drops it after.
    Safe for parallel pytest-xdist execution - each test gets its own database.
    """
    test_node_id = request.node.nodeid
    sanitized_id = _sanitize_test_id(test_node_id)
    db_name = f"props_test_{sanitized_id}"

    get_database_config_original = block_production_config_in_tests
    base_config = get_database_config_original()

    ensure_database_exists(base_config, db_name, drop_existing=True)
    test_config = base_config.with_database(db_name)

    postgres_config = base_config.with_database("postgres")
    postgres_engine = create_engine(postgres_config.admin_url(), isolation_level="AUTOCOMMIT")

    dispose_db()
    init_db(test_config)
    recreate_database()

    yield test_config

    keep_db = request.config.getoption("--keep-db") or os.environ.get("KEEP_TEST_DB") == "1"
    if keep_db:
        print(f"\n\n=== KEEPING TEST DATABASE: {db_name} ===")
        print(f"Database config: {test_config}")
        print(f"Connect with: direnv exec adgn psql -d {db_name}")
        postgres_engine.dispose()
        return

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
    request: pytest.FixtureRequest, session_monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[DatabaseConfig]:
    """Internal: Session-scoped synced database.

    Use synced_readonly_session instead of this directly.
    """
    db_name = "props_test_session_shared"
    base_config = get_database_config()
    ensure_database_exists(base_config, db_name, drop_existing=True)
    test_config = base_config.with_database(db_name)

    postgres_config = base_config.with_database("postgres")
    postgres_engine = create_engine(postgres_config.admin_url(), isolation_level="AUTOCOMMIT")

    dispose_db()
    init_db(test_config)
    recreate_database()

    _sync_test_fixtures(session_monkeypatch)

    yield test_config

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
