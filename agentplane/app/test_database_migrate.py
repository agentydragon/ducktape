"""The migration history against a real PostgreSQL: it refuses a database that differs from the models,
re-running it is a no-op, and a database already stamped at an earlier revision is carried to the models."""

import os
import subprocess
import sys
import uuid

import pytest
import pytest_bazel
from sqlalchemy import Connection, create_engine, inspect, text
from sqlalchemy.exc import DBAPIError

from agentplane.app.database_migrate import RUNNER
from agentplane.history_service.fold import models as fold_models

# The migrated database is read back over psycopg, which SQLAlchemy loads from the URL scheme.
# gazelle:include_dep @pypi//psycopg


def _execute(db_url: str, statement: str) -> None:
    engine = create_engine(RUNNER.sync_url(db_url))
    try:
        with engine.begin() as connection:
            connection.execute(text(statement))
    finally:
        engine.dispose()


def _restore_event_log(connection: Connection) -> None:
    """The `event_log` table that migrations before `0035_event_log_into_thread` expect."""
    connection.execute(text("CREATE TABLE event_log (id uuid PRIMARY KEY, created_at timestamptz NOT NULL)"))
    connection.execute(text("ALTER TABLE thread DROP COLUMN created_at"))
    connection.execute(
        text(
            "ALTER TABLE thread ADD CONSTRAINT thread_id_fkey FOREIGN KEY (id) REFERENCES event_log(id) "
            "ON DELETE CASCADE"
        )
    )


def _restore_raw_history_schema(connection: Connection) -> None:
    """Only historical migration tests need the retired schema; never runtime fixtures."""
    connection.execute(
        text(
            "CREATE TABLE event (thread_id uuid REFERENCES event_log(id), cursor bigint, "
            "at timestamptz NOT NULL, kind text NOT NULL, payload json NOT NULL, "
            "PRIMARY KEY (thread_id, cursor))"
        )
    )
    connection.execute(
        text(
            "CREATE TABLE feed_state (thread_id uuid PRIMARY KEY REFERENCES event_log(id), "
            'attached jsonb NOT NULL, "end" jsonb)'
        )
    )


def _restore_sandbox(connection: Connection) -> None:
    """The `event_log.sandbox` column that migrations before `0033_event_log_sandbox_drop` expect."""
    connection.execute(text("ALTER TABLE event_log ADD COLUMN sandbox text NOT NULL"))


def _restore_launch_copies(connection: Connection) -> None:
    """The `event_log` launch copies that migrations before `0029_event_log_copies_drop` expect."""
    connection.execute(
        text(
            "ALTER TABLE event_log ADD COLUMN harness varchar(14), ADD COLUMN model text, ADD COLUMN cwd text, "
            "ADD COLUMN last_model_activity_at timestamptz"
        )
    )


def _restore_app_projection(connection: Connection) -> None:
    """The fold tables that migrations before `0032_app_projection_drop` expect."""
    fold_models.Base.metadata.create_all(connection)
    # `0030_thread_resume` copies the app's resume marker, which the fold model no longer has.
    connection.execute(text("ALTER TABLE thread_history_summary ADD COLUMN resumed_after_cursor bigint"))


def test_raw_history_retirement_preserves_current_state(db_url: str) -> None:
    engine = create_engine(RUNNER.sync_url(db_url))
    thread, token = uuid.uuid4(), uuid.uuid4()
    try:
        with engine.begin() as connection:
            _restore_event_log(connection)
            _restore_sandbox(connection)
            _restore_raw_history_schema(connection)
            connection.execute(text("ALTER TABLE event_log ADD COLUMN raw_ingestion_fenced_at_cursor bigint"))
            connection.execute(
                text(
                    "CREATE FUNCTION reject_fenced_app_ingestion() RETURNS trigger "
                    "LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$"
                )
            )
            for table in ("event", "feed_state"):
                connection.execute(
                    text(
                        f"CREATE TRIGGER reject_fenced_app_ingestion BEFORE INSERT ON {table} "
                        "FOR EACH ROW EXECUTE FUNCTION reject_fenced_app_ingestion()"
                    )
                )
            _restore_app_projection(connection)
            _restore_launch_copies(connection)
            connection.execute(text("UPDATE alembic_version_app SET version_num = '0023_drop_app_runner_locator'"))
            connection.execute(
                text(
                    "INSERT INTO event_log (id, sandbox, harness, model, cwd, created_at, "
                    "raw_ingestion_fenced_at_cursor) VALUES "
                    "(:id, 'retained', 'HARNESS_CODEX', 'model', '/', now(), 17)"
                ),
                {"id": thread},
            )
            connection.execute(
                text("INSERT INTO event VALUES (:id, 17, now(), 'text_delta', '{}'::json)"), {"id": thread}
            )
            connection.execute(text("INSERT INTO feed_state VALUES (:id, '{}'::jsonb, NULL)"), {"id": thread})
            connection.execute(
                text("INSERT INTO thread_checkpoint VALUES (:id, 'source', 'epoch', 17)"), {"id": thread}
            )
            connection.execute(text("INSERT INTO thread (id, name) VALUES (:id, 'operator name')"), {"id": thread})
            connection.execute(text("INSERT INTO thread_history_summary (thread_id) VALUES (:id)"), {"id": thread})
            connection.execute(
                text("INSERT INTO session_projection_lease VALUES (:id, :token, '2030-01-01T00:00:00Z')"),
                {"id": thread, "token": token},
            )
        RUNNER.apply(db_url)
        RUNNER.apply(db_url)
        with engine.connect() as connection:
            assert not inspect(connection).has_table("event")
            assert not inspect(connection).has_table("feed_state")
            assert connection.scalar(text("SELECT to_regprocedure('reject_fenced_app_ingestion()')")) is None
            assert connection.scalar(text("SELECT name FROM thread WHERE id = :id"), {"id": thread}) == "operator name"
            assert not set(fold_models.Base.metadata.tables) & set(inspect(connection).get_table_names())
    finally:
        engine.dispose()


def test_raw_retirement_refuses_unexpected_dependencies(db_url: str) -> None:
    engine = create_engine(RUNNER.sync_url(db_url))
    try:
        with engine.begin() as connection:
            _restore_event_log(connection)
            _restore_raw_history_schema(connection)
            connection.execute(
                text(
                    "CREATE TABLE unexpected_raw_reader (thread_id uuid, cursor bigint, "
                    "FOREIGN KEY (thread_id, cursor) REFERENCES event(thread_id, cursor))"
                )
            )
            connection.execute(text("UPDATE alembic_version_app SET version_num = '0023_drop_app_runner_locator'"))
        with pytest.raises(DBAPIError, match="depend"):
            RUNNER.apply(db_url)
        with engine.connect() as connection:
            assert inspect(connection).has_table("event")
            assert inspect(connection).has_table("unexpected_raw_reader")
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version_app")) == "0023_drop_app_runner_locator"
            )
    finally:
        engine.dispose()


def test_a_database_that_differs_from_the_models_fails_to_migrate(db_url: str) -> None:
    """A database that applied a since-edited migration is stamped at head and still differs; the
    init container has to stop the rollout instead of letting the app write to the wrong schema."""
    _execute(db_url, "ALTER TABLE thread ALTER COLUMN name TYPE varchar(80)")
    with pytest.raises(RuntimeError, match="thread"):
        RUNNER.apply(db_url)


def test_an_index_only_the_database_has_is_drift(db_url: str) -> None:
    _execute(db_url, "CREATE INDEX index_only_in_test_database ON thread (created_at)")
    with pytest.raises(RuntimeError, match="index_only_in_test_database"):
        RUNNER.apply(db_url)


def test_a_second_run_over_a_migrated_database_changes_nothing(db_url: str) -> None:
    """Every replica's init container runs this against the same database, on every rollout."""
    RUNNER.apply(db_url)


def test_drop_locator_preserves_identity(db_url: str) -> None:
    engine = create_engine(RUNNER.sync_url(db_url))
    thread = uuid.uuid4()
    try:
        with engine.begin() as connection:
            _restore_event_log(connection)
            _restore_sandbox(connection)
            connection.execute(text("ALTER TABLE event_log ADD COLUMN session_id text"))
            connection.execute(text("ALTER TABLE event_log ADD UNIQUE (sandbox, session_id)"))
            _restore_app_projection(connection)
            _restore_launch_copies(connection)
            connection.execute(text("UPDATE alembic_version_app SET version_num = '0022_session_projection_lease'"))
            connection.execute(
                text(
                    "INSERT INTO event_log (id, sandbox, session_id, harness, model, cwd, created_at) "
                    "VALUES (:id, 'retained', 'private-locator', 'HARNESS_CODEX', 'model', '/', now())"
                ),
                {"id": thread},
            )
        RUNNER.apply(db_url)
        RUNNER.apply(db_url)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT id FROM thread WHERE id = :id"), {"id": thread}) == thread
    finally:
        engine.dispose()


def test_event_log_folds_into_thread(db_url: str) -> None:
    engine = create_engine(RUNNER.sync_url(db_url))
    unnamed, named = uuid.uuid4(), uuid.uuid4()
    try:
        with engine.begin() as connection:
            _restore_event_log(connection)
            connection.execute(text("UPDATE alembic_version_app SET version_num = '0034_thread_resume_drop'"))
            connection.execute(
                text(
                    "INSERT INTO event_log (id, created_at) VALUES "
                    "(:unnamed, '2026-01-01T00:00:00Z'), (:named, '2026-02-01T00:00:00Z')"
                ),
                {"unnamed": unnamed, "named": named},
            )
            connection.execute(
                text("INSERT INTO thread (id, name, archived) VALUES (:id, 'operator name', true)"), {"id": named}
            )
        RUNNER.apply(db_url)
        RUNNER.apply(db_url)
        with engine.connect() as connection:
            assert not inspect(connection).has_table("event_log")
            rows = connection.execute(
                text("SELECT id, to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM'), name, archived FROM thread")
            )
            assert set(rows) == {(unnamed, "2026-01", None, False), (named, "2026-02", "operator name", True)}
    finally:
        engine.dispose()


def test_tables_the_history_does_not_own_are_not_drift(db_url: str) -> None:
    """Histories can share a database, each with a version table of its own."""
    _execute(db_url, "CREATE TABLE another_history_test_table (id integer PRIMARY KEY)")
    RUNNER.apply(db_url)


def test_the_runner_registers_every_migrated_table(db_url: str) -> None:
    """`database_migrate.py` registers tables by importing their modules. This process's fixtures import
    them all anyway, so the runner is imported in a fresh interpreter."""
    registered = subprocess.run(
        [sys.executable, "-c", "from agentplane.app.database_migrate import RUNNER; print(*RUNNER.metadata.tables)"],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    engine = create_engine(RUNNER.sync_url(db_url))
    try:
        migrated = set(inspect(engine).get_table_names()) - {RUNNER.version_table}
    finally:
        engine.dispose()
    assert set(registered) == migrated


if __name__ == "__main__":
    pytest_bazel.main()
