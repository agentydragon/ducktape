"""Database session management.

Provides the `Database` class — the single owner of an engine + session factory.
Created at entrypoints (backend lifespan, CLI, agent main, test fixtures) and
passed explicitly through the call graph.

Usage:
    # From environment variables (containers, CLI):
    db = Database.from_env()

    # From explicit config (tests, custom setup):
    db = Database(config)

    with db.session() as session:
        session.add(obj)
        # Commits on successful exit, rolls back on exception

    # Schema management:
    db.recreate()

    # Cleanup:
    db.dispose()
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any

from psycopg2.extras import register_composite
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, scoped_session, sessionmaker
from sqlalchemy.pool import ConnectionPoolEntry, NullPool

from props.db.config import DatabaseConfig
from props.db.setup import recreate_database

logger = logging.getLogger(__name__)


def _register_composite_types(
    dbapi_connection: Any, connection_record: ConnectionPoolEntry, connection_proxy: Any
) -> None:
    """Register the stats_with_ci adapter on a freshly checked-out connection.

    NullPool creates a fresh connection per checkout, so this runs on every use.
    """
    try:
        register_composite("stats_with_ci", dbapi_connection, globally=False)
    except Exception as e:
        logger.debug(f"Could not register stats_with_ci composite type: {e}")


class Database:
    """Owns an engine and session factory. No global state.

    Create at entrypoints, pass explicitly. Call dispose() when done.
    """

    @classmethod
    def from_env(cls) -> Database:
        """Create Database from PG* environment variables.

        Reads PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD from the environment.
        Use at container/CLI entrypoints where config comes from env vars.
        """
        return cls(DatabaseConfig())

    @classmethod
    def per_request(cls, config: DatabaseConfig) -> Database:
        """Create Database for per-request use (no startup verification).

        Used by get_caller_db for caller credential passthrough. Caller must
        call dispose() when done.
        """
        return cls(config, verify=False)

    def __init__(self, config: DatabaseConfig, *, verify: bool = True) -> None:
        """`verify=False` skips the startup connection probe and its log line (see `per_request`)."""
        self._config = config
        self._engine: Engine = create_engine(config.url, echo=False, poolclass=NullPool)
        event.listen(self._engine, "checkout", _register_composite_types)
        if verify:
            logger.info(f"Connecting to database: {config.host}:{config.port}/{config.database}")
            self._verify_connection()
        self._scoped_factory = scoped_session(sessionmaker(bind=self._engine))

    def _verify_connection(self, timeout_secs: int = 2) -> None:
        with ExitStack() as cleanup:
            test_engine = create_engine(
                self._engine.url.render_as_string(hide_password=False),
                echo=False,
                connect_args={"connect_timeout": timeout_secs},
            )
            cleanup.callback(test_engine.dispose)
            with test_engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            logger.debug("Database connection validated")

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Get a database session (context manager).

        Commits on successful exit; the session context rolls back on exception.
        """
        try:
            with self._scoped_factory() as session:
                yield session
                session.commit()
        finally:
            # Closing the Session does not clear scoped_session's thread-local registry.
            self._scoped_factory.remove()

    def recreate(self) -> None:
        """Recreate database from scratch (drop all + schema via Alembic)."""
        recreate_database(self._engine)

    def dispose(self) -> None:
        """Dispose engine and session factory."""
        self._scoped_factory.remove()
        self._engine.dispose()

    @property
    def config(self) -> DatabaseConfig:
        return self._config

    @property
    def engine(self) -> Engine:
        return self._engine
