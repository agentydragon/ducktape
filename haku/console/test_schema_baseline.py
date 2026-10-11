"""Upgrade-at-head leaves the migrated schema untouched.

This only uses the isolated pgvector testcontainer; it never reads a deployed DB.
"""

import re

import pytest_bazel
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from haku.console.database_migrate import apply_migrations, sync_database_url
from util.testing.undeclared_outputs import undeclared_outputs_dir


def _schema(container: PostgresContainer, database: str) -> str:
    result = container.get_wrapped_container().exec_run(
        ["pg_dump", "-U", "postgres", "--schema-only", "--no-owner", "--exclude-table=public.alembic_version", database]
    )
    assert result.exit_code == 0, result.output.decode()
    # Random psql restriction keys are transport framing, not schema.
    return re.sub(r"^\\(?:un)?restrict .*\n", "", result.output.decode(), flags=re.MULTILINE)


def test_reapply_at_head_preserves_schema(postgres_container: PostgresContainer, db_url: str) -> None:
    apply_migrations(db_url)
    database = make_url(db_url).database
    assert database is not None
    before = _schema(postgres_container, database)
    (undeclared_outputs_dir() / "haku_console_schema.sql").write_text(before)
    engine = create_engine(sync_database_url(db_url))
    try:
        with engine.connect() as conn:
            objects = conn.execute(
                text("SELECT oid, relname FROM pg_class WHERE relnamespace = 'public'::regnamespace ORDER BY oid")
            ).all()
        apply_migrations(db_url)
        assert _schema(postgres_container, database) == before
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT oid, relname FROM pg_class WHERE relnamespace = 'public'::regnamespace ORDER BY oid")
                ).all()
                == objects
            )
    finally:
        engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
