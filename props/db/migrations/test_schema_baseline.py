"""Check the upgraded schema and repeat-upgrade stability.

Only a disposable testcontainer database is used; no deployed database is contacted.
"""

from contextlib import ExitStack

import pytest_bazel
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from props.db.config import DatabaseConfig
from props.db.setup import upgrade_database
from testing.postgres import DatabaseNames, create_temporary_database
from testing.postgres_fixtures import dump_migrated_schema

# gazelle:include_dep //testing:postgres_fixtures
# gazelle:include_dep //props/testing/fixtures:db
pytest_plugins = ("testing.postgres_fixtures", "props.testing.fixtures.db")

_PUBLIC_OBJECTS = text("SELECT oid, relname FROM pg_class WHERE relnamespace = 'public'::regnamespace ORDER BY oid")


def test_schema_baseline(
    postgres_container: PostgresContainer, postgres_base_config: DatabaseConfig, database_names: DatabaseNames
) -> None:
    database = database_names()
    admin_url = postgres_base_config.with_database("postgres").url
    with create_temporary_database(admin_url, database) as url, ExitStack() as cleanup:
        engine = create_engine(url)
        cleanup.callback(engine.dispose)
        upgrade_database(engine)
        before = dump_migrated_schema(postgres_container, database)
        (undeclared_outputs_dir() / "props_schema.sql").write_text(before)
        with engine.connect() as conn:
            objects = conn.execute(_PUBLIC_OBJECTS).all()
            salt = conn.scalar(text("SELECT salt FROM agent_role_salt WHERE id = 1"))
            assert salt is not None
            assert len(salt) == 32
            assert conn.scalar(text("SELECT relispopulated FROM pg_class WHERE oid = 'examples'::regclass"))
            assert conn.scalar(text("SELECT rolbypassrls FROM pg_roles WHERE rolname = 'evaluator_base'")) is False
            assert conn.scalar(text("SELECT pg_has_role('evaluator', 'evaluator_base', 'MEMBER')"))
        upgrade_database(engine)
        assert dump_migrated_schema(postgres_container, database) == before
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT salt FROM agent_role_salt WHERE id = 1")) == salt
            assert conn.execute(_PUBLIC_OBJECTS).all() == objects


if __name__ == "__main__":
    pytest_bazel.main()
