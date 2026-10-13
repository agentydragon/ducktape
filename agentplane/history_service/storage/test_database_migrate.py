"""Data migrations of the history schema against a real PostgreSQL."""

import json
import uuid

import pytest_bazel
from google.protobuf.json_format import MessageToDict
from sqlalchemy import Connection, create_engine, text

from agentplane.history_service.storage.database_migrate import RUNNER
from agentplane.proto import history_service_pb2, runner_pb2

# The migrated database is read back over psycopg, which SQLAlchemy loads from the URL scheme.
# gazelle:include_dep @pypi//psycopg


def _restore_sandbox_locator(connection: Connection) -> None:
    """The follower's columns that migrations before `0014_drop_sandbox_locator` expect."""
    connection.execute(text("ALTER TABLE session_history DROP COLUMN sandbox_namespace, DROP COLUMN sandbox_name"))
    connection.execute(
        text(
            "ALTER TABLE session_history ADD COLUMN sandbox_namespace varchar, ADD COLUMN sandbox_name varchar, "
            "ADD COLUMN sandbox_uid uuid, ADD COLUMN runner_session_id varchar, "
            "ADD CONSTRAINT history_sandbox_complete CHECK (num_nulls(sandbox_namespace, sandbox_name) IN (0, 2)), "
            "ADD CONSTRAINT history_origin CHECK (sandbox_namespace IS NOT NULL OR writer_uid IS NOT NULL)"
        )
    )
    connection.execute(
        text(
            "CREATE UNIQUE INDEX ux_history_runner_locator ON session_history "
            "(sandbox_namespace, sandbox_name, sandbox_uid, runner_session_id) NULLS NOT DISTINCT "
            "WHERE runner_session_id IS NOT NULL"
        )
    )


def test_follower_snapshots_take_the_public_session_id(db_url: str) -> None:
    session_id = uuid.uuid4()
    attached = runner_pb2.Attached(
        session_id="test-native-runner",
        last_cursor=3,
        harness_state=runner_pb2.HARNESS_STATE_STOPPED,
        active_turn_id="test-turn",
    )
    feed = history_service_pb2.SessionFeedState(attached=attached, sealed=runner_pb2.Sealed(cursor=3))
    engine = create_engine(RUNNER.sync_url(db_url))
    try:
        with engine.begin() as connection:
            _restore_sandbox_locator(connection)
            connection.execute(text("UPDATE alembic_version SET version_num = '0012_drop_resumed_after_cursor'"))
            connection.execute(
                text(
                    "INSERT INTO session_history (id, sandbox_namespace, sandbox_name, runner_session_id, last_cursor, "
                    "feed_state) VALUES (:id, 'test-namespace', 'test-gone', 'test-native-runner', 3, :feed)"
                ),
                {"id": session_id, "feed": feed.SerializeToString()},
            )
            connection.execute(
                text("INSERT INTO thread_history_summary (thread_id, attached) VALUES (:id, CAST(:attached AS jsonb))"),
                {"id": session_id, "attached": json.dumps(MessageToDict(attached))},
            )
        # Through the locator drop only: the later migrations already ran on this database before the stamp.
        RUNNER.apply(db_url, "0014_drop_sandbox_locator")
        with engine.connect() as connection:
            stored = history_service_pb2.SessionFeedState.FromString(
                connection.scalar(text("SELECT feed_state FROM session_history WHERE id = :id"), {"id": session_id})
            )
            summary = connection.scalar(
                text("SELECT attached FROM thread_history_summary WHERE thread_id = :id"), {"id": session_id}
            )
    finally:
        engine.dispose()
    attached.session_id = str(session_id)
    feed.attached.CopyFrom(attached)
    assert stored == feed
    assert summary == MessageToDict(attached)


def test_sandbox_of_a_bound_session_is_its_writers_account(db_url: str) -> None:
    bound, unbound = uuid.uuid4(), uuid.uuid4()
    engine = create_engine(RUNNER.sync_url(db_url))
    try:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE session_history DROP COLUMN sandbox_namespace, DROP COLUMN sandbox_name")
            )
            connection.execute(text("UPDATE alembic_version SET version_num = '0016_recovery_reason_null'"))
            connection.execute(
                text(
                    "INSERT INTO session_history (id, writer_namespace, writer_name, writer_uid, last_cursor) "
                    "VALUES (:id, 'test-namespace', 'test-sandbox', 'test-account-uid', 0)"
                ),
                {"id": bound},
            )
            connection.execute(text("INSERT INTO session_history (id, last_cursor) VALUES (:id, 0)"), {"id": unbound})
        RUNNER.apply(db_url)
        with engine.connect() as connection:
            rows = {
                row.id: (row.sandbox_namespace, row.sandbox_name)
                for row in connection.execute(text("SELECT id, sandbox_namespace, sandbox_name FROM session_history"))
            }
    finally:
        engine.dispose()
    assert rows == {bound: ("test-namespace", "test-sandbox"), unbound: (None, None)}


if __name__ == "__main__":
    pytest_bazel.main()
