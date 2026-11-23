"""Check the upgraded schema and repeat-upgrade stability.

Only a disposable testcontainer database is used; no deployed database is contacted.
"""

from contextlib import ExitStack

from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from props.db.config import DatabaseConfig
from props.db.setup import upgrade_database
from testing.postgres import PUBLIC_RELATION_IDENTITIES, DatabaseNames, create_temporary_database
from testing.postgres_fixtures import dump_schema_for_migration_check

# gazelle:include_dep //testing:postgres_fixtures
# gazelle:include_dep //props/testing/fixtures:db
pytest_plugins = ("testing.postgres_fixtures", "props.testing.fixtures.db")


def test_schema_baseline(
    postgres_container: PostgresContainer, postgres_base_config: DatabaseConfig, database_names: DatabaseNames
) -> None:
    database = database_names()
    admin_url = postgres_base_config.with_database("postgres").url
    with create_temporary_database(admin_url, database) as url, ExitStack() as cleanup:
        engine = create_engine(url)
        cleanup.callback(engine.dispose)
        upgrade_database(engine)
        before = dump_schema_for_migration_check(postgres_container, database)
        (undeclared_outputs_dir() / "props_schema.sql").write_text(before)
        with engine.connect() as conn:
            objects = conn.execute(PUBLIC_RELATION_IDENTITIES).all()
            salt = conn.scalar(text("SELECT salt FROM agent_role_salt WHERE id = 1"))
            assert salt is not None
            assert len(salt) == 32
            assert conn.scalar(text("SELECT relispopulated FROM pg_class WHERE oid = 'examples'::regclass"))
            assert conn.scalar(text("SELECT rolbypassrls FROM pg_roles WHERE rolname = 'evaluator_base'")) is False
            assert conn.scalar(text("SELECT pg_has_role('evaluator', 'evaluator_base', 'MEMBER')"))
        upgrade_database(engine)
        assert dump_schema_for_migration_check(postgres_container, database) == before
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT salt FROM agent_role_salt WHERE id = 1")) == salt
            assert conn.execute(PUBLIC_RELATION_IDENTITIES).all() == objects
