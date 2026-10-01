"""The migration history against a real PostgreSQL: it refuses a database that differs from the models,
re-running it is a no-op, and a database already stamped at an earlier revision is carried to the models."""

import os
import subprocess
import sys
import uuid

import pytest
import pytest_bazel
from sqlalchemy import create_engine, inspect, text

from agentplane.app.database_migrate import RUNNER

# The migrated database is read back over psycopg, which SQLAlchemy loads from the URL scheme.
# gazelle:include_dep @pypi//psycopg


def _execute(db_url: str, statement: str) -> None:
    engine = create_engine(RUNNER.sync_url(db_url))
    try:
        with engine.begin() as connection:
            connection.execute(text(statement))
    finally:
        engine.dispose()


def test_a_database_that_differs_from_the_models_fails_to_migrate(db_url: str) -> None:
    """A database that applied a since-edited migration is stamped at head and still differs; the
    init container has to stop the rollout instead of letting the app write to the wrong schema."""
    _execute(db_url, 'ALTER TABLE thread_payload_chunk ALTER COLUMN "text" TYPE text USING "text"::text')
    with pytest.raises(RuntimeError, match="thread_payload_chunk"):
        RUNNER.apply(db_url)


def test_an_index_only_the_database_has_is_drift(db_url: str) -> None:
    _execute(db_url, "CREATE INDEX index_only_in_test_database ON thread_payload_chunk (chunk_index)")
    with pytest.raises(RuntimeError, match="index_only_in_test_database"):
        RUNNER.apply(db_url)


def test_a_second_run_over_a_migrated_database_changes_nothing(db_url: str) -> None:
    """Every replica's init container runs this against the same database, on every rollout."""
    RUNNER.apply(db_url)


def test_tables_the_history_does_not_own_are_not_drift(db_url: str) -> None:
    """Histories can share a database, each with a version table of its own."""
    _execute(db_url, "CREATE TABLE another_history_test_table (id integer PRIMARY KEY)")
    RUNNER.apply(db_url)


def _migrate_from_0015(db_url: str, column_type: str, stored: list[str]) -> list[str]:
    """Carry a database stamped `0015`, its chunk column `column_type` and holding `stored` as written, to
    head. Returns the chunks as a reader decodes them."""
    engine = create_engine(RUNNER.sync_url(db_url))
    thread = uuid.uuid4()
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    f'ALTER TABLE thread_payload_chunk ALTER COLUMN "text" TYPE {column_type} USING "text"::{column_type}'
                )
            )
            connection.execute(text("UPDATE alembic_version_app SET version_num = '0015_event_payload_json'"))
            connection.execute(
                text(
                    "INSERT INTO event_log (id, sandbox, session_id, harness, model, cwd, created_at) "
                    "VALUES (:thread, 'test-sandbox', 'test-session', 'HARNESS_CODEX', 'test-model', '/test', now())"
                ),
                {"thread": thread},
            )
            for index, value in enumerate(stored):
                connection.execute(
                    text(
                        "INSERT INTO thread_payload_chunk "
                        '(thread_id, projection_epoch, owner_cursor, owner_id, field, generation, chunk_index, "text") '
                        f"VALUES (:thread, 'test-epoch', 1, 'test-owner', 'body', 0, :index, CAST(:stored AS {column_type}))"
                    ),
                    {"thread": thread, "index": index, "stored": value},
                )
        RUNNER.apply(db_url)
        with engine.connect() as connection:
            return list(connection.scalars(text('SELECT "text" FROM thread_payload_chunk ORDER BY chunk_index')))
    finally:
        engine.dispose()


def test_a_chunk_column_left_as_text_is_converted_to_json_strings(db_url: str) -> None:
    """A staging database applied `0005` while it created `text`, then ran the app that writes JSON strings."""
    chunks = _migrate_from_0015(
        db_url,
        "text",
        [
            "plain output",
            'say "hi"\nthen',
            "123",  # Bare text that is valid JSON, but not a string.
            r'"written \"by\" the app — \u0000"',
        ],
    )
    assert chunks == ["plain output", 'say "hi"\nthen', "123", 'written "by" the app — \x00']


def test_a_chunk_column_already_json_keeps_its_strings(db_url: str) -> None:
    """A database created while `0005` created `json` is stamped `0015` without ever holding `text`."""
    chunks = _migrate_from_0015(db_url, "json", [r'"plain output"', r'"written \"by\" the app — \u0000"'])
    assert chunks == ["plain output", 'written "by" the app — \x00']


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
