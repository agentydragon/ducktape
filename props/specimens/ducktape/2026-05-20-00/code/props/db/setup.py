"""Database setup and initialization (RLS policies, views).

Extracted from database.py to separate concerns:
- database.py: Database class (owns engine + session factory)
- setup.py: Database schema and security setup (recreate_database, RLS, views)
"""

from __future__ import annotations

import logging
import os
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
def ensure_database_exists(base_config: DatabaseConfig, database_name: str) -> None:
    """Ensure a PostgreSQL database exists, creating it if absent."""
    postgres_config = base_config.with_database("postgres")
    engine = create_engine(postgres_config.url, isolation_level="AUTOCOMMIT")

    with engine.connect() as conn:
        result = conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :dbname"), {"dbname": database_name})

        if not result.fetchone():
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


def ensure_evaluator_role(db_config: DatabaseConfig) -> None:
    """Sync evaluator Postgres role password with PROPS_EVALUATOR_PASSWORD env var.

    The evaluator_base role and evaluator login user are created by the migration.
    This handles password updates on re-deploy (migrations run only once).
    No-op if PROPS_EVALUATOR_PASSWORD is not set.
    """
    password = os.environ.get("PROPS_EVALUATOR_PASSWORD")
    if not password:
        return
    engine = create_engine(db_config.url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(text("ALTER ROLE evaluator PASSWORD :pw"), {"pw": password})
        logger.info("Evaluator role password synced")
    finally:
        engine.dispose()


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
