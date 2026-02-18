"""Database setup and initialization (RLS policies, views).

Extracted from database.py to separate concerns:
- database.py: Database class (owns engine + session factory)
- setup.py: Database schema and security setup (recreate_database, RLS, views)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from alembic import command
from alembic.config import Config
from opentelemetry import trace
from psycopg2 import sql
from sqlalchemy import Engine, create_engine, inspect, text

from props.db.models import Base

if TYPE_CHECKING:
    from props.db.config import DatabaseConfig

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


@tracer.start_as_current_span("ensure_database_exists")
def ensure_database_exists(base_config: DatabaseConfig, database_name: str, *, drop_existing: bool = False) -> None:
    """Ensure a PostgreSQL database exists.

    Args:
        base_config: Config with connection params (database name will be replaced)
        database_name: Name of database to create
        drop_existing: If True, drop and recreate (for test setup).
                      If False, create only if missing (for production).

    Note: Does not terminate connections. Tests use unique database names so no
          conflicts in setup. Connection termination remains in test teardown only.
    """
    postgres_config = base_config.with_database("postgres")
    engine = create_engine(postgres_config.url, isolation_level="AUTOCOMMIT")

    with engine.connect() as conn:
        if drop_existing:
            # Fail fast if other sessions are connected to the target DB to surface
            # cross-test interference instead of a vague DROP failure.
            active_sessions = conn.execute(
                text(
                    """
                    select pid, usename, application_name, client_addr
                    from pg_stat_activity
                    where datname = :dbname and pid <> pg_backend_pid()
                    """
                ),
                {"dbname": database_name},
            ).fetchall()

            if active_sessions:
                details = ", ".join(
                    f"pid={pid} user={user} app={app or '-'} addr={addr or '-'}"
                    for pid, user, app, addr in active_sessions
                )
                raise RuntimeError(
                    "Test database in use by other sessions; aborting drop. "
                    f"database={database_name}; sessions=[{details}]"
                )

            # Idempotent drop (for test setup)
            conn.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))

        # Check if database exists
        result = conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :dbname"), {"dbname": database_name})

        if not result.fetchone():
            # Create using safe identifier quoting
            raw_conn = conn.connection
            cursor = raw_conn.cursor()
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
            cursor.close()

    engine.dispose()


@tracer.start_as_current_span("upgrade_database")
def upgrade_database(engine: Engine) -> None:
    """Run Alembic migrations to HEAD (idempotent, non-destructive).

    Safe to call on every startup — Alembic checks the alembic_version table
    and only applies pending migrations.
    """
    logger.info("Running Alembic migrations...")
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))

    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "head")

    logger.info("Alembic migrations complete")


@tracer.start_as_current_span("recreate_database")
def recreate_database(engine: Engine) -> None:
    """Recreate database from scratch (drop all + migrate).

    This is destructive: drops all existing tables, views, and policies,
    then runs all migrations from scratch. For tests only.
    """
    logger.info("Recreating database from scratch...")
    _drop_all(engine)
    upgrade_database(engine)
    logger.info("Database recreation complete")


def _drop_all(engine: Engine) -> None:
    """Drop all database objects by dropping and recreating the public schema."""
    # Check if any of our tables exist
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    our_tables = {table.name for table in Base.metadata.tables.values()}

    if our_tables & existing_tables:
        logger.info("Dropping entire public schema and recreating...")
        with engine.begin() as conn:
            # Drop and recreate public schema (drops everything: tables, views, functions, types, policies)
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
            # Restore default permissions on schema
            conn.execute(text("GRANT ALL ON SCHEMA public TO postgres"))
            conn.execute(text("GRANT ALL ON SCHEMA public TO public"))
        logger.info("Public schema dropped and recreated")
    else:
        logger.debug("No tables to drop")
