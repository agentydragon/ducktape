"""Retiring the attachment snapshot preserves durable Session openings."""

import uuid

import pytest_bazel
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, text

from agentplane.session_dispatcher.database_migrate import RUNNER

# gazelle:include_dep @pypi//psycopg


def test_drop_snapshot_preserves_opening(db_url: str) -> None:
    engine = create_engine(RUNNER.sync_url(db_url))
    try:
        with engine.begin() as connection:
            config = AlembicConfig()
            config.set_main_option("script_location", str(RUNNER.migrations_dir))
            config.attributes["connection"] = connection
            config.attributes["target_metadata"] = RUNNER.metadata
            alembic_command.downgrade(config, "0006_wake_deadline")
            connection.execute(
                text(
                    "INSERT INTO session_opening "
                    "(session_id, sandbox_name, sandbox_uid, spec, setup_script, state, reason, "
                    "attached, created_at, updated_at) "
                    "VALUES (:id, 'worker', :uid, :spec, 'echo ready', 'pending', 'queued', "
                    ":attached, now(), now())"
                ),
                {"id": str(uuid.uuid4()), "uid": uuid.uuid4(), "spec": b"spec", "attached": b"snapshot"},
            )
            preserved = text(
                "SELECT session_id, sandbox_name, sandbox_uid, spec, setup_script, state, reason, "
                "created_at, updated_at, wake_eligible, wake_requested_at FROM session_opening"
            )
            before = connection.execute(preserved).all()

        RUNNER.apply(db_url)

        with engine.connect() as connection:
            assert connection.execute(preserved).all() == before
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0007_drop_opening_attached"
    finally:
        engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
