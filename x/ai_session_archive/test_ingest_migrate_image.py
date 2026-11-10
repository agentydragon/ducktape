"""The importer image carries and can apply its own Alembic history."""

# gazelle:include_dep //util/testing:postgres_fixtures
import pytest_bazel
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from devinfra.oci.oci import load_oci_image
from util.testing.container_logs import LoggedContainer
from util.testing.postgres import DatabaseNames, create_temporary_database
from x.ai_session_archive import ingest_test_image

pytest_plugins = ["util.testing.postgres_fixtures"]


def test_importer_image_runs_migrations(
    postgres_admin_url: str, postgres_container: PostgresContainer, database_names: DatabaseNames
) -> None:
    with create_temporary_database(postgres_admin_url, database_names()) as database_url:
        # Sharing Postgres's network namespace gives the image the same address
        # as a CNPG service without relying on the test runner's mapped host port.
        container_url = make_url(database_url).set(host="127.0.0.1", port=5432).render_as_string(hide_password=False)
        with LoggedContainer(
            load_oci_image(ingest_test_image.LAYOUT_RLOCATION),
            test_name="archive-importer-migration",
            network_mode=f"container:{postgres_container.get_wrapped_container().id}",
            command=["--migrate-database"],
        ).with_env("DATABASE_URL", container_url) as container:
            assert container.get_wrapped_container().wait(timeout=30)["StatusCode"] == 0

        engine = create_engine(database_url)
        try:
            with engine.connect() as connection:
                assert connection.scalar(text("SELECT to_regclass('public.sessions')")) is not None
                assert connection.scalar(text("SELECT version_num FROM alembic_version_ai_session_archive"))
        finally:
            engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
