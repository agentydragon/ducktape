"""Integration checks for migrated PostgreSQL database templates."""

import pytest
import pytest_bazel
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from util.testing.postgres import PostgresDatabaseTemplate

# SQLAlchemy loads this driver from the explicit URL scheme below.
# gazelle:include_dep @pypi//psycopg
# pytest loads the shared container fixture as a plugin.
# gazelle:include_dep //util/testing:postgres_fixtures

pytest_plugins = ["util.testing.postgres_fixtures"]


@pytest.fixture(scope="module")
def admin_url(postgres_container: PostgresContainer) -> str:
    return (
        f"postgresql+psycopg://postgres:postgres@{postgres_container.get_container_host_ip()}"
        f":{postgres_container.get_exposed_port(5432)}/postgres?application_name=postgres-template-helper"
    )


def _database_exists(admin_url: str, db_name: str) -> bool:
    engine = create_engine(admin_url)
    try:
        with engine.connect() as connection:
            return bool(
                connection.execute(
                    text("SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = :name)"), {"name": db_name}
                ).scalar_one()
            )
    finally:
        engine.dispose()


def test_template_clones_keep_schema_extensions_and_isolate_writes(admin_url: str) -> None:
    with PostgresDatabaseTemplate(admin_url, "template_helper_test", extensions=("pgcrypto",)) as template:
        template_url = make_url(template.url)
        assert template_url.drivername == "postgresql+psycopg"
        assert template_url.query["application_name"] == "postgres-template-helper"
        engine = create_engine(template.url)
        try:
            with engine.begin() as connection:
                connection.execute(text("CREATE TABLE template_data (value text NOT NULL)"))
                connection.execute(text("INSERT INTO template_data VALUES ('seed')"))
        finally:
            engine.dispose()

        callback_names: list[str] = []

        def before_drop(db_name: str) -> None:
            assert _database_exists(admin_url, db_name)
            callback_names.append(db_name)

        with template.clone("template_helper_clone_one", before_drop=before_drop) as clone_one_url:
            clone_url = make_url(clone_one_url)
            assert clone_url.drivername == "postgresql+psycopg"
            assert clone_url.query["application_name"] == "postgres-template-helper"
            engine = create_engine(clone_one_url)
            try:
                with engine.begin() as connection:
                    assert connection.execute(text("SELECT value FROM template_data")).scalar_one() == "seed"
                    assert connection.execute(
                        text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pgcrypto')")
                    ).scalar_one()
                    connection.execute(text("INSERT INTO template_data VALUES ('clone one')"))
            finally:
                engine.dispose()

        assert callback_names == ["template_helper_clone_one"]
        assert not _database_exists(admin_url, "template_helper_clone_one")

        with template.clone("template_helper_clone_two") as clone_two_url:
            engine = create_engine(clone_two_url)
            try:
                with engine.connect() as connection:
                    values = connection.execute(text("SELECT value FROM template_data ORDER BY value")).scalars().all()
            finally:
                engine.dispose()
            assert values == ["seed"]

        with (
            pytest.raises(RuntimeError, match="failure from the test body"),
            template.clone("template_helper_clone_failure"),
        ):
            raise RuntimeError("failure from the test body")
        assert not _database_exists(admin_url, "template_helper_clone_failure")

    assert not _database_exists(admin_url, "template_helper_test")


if __name__ == "__main__":
    pytest_bazel.main()
