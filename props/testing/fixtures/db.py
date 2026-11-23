"""Database fixtures for props tests.

Uses Testcontainers for hermetic PostgreSQL instances. Each test session gets
a fresh PostgreSQL container, and each test gets its own isolated database.
"""

import os
from collections.abc import Generator
from contextlib import ExitStack

import pytest
from python.runfiles import Runfiles
from sqlalchemy import Engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer

from openai_utils.model_metadata import MODEL_METADATA
from props.config import CustomModelConfig, PropsConfig, UpstreamConfig
from props.db.config import DatabaseConfig
from props.db.database import Database
from props.db.sync.model_metadata import sync_model_metadata_with_session
from props.db.sync.sync import SpecimenBundle, refresh_examples_matview, sync_specimen
from testing.postgres import DatabaseNames, PostgresDatabaseTemplate


def _test_model_config() -> PropsConfig:
    """Explicitly route every test model through the test-only fake upstream."""
    return PropsConfig(
        backend_url="http://props-test:8000",
        agent_env={},
        upstreams={"test": UpstreamConfig(url="http://props-test-upstream:8000/v1", api_key_env="TEST_API_KEY")},
        models=[
            CustomModelConfig(name=model_id, upstream="test", upstream_model=model_id, **metadata.model_dump())
            for model_id, metadata in MODEL_METADATA.items()
        ],
    )


@pytest.fixture(scope="session")
def postgres_base_config(postgres_container: PostgresContainer) -> DatabaseConfig:
    """Session-scoped base database config from the testcontainer.

    Provides connection parameters for the containerized PostgreSQL instance.
    Agent containers reach postgres via host.docker.internal (same mapped port).
    """
    host = postgres_container.get_container_host_ip()
    port = int(postgres_container.get_exposed_port(5432))

    return DatabaseConfig(host=host, port=port, database="postgres", user="postgres", password="postgres")


@pytest.fixture(scope="session")
def postgres_template(postgres_base_config: DatabaseConfig) -> Generator[PostgresDatabaseTemplate]:
    """Migrate once; ordinary test databases copy this untouched schema."""
    with PostgresDatabaseTemplate(postgres_base_config.url, "props_test_template") as template:
        with ExitStack() as cleanup:
            database = Database(postgres_base_config.with_database(template.template_name))
            cleanup.callback(database.dispose)
            database.recreate()
        yield template


@pytest.fixture
def db(
    request: pytest.FixtureRequest,
    postgres_base_config: DatabaseConfig,
    postgres_template: PostgresDatabaseTemplate,
    database_names: DatabaseNames,
) -> Generator[Database]:
    """Clone the migrated schema into a fresh writable database for this test."""
    with ExitStack() as cleanup:
        url = postgres_template.create_clone(database_names, cleanup=cleanup)
        db_name = make_url(url).database
        assert db_name is not None
        test_config = postgres_base_config.with_database(db_name)
        database = Database(test_config)
        try:
            yield database
        finally:
            database.dispose()
        if request.config.getoption("--keep-db") or os.environ.get("KEEP_TEST_DB") == "1":
            print(f"\n\n=== KEEPING TEST DATABASE: {db_name} ===")
            print(f"Database config: {test_config}")
            print(f"Connect with: psql {test_config.url}")
            cleanup.pop_all()


@pytest.fixture
def engine(db: Database) -> Engine:
    """Engine from the db fixture."""
    return db.engine


def _sync_test_fixtures(db: Database) -> None:
    """Sync test fixtures to the current database using bundle workflow."""
    fixture_slugs = ["test-fixtures/test1", "test-fixtures/train1", "test-fixtures/valid1", "test-fixtures/valid2"]

    runfiles_root = Runfiles.CreateOrRaise().root()
    with db.session() as session:
        sync_model_metadata_with_session(session, _test_model_config())
        for slug in fixture_slugs:
            base_path = f"props/testing/fixtures/testdata/specimens/{slug}"
            code_tar = runfiles_root / f"ducktape/{base_path}/specimen_code.tar"
            data_yaml = runfiles_root / f"ducktape/{base_path}/specimen_data.yaml"
            bundle = SpecimenBundle.from_paths(code_tar, data_yaml)
            sync_specimen(session, bundle)
        session.commit()
        refresh_examples_matview(session)


@pytest.fixture
def synced_db(db: Database) -> Database:
    """Test database with test fixture specimens synced."""
    _sync_test_fixtures(db)
    return db


@pytest.fixture
def synced_test_session(synced_db: Database) -> Generator[Session]:
    """Function-scoped session over synced test database (read-write)."""
    with synced_db.session() as session:
        yield session
