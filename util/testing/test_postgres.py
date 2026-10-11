"""Integration checks for migrated PostgreSQL database templates."""

import pytest
import pytest_bazel
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from testcontainers.postgres import PostgresContainer

from util.testing.postgres import DatabaseClones, DatabaseNames, PostgresDatabaseTemplate, create_temporary_database

# SQLAlchemy loads this driver from the explicit URL scheme below.
# gazelle:include_dep @pypi//psycopg
# pytest loads the shared container fixture as a plugin.
# gazelle:include_dep //util/testing:postgres_fixtures

pytest_plugins = ["util.testing.postgres_fixtures"]


def test_database_names_are_safe_unique_and_deterministic() -> None:
    node_id = "tests/" + ("非常長" * 30) + "::test_name[param]"
    names = DatabaseNames(node_id)
    first, second = names(), names()

    assert first != second
    assert DatabaseNames(node_id)() == first
    assert len(first) <= 63
    assert len(second) <= 63
    assert first.replace("_", "").isalnum()
    assert second.replace("_", "").isalnum()
    assert first.isascii()
    assert second.isascii()


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


def test_temporary_database_drops_after_body_failure(admin_url: str) -> None:
    name = "template_helper_temporary_database"

    def fail_inside_database() -> None:
        with create_temporary_database(admin_url, name) as url:
            engine = create_engine(url)
            try:
                with engine.connect() as connection:
                    assert connection.execute(text("SELECT current_database()")).scalar_one() == name
            finally:
                engine.dispose()
            raise RuntimeError("failure from the test body")

    with pytest.raises(RuntimeError, match="failure from the test body"):
        fail_inside_database()
    assert not _database_exists(admin_url, name)


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

        with template.clone("template_helper_clone_one") as clone_one_url:
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


def test_database_clones_allocate_isolated_databases_and_clean_up(admin_url: str) -> None:
    node_id = "test_database_clones_allocate_isolated_databases_and_clean_up"
    clone_names: list[str] = []
    with PostgresDatabaseTemplate(admin_url, "template_helper_owner_test") as template:
        engine = create_engine(template.url)
        try:
            with engine.begin() as connection:
                connection.execute(text("CREATE TABLE template_data (value text NOT NULL)"))
                connection.execute(text("INSERT INTO template_data VALUES ('seed')"))
        finally:
            engine.dispose()

        def exercise_clones() -> None:
            with DatabaseClones(DatabaseNames(node_id)) as clones:
                first_url = clones.clone(template, drivername="postgresql+psycopg")
                second_url = clones.clone(template, drivername="postgresql+psycopg")
                async_url = clones.clone(template, drivername="postgresql+asyncpg")
                assert make_url(first_url).query["application_name"] == "postgres-template-helper"
                assert make_url(second_url).query["application_name"] == "postgres-template-helper"
                assert make_url(async_url).drivername == "postgresql+asyncpg"
                assert make_url(async_url).query["application_name"] == "postgres-template-helper"
                for url in (first_url, second_url, async_url):
                    name = make_url(url).database
                    assert name is not None
                    clone_names.append(name)
                assert len(set(clone_names)) == 3

                engine = create_engine(first_url)
                try:
                    with engine.begin() as connection:
                        connection.execute(text("INSERT INTO template_data VALUES ('first clone')"))
                finally:
                    engine.dispose()

                engine = create_engine(second_url)
                try:
                    with engine.connect() as connection:
                        values = (
                            connection.execute(text("SELECT value FROM template_data ORDER BY value")).scalars().all()
                        )
                finally:
                    engine.dispose()
                assert values == ["seed"]
                raise RuntimeError("failure from the test body")

        with pytest.raises(RuntimeError, match="failure from the test body"):
            exercise_clones()

        assert all(not _database_exists(admin_url, name) for name in clone_names)
    assert not _database_exists(admin_url, "template_helper_owner_test")


def test_failed_extension_setup_drops_the_unentered_template(admin_url: str) -> None:
    name = "template_helper_missing_extension"
    with (
        pytest.raises(DBAPIError, match=r"extension .* is not available"),
        PostgresDatabaseTemplate(admin_url, name, extensions=("pgcrypto", "missing_test_extension")),
    ):
        pytest.fail("A template with an unavailable extension must not open")
    assert not _database_exists(admin_url, name)


def test_failed_duplicate_creation_keeps_the_existing_database(admin_url: str) -> None:
    name = "template_helper_existing_database"
    with PostgresDatabaseTemplate(admin_url, name):
        with pytest.raises(DBAPIError, match="already exists"), PostgresDatabaseTemplate(admin_url, name):
            pytest.fail("A duplicate database must not open")
        assert _database_exists(admin_url, name)


if __name__ == "__main__":
    pytest_bazel.main()
