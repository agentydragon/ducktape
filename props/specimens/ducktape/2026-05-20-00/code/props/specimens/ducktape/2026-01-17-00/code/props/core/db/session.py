"""Database session management.

Uses SQLAlchemy's scoped_session for thread-local session management with lazy
engine initialization.

Usage pattern:
    # Just use get_session() - database auto-initializes on first use:
    with get_session() as session:
        session.add(obj)
        # Commits on successful exit, rolls back on exception

    # For tests that need explicit control:
    dispose_db()          # Reset state
    init_db(test_config)  # Initialize with specific config
    recreate_database()   # Drop all + create schema

Limitations:
    - Cannot have multiple database connections in the same process
    - Safe with pytest-xdist using --dist=loadscope (module-level isolation)

Thread safety:
    - scoped_session provides thread-local sessions automatically
    - Engine initialization uses a lock for one-time setup
    - Multiple threads can call get_session() concurrently

Design rationale:
    - Auto-init: No explicit setup required for normal use
    - Connection pooling: Single engine = efficient connection reuse
    - Simplicity: No dependency injection, works well for evaluation harness use case
    - Test-friendly: dispose_db() + init_db(config) for test isolation
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from psycopg2.extras import register_composite
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session, scoped_session, sessionmaker
from sqlalchemy.pool import ConnectionPoolEntry

from props.core.db.config import DatabaseConfig, get_database_config
from props.core.db.models import Base

logger = logging.getLogger(__name__)

# Module-level state
_engine = None
_session_factory = scoped_session(sessionmaker())
_init_lock = threading.Lock()


def _get_engine(config: DatabaseConfig | None = None):
    """Get or create the database engine (lazy initialization).

    Thread-safe: uses a lock to ensure only one initialization happens.
    """
    global _engine  # noqa: PLW0603

    if _engine is not None:
        return _engine

    with _init_lock:
        # Double-check after acquiring lock
        if _engine is not None:
            return _engine

        if config is None:
            config = get_database_config()

        url = config.admin_url()
        logger.info(f"Connecting to database: {config.admin.host}:{config.admin.port}/{config.admin.database}")

        # Connection pool sized for parallel evaluation (default max_parallelism=20 + overhead)
        _engine = create_engine(url, echo=False, pool_size=20, max_overflow=12)

        # Register composite type adapter on each checkout from pool
        # Uses "checkout" event (not "connect") so registration happens on every use,
        # not just when connection is created. This handles the case where:
        # 1. Connection is created before migrations (type doesn't exist)
        # 2. Migrations create the type
        # 3. Subsequent checkouts get the type registered
        @event.listens_for(_engine, "checkout")
        def _register_composite_types(
            dbapi_connection: Any, connection_record: ConnectionPoolEntry, connection_proxy: Any
        ) -> None:
            """Register PostgreSQL composite types on connection checkout."""
            try:
                register_composite("stats_with_ci", dbapi_connection, globally=False)
            except Exception as e:
                # Type may not exist yet (during migration) - that's OK
                logger.debug(f"Could not register stats_with_ci composite type: {e}")

        # Bind the scoped session factory to the engine
        _session_factory.configure(bind=_engine)

        # Verify connection immediately
        _check_connection_internal(timeout_secs=2)

        return _engine


def _check_connection_internal(timeout_secs: int = 2) -> None:
    """Internal connection check (assumes _engine is set)."""
    if _engine is None:
        raise RuntimeError("Database engine not initialized - call init_db() first")
    logger.debug(f"Validating database connection (timeout: {timeout_secs}s)...")
    test_engine = create_engine(
        _engine.url.render_as_string(hide_password=False), echo=False, connect_args={"connect_timeout": timeout_secs}
    )
    try:
        with test_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.debug("Database connection validated")
    finally:
        test_engine.dispose()


def dispose_db() -> None:
    """Dispose of the current database connection.

    This is needed when switching databases (e.g., between test databases).
    After calling this, the next get_session() call will reinitialize.
    """
    global _engine  # noqa: PLW0603

    with _init_lock:
        _session_factory.remove()
        if _engine is not None:
            _engine.dispose()
            _engine = None


def is_db_initialized() -> bool:
    """Check if database connection is already established."""
    return _engine is not None


def init_db(config: DatabaseConfig | None = None) -> None:
    """Explicitly initialize database connection (for tests).

    For normal use, prefer get_session() which auto-initializes.
    Use this when you need to:
    - Initialize with a specific config (e.g., test database)
    - Control when initialization happens

    Thread-safe: only one initialization can happen at a time.

    Args:
        config: Database configuration (defaults to production config from env vars)

    Raises:
        ValueError: If config is None and required env vars not set (run from devenv shell)
        sqlalchemy.exc.OperationalError: If cannot connect to database within timeout
        RuntimeError: If database already initialized (call dispose_db() first)
    """
    with _init_lock:
        if _engine is not None:
            raise RuntimeError("Database already initialized. Call dispose_db() first to switch databases.")
    # Release lock before potentially slow operation
    _get_engine(config)


def check_connection(timeout_secs: int = 2) -> None:
    """Validate database connection (fail fast if DB not reachable).

    Args:
        timeout_secs: Connection timeout in seconds (default: 2)

    Raises:
        RuntimeError: If database not initialized (call init_db() first)
        sqlalchemy.exc.OperationalError: If cannot connect to database within timeout
    """
    if _engine is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    _check_connection_internal(timeout_secs)


def recreate_database() -> None:
    """Recreate database from scratch (drop all + schema + RLS).

    This is destructive: drops all existing tables, views, and policies.
    Temporary database users are created per-agent as needed (not global roles).

    Must call init_db() first to establish connection as postgres superuser.

    Raises:
        RuntimeError: If database not initialized (call init_db() first)
    """
    if _engine is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")

    logger.info("Recreating database from scratch...")
    _drop_all()
    _create_schema()
    logger.info("Database recreation complete")


def _drop_all() -> None:
    """Drop all database objects by dropping and recreating the public schema."""
    if _engine is None:
        raise RuntimeError("Database not initialized.")

    # Check if any of our tables exist
    inspector = inspect(_engine)
    existing_tables = set(inspector.get_table_names())
    our_tables = {table.name for table in Base.metadata.tables.values()}

    logger.debug(f"_drop_all: existing_tables={existing_tables}, our_tables={our_tables}")

    if our_tables & existing_tables:
        logger.info("Dropping entire public schema and recreating...")
        with _engine.begin() as conn:
            # Drop and recreate public schema (drops everything: tables, views, functions, types, policies)
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
            # Restore default permissions on schema
            conn.execute(text("GRANT ALL ON SCHEMA public TO postgres"))
            conn.execute(text("GRANT ALL ON SCHEMA public TO public"))
        logger.info("Public schema dropped and recreated")
    else:
        logger.debug("No tables to drop - schema is clean")


def _create_schema() -> None:
    """Create schema via Alembic migrations.

    Runs Alembic migrations to create tables, RLS policies, views, and grants.
    Used for test databases (production uses setup.py).
    """
    if _engine is None:
        raise RuntimeError("Database not initialized.")

    # Debug: Check what tables exist BEFORE running migrations
    inspector = inspect(_engine)
    existing_tables_before = set(inspector.get_table_names())
    logger.debug(f"_create_schema BEFORE migration: existing_tables={existing_tables_before}")

    # Check if alembic_version exists
    with _engine.connect() as conn:
        result = conn.execute(
            text("SELECT EXISTS (SELECT FROM pg_tables WHERE schemaname = 'public' AND tablename = 'alembic_version')")
        )
        has_alembic_before = result.scalar()
        logger.debug(f"_create_schema BEFORE migration: alembic_version exists={has_alembic_before}")

        if has_alembic_before:
            result = conn.execute(text("SELECT version_num FROM alembic_version"))
            version = result.scalar()
            logger.debug(f"_create_schema BEFORE migration: current revision={version}")

    # Run Alembic migrations to create all schema objects
    logger.info("Running Alembic migrations...")

    # Enable verbose Alembic logging
    logging.getLogger("alembic").setLevel(logging.DEBUG)

    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))

    # Check what migrations exist
    migrations_dir = Path(__file__).parent / "migrations" / "versions"
    migration_files = list(migrations_dir.glob("*.py"))
    logger.debug(f"Found {len(migration_files)} migration files: {[f.name for f in migration_files]}")

    with _engine.begin() as conn:
        config.attributes["connection"] = conn

        # Check current revision BEFORE upgrade
        script = ScriptDirectory.from_config(config)
        context = MigrationContext.configure(conn)
        current_rev = context.get_current_revision()
        logger.debug(f"Current Alembic revision BEFORE upgrade: {current_rev}")
        logger.debug(f"Target revision (head): {script.get_current_head()}")

        command.upgrade(config, "head")

        # Check current revision AFTER upgrade
        context = MigrationContext.configure(conn)
        current_rev_after = context.get_current_revision()
        logger.debug(f"Current Alembic revision AFTER upgrade: {current_rev_after}")

    # Debug: Check what tables exist AFTER running migrations
    inspector = inspect(_engine)
    existing_tables_after = set(inspector.get_table_names())
    logger.debug(f"_create_schema AFTER migration: existing_tables={existing_tables_after}")

    logger.info("Schema creation complete (via migrations)")


@contextmanager
def get_session() -> Iterator[Session]:
    """Get a database session (context manager).

    Auto-initializes database on first use (reads config from PG* env vars).
    Uses scoped_session for thread-local session management.

    Example:
        with get_session() as session:
            session.add(obj)
            session.commit()

    Raises:
        ValueError: If required env vars not set (run from devenv shell)
        sqlalchemy.exc.OperationalError: If cannot connect to database
    """
    # Ensure engine is initialized (lazy, thread-safe)
    _get_engine()

    session = _session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        # Broad catch is intentional: rollback on any error before re-raising
        session.rollback()
        raise
    finally:
        _session_factory.remove()
