"""Tests for the materialize_examples migration.

Covers the PG 18 search_path restriction during CREATE MATERIALIZED VIEW
(see debug/pg18-matview-inlining.md for full root cause analysis).
"""

from collections.abc import Generator
from pathlib import Path

import pytest
import pytest_bazel
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text

from props.db.config import DatabaseConfig
from util.testing.postgres import force_drop_database_sync

MIGRATIONS_DIR = str(Path(__file__).parent)


@pytest.fixture
def blank_engine(postgres_base_config: DatabaseConfig) -> Generator[Engine]:
    """Engine for a fresh database with no migrations applied."""
    db_name = "props_test_matview_migration"
    admin_url = postgres_base_config.with_database("postgres").url
    force_drop_database_sync(admin_url, db_name)
    pg_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with pg_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    pg_engine.dispose()

    engine = create_engine(postgres_base_config.with_database(db_name).url)
    yield engine
    engine.dispose()

    force_drop_database_sync(admin_url, db_name)


def _make_alembic_config(engine: Engine) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", MIGRATIONS_DIR)
    cfg.set_main_option("sqlalchemy.url", str(engine.url))
    return cfg


def test_pg18_unqualified_table_in_function_fails_in_matview(blank_engine: Engine) -> None:
    """PG 18 restricts search_path to 'pg_catalog, pg_temp' during matview creation.

    This causes SQL function inlining to fail when the function body uses
    unqualified table references. Commit 4b74ebf726 made CREATE MATERIALIZED
    VIEW use the REFRESH code path, which calls RestrictSearchPath().
    """
    with blank_engine.begin() as conn:
        conn.execute(text("CREATE TABLE items (id int PRIMARY KEY, val text)"))
        conn.execute(
            text("""
            CREATE FUNCTION get_val(p_id int) RETURNS text
            LANGUAGE sql STABLE AS $$
                SELECT val FROM items WHERE id = p_id
            $$
        """)
        )
        with pytest.raises(Exception, match="does not exist"):
            conn.execute(text("CREATE MATERIALIZED VIEW mv AS SELECT get_val(1) AS result"))


def test_set_search_path_fixes_matview_creation(blank_engine: Engine) -> None:
    """SET search_path on the function prevents inlining, avoiding the error."""
    with blank_engine.begin() as conn:
        conn.execute(text("CREATE TABLE items (id int PRIMARY KEY, val text)"))
        conn.execute(
            text("""
            CREATE FUNCTION get_val_sp(p_id int) RETURNS text
            LANGUAGE sql STABLE
            SET search_path = public
            AS $$
                SELECT val FROM items WHERE id = p_id
            $$
        """)
        )
        conn.execute(text("CREATE MATERIALIZED VIEW mv AS SELECT get_val_sp(1) AS result"))


def test_materialize_examples_migration(blank_engine: Engine) -> None:
    """The full migration chain succeeds: initial schema → matview conversion."""
    cfg = _make_alembic_config(blank_engine)

    with blank_engine.connect() as conn:
        # Pass connection to env.py via config attributes (avoids DatabaseConfig env lookup)
        cfg.attributes["connection"] = conn

        # Run up to the migration before materialize_examples
        command.upgrade(cfg, "20260223000000")

        # Verify the function and table exist
        fn_exists = conn.execute(
            text("SELECT 1 FROM pg_proc WHERE proname = 'is_tp_in_expected_recall_scope'")
        ).fetchone()
        assert fn_exists, "Function should exist after initial migration"

        tbl_exists = conn.execute(
            text("SELECT 1 FROM pg_class WHERE relname = 'critic_scopes_expected_to_recall'")
        ).fetchone()
        assert tbl_exists, "Table should exist after initial migration"

        # Apply the materialize_examples migration (the one that was failing)
        command.upgrade(cfg, "20260224000000")

        # Verify the materialized view was created
        is_matview = conn.execute(text("SELECT relkind FROM pg_class WHERE relname = 'examples'")).scalar()
        assert is_matview == "m", f"Expected matview (relkind='m'), got {is_matview!r}"


if __name__ == "__main__":
    pytest_bazel.main()
