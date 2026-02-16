"""Generic single-specimen validation test.

This test is instantiated once per specimen by the specimen_targets macro.
Test parameters (slug, code tar, data YAML) are passed via environment variables.
"""

from __future__ import annotations

import os
from collections.abc import Generator

import pytest
import pytest_bazel
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from bazel_util.runfiles import get_required_path
from props.db.config import DatabaseConfig
from props.db.database import Database
from props.db.models import Snapshot
from props.db.setup import ensure_database_exists
from props.db.sync.sync import SpecimenBundle, sync_all

pytestmark = pytest.mark.integration


@pytest.fixture
def synced_db(postgres_container: PostgresContainer) -> Generator[Database]:
    """Function-scoped database synced with the specimen from bundle artifacts."""
    slug = os.environ["SPECIMEN_SLUG"]
    code_tar_rloc = os.environ["SPECIMEN_CODE_TAR"]
    data_yaml_rloc = os.environ["SPECIMEN_DATA_YAML"]

    # Resolve runfiles paths
    code_tar = get_required_path(f"_main/{code_tar_rloc}")
    data_yaml = get_required_path(f"_main/{data_yaml_rloc}")

    # Create specimen bundle
    bundle = SpecimenBundle(slug=slug, code_tar=code_tar, data_yaml=data_yaml)

    host = postgres_container.get_container_host_ip()
    port = int(postgres_container.get_exposed_port(5432))
    base_config = DatabaseConfig(host=host, port=port, database="postgres", user="postgres", password="postgres")

    db_name = f"props_test_specimen_{slug.replace('/', '_')}"
    ensure_database_exists(base_config, db_name, drop_existing=True)
    test_config = base_config.with_database(db_name)

    postgres_config = base_config.with_database("postgres")
    postgres_engine = create_engine(postgres_config.url, isolation_level="AUTOCOMMIT")

    db = Database(test_config)
    db.recreate()

    # Sync from bundle artifacts (no filesystem needed)
    with db.session() as session:
        sync_all(session, specimen_bundles=[bundle], use_staged=True, collect_errors=True)

    yield db

    db.dispose()
    with postgres_engine.connect() as conn:
        conn.execute(
            text(f"""
                SELECT pg_terminate_backend(pg_stat_activity.pid)
                FROM pg_stat_activity
                WHERE pg_stat_activity.datname = '{db_name}'
                  AND pid <> pg_backend_pid()
            """)
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
    postgres_engine.dispose()


def test_specimen_syncs_successfully(synced_db: Database) -> None:
    """Verify that the specimen syncs without errors."""
    slug = os.environ["SPECIMEN_SLUG"]

    with synced_db.session() as session:
        snapshot = session.query(Snapshot).filter_by(slug=slug).one_or_none()
        assert snapshot is not None, f"Specimen {slug} was not synced"
        assert snapshot.content is not None, f"Specimen {slug} has no content tar"


def test_specimen_has_issues(synced_db: Database) -> None:
    """Verify that the specimen has at least one true positive."""
    slug = os.environ["SPECIMEN_SLUG"]

    with synced_db.session() as session:
        snapshot = session.query(Snapshot).filter_by(slug=slug).one()
        tp_count = len(snapshot.true_positives)

    assert tp_count > 0, f"Specimen {slug} has no true positives"


if __name__ == "__main__":
    pytest_bazel.main()
