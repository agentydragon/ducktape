"""Check the upgraded schema and repeat-upgrade stability.

Only a disposable testcontainer database is used; no deployed database is contacted.
"""

import re

import pytest_bazel
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from props.db.config import DatabaseConfig
from props.db.setup import upgrade_database
from util.testing.postgres import DatabaseNames, create_temporary_database
from util.testing.undeclared_outputs import undeclared_outputs_dir


def _schema(container: PostgresContainer, database: str) -> str:
    result = container.get_wrapped_container().exec_run(
        ["pg_dump", "-U", "postgres", "--schema-only", "--no-owner", "--exclude-table=public.alembic_version", database]
    )
    assert result.exit_code == 0, result.output.decode()
    # pg_dump 18 emits random psql restriction keys, which are not schema.
    return re.sub(r"^\\(?:un)?restrict .*\n", "", result.output.decode(), flags=re.MULTILINE)


def test_schema_baseline(
    postgres_container: PostgresContainer, postgres_base_config: DatabaseConfig, database_names: DatabaseNames
) -> None:
    database = database_names()
    admin_url = postgres_base_config.with_database("postgres").url
    with create_temporary_database(admin_url, database) as url:
        engine = create_engine(url)
        try:
            upgrade_database(engine)
            before = _schema(postgres_container, database)
            (undeclared_outputs_dir() / "props_schema.sql").write_text(before)
            with engine.connect() as conn:
                objects = conn.execute(
                    text("SELECT oid, relname FROM pg_class WHERE relnamespace = 'public'::regnamespace ORDER BY oid")
                ).all()
            with engine.connect() as conn:
                salt = conn.scalar(text("SELECT salt FROM agent_role_salt WHERE id = 1"))
                assert salt is not None
                assert len(salt) == 32
                assert conn.scalar(text("SELECT relispopulated FROM pg_class WHERE oid = 'examples'::regclass"))
                assert conn.scalar(text("SELECT rolbypassrls FROM pg_roles WHERE rolname = 'evaluator_base'")) is False
                assert conn.scalar(text("SELECT pg_has_role('evaluator', 'evaluator_base', 'MEMBER')"))
            upgrade_database(engine)
            assert _schema(postgres_container, database) == before
            with engine.connect() as conn:
                assert conn.scalar(text("SELECT salt FROM agent_role_salt WHERE id = 1")) == salt
            with engine.connect() as conn:
                assert (
                    conn.execute(
                        text(
                            "SELECT oid, relname FROM pg_class WHERE relnamespace = 'public'::regnamespace ORDER BY oid"
                        )
                    ).all()
                    == objects
                )
        finally:
            engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
