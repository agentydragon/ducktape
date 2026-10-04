"""PG 18 restricts search_path during CREATE MATERIALIZED VIEW, which the materialize_examples migration works
around (root cause: props/debug/pg18_matview_inlining.md).
"""

from collections.abc import Generator

import pytest
import pytest_bazel
from sqlalchemy import Engine, create_engine, text

from props.db.config import DatabaseConfig
from util.testing.postgres import force_drop_database_sync


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


if __name__ == "__main__":
    pytest_bazel.main()
