"""The Gateway schema migration drops its retired admission queue and keeps the Session registry."""

import uuid

import pytest_bazel
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, inspect, text

from agentplane.runner_gateway.commands.database_migrate import RUNNER

# The migration is inspected through psycopg, which SQLAlchemy loads from the URL scheme.
# gazelle:include_dep @pypi//psycopg


def test_command_submission_drop_preserves_session_registry_rows(db_url: str) -> None:
    engine = create_engine(RUNNER.sync_url(db_url))
    sessions = [
        (uuid.uuid4(), "agentplane-testing", "worker-a", uuid.uuid4()),
        (uuid.uuid4(), "agentplane-staging", "imported-session", None),
    ]
    removed_session_id = sessions[0][0]
    command_id = "legacy-command"
    try:
        # Build the legacy state at 0009 before seeding representative queue and registry data.
        with engine.begin() as connection:
            connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": RUNNER.lock_key})
            config = AlembicConfig()
            config.set_main_option("script_location", str(RUNNER.migrations_dir))
            config.attributes["connection"] = connection
            config.attributes["target_metadata"] = RUNNER.metadata
            alembic_command.downgrade(config, "0009_session_open_columns")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO session (id, sandbox_namespace, sandbox_name, sandbox_uid) "
                    "VALUES (:id, :namespace, :name, :uid)"
                ),
                [
                    {"id": session_id, "namespace": namespace, "name": name, "uid": sandbox_uid}
                    for session_id, namespace, name, sandbox_uid in sessions
                ],
            )
            inserted = connection.scalar(
                text(
                    "INSERT INTO command_submission "
                    "(session_id, command_id, runner_command, caller_namespace, caller_name, state, admission) "
                    "VALUES (:session_id, :command_id, :command, 'agentplane-testing', 'dispatcher', 'admitted', "
                    ":admission) RETURNING command_id"
                ),
                {
                    "session_id": removed_session_id,
                    "command_id": command_id,
                    "command": b"legacy command payload",
                    "admission": b"legacy admission receipt",
                },
            )
            assert inserted == command_id

        RUNNER.apply(db_url)

        with engine.connect() as connection:
            inspector = inspect(connection)
            tables = set(inspector.get_table_names())
            assert "command_submission" not in tables
            assert tables == set(RUNNER.metadata.tables) | {RUNNER.version_table or "alembic_version"}
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0010_command_submission_drop"
            rows = connection.execute(
                text("SELECT id, sandbox_namespace, sandbox_name, sandbox_uid FROM session")
            ).all()
            assert set(rows) == set(sessions)
    finally:
        engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
