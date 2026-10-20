"""Data migrations of the history schema against a real PostgreSQL."""

import json
import uuid

import pytest_bazel
from google.protobuf.json_format import MessageToDict
from sqlalchemy import Connection, create_engine, text

from agentplane.history_service import history_service_pb2
from agentplane.history_service.storage.database_migrate import RUNNER
from agentplane.proto import runner_pb2

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
        # Later migrations already ran before the stamp; exercise only the Sandbox backfill.
        RUNNER.apply(db_url, "0017_session_sandbox")
        with engine.connect() as connection:
            rows = {
                row.id: (row.sandbox_namespace, row.sandbox_name)
                for row in connection.execute(text("SELECT id, sandbox_namespace, sandbox_name FROM session_history"))
            }
    finally:
        engine.dispose()
    assert rows == {bound: ("test-namespace", "test-sandbox"), unbound: (None, None)}


def _command_state(operation: str, outcome: str, outcome_cursor: int | None = None) -> dict[str, object]:
    return {
        "operation": operation,
        "outcome": outcome,
        "outcome_cursor": None if outcome_cursor is None else str(outcome_cursor),
        "outcome_reason": None,
    }


_EPOCH = "test-epoch"

type _Entity = tuple[str, str, int, dict[str, object]]


def _seed_fold(connection: Connection, session_id: uuid.UUID, last_cursor: int, entities: list[_Entity]) -> None:
    """A Session with `last_cursor` empty stored entries, a fold checkpoint there and `(kind, id, cursor, state)` entities."""
    connection.execute(
        text("INSERT INTO session_history (id, last_cursor) VALUES (:id, :last)"),
        {"id": session_id, "last": last_cursor},
    )
    connection.execute(
        text(
            "INSERT INTO session_event (session_id, cursor, payload) "
            "SELECT :id, cursor, ''::bytea FROM generate_series(1, :last) AS series(cursor)"
        ),
        {"id": session_id, "last": last_cursor},
    )
    connection.execute(
        text(
            "INSERT INTO thread_checkpoint (thread_id, source_id, projection_epoch, through_cursor) "
            "VALUES (:id, 'test-source', :epoch, :last)"
        ),
        {"id": session_id, "epoch": _EPOCH, "last": last_cursor},
    )
    for index, (kind, entity_id, cursor, state) in enumerate(entities):
        connection.execute(
            text(
                "INSERT INTO thread_entity (thread_id, projection_epoch, entity_kind, entity_id, cursor, "
                "revision_cursor, pending, state, entity_index) "
                "VALUES (:id, :epoch, :kind, :entity_id, :cursor, :cursor, false, CAST(:state AS jsonb), :index)"
            ),
            {
                "id": session_id,
                "epoch": _EPOCH,
                "kind": kind,
                "entity_id": entity_id,
                "cursor": cursor,
                "state": json.dumps(state),
                "index": index,
            },
        )


def test_harness_stop_records_are_removed_and_the_fold_closes_over_them(db_url: str) -> None:
    """Admissions at 3 and 5 and the failure at 4 go; 7 is the stop's retained `HarnessExited`; 8 is pending."""
    stopped, untouched = uuid.uuid4(), uuid.uuid4()
    exited: dict[str, object] = {
        "observation": "harness_exited",
        "event": {"harnessExited": {"exitCode": 1, "stoppedByCommandId": "b"}},
    }
    entities: list[_Entity] = [
        ("view_state", "current", 8, {}),
        ("lifecycle", "1", 1, {"observation": "harness_started", "event": {}}),
        ("command", "keep", 2, _command_state("submit_input", "pending")),
        ("command", "a", 3, _command_state("stop_runner_session", "failed", 4)),
        ("command", "b", 5, _command_state("historical_harness_stop", "effected", 7)),
        ("lifecycle", "7", 7, exited),
        ("command", "c", 8, _command_state("stop_runner_session", "pending")),
    ]
    engine = create_engine(RUNNER.sync_url(db_url))
    try:
        with engine.begin() as connection:
            connection.execute(text("UPDATE alembic_version SET version_num = '0020_removed_ranges'"))
            for session_id, last_cursor, rows in ((stopped, 8, entities), (untouched, 2, entities[:3])):
                _seed_fold(connection, session_id, last_cursor, rows)
            for entity_cursor, observation_cursor in ((2, 2), (3, 3), (3, 4), (5, 5), (5, 7), (8, 8)):
                connection.execute(
                    text(
                        "INSERT INTO thread_evidence (thread_id, projection_epoch, entity_cursor, observation_cursor) "
                        "VALUES (:id, :epoch, :entity, :observation)"
                    ),
                    {"id": stopped, "epoch": _EPOCH, "entity": entity_cursor, "observation": observation_cursor},
                )
        RUNNER.apply(db_url, "0021_drop_harness_stop_records")
        with engine.connect() as connection:

            def read(query: str, session_id: uuid.UUID) -> list[tuple[object, ...]]:
                return [tuple(row) for row in connection.execute(text(query), {"id": session_id})]

            def read_entities(session_id: uuid.UUID) -> list[tuple[object, ...]]:
                return read(
                    "SELECT entity_index, entity_kind, entity_id, state FROM thread_entity WHERE thread_id = :id "
                    "ORDER BY entity_index",
                    session_id,
                )

            events = {
                session_id: read("SELECT cursor FROM session_event WHERE session_id = :id ORDER BY cursor", session_id)
                for session_id in (stopped, untouched)
            }
            removed = {
                session_id: read(
                    "SELECT first_cursor, last_cursor FROM session_event_removed_range WHERE session_id = :id "
                    "ORDER BY first_cursor",
                    session_id,
                )
                for session_id in (stopped, untouched)
            }
            stopped_entities = read_entities(stopped)
            untouched_entities = read_entities(untouched)
            evidence = read(
                "SELECT entity_cursor, observation_cursor FROM thread_evidence WHERE thread_id = :id "
                "ORDER BY entity_cursor, observation_cursor",
                stopped,
            )
            checkpoint = read("SELECT through_cursor FROM thread_checkpoint WHERE thread_id = :id", stopped)
    finally:
        engine.dispose()
    assert events == {stopped: [(1,), (2,), (6,), (7,), (8,)], untouched: [(1,), (2,)]}
    assert removed == {stopped: [(3, 5)], untouched: []}
    # Dense from 0 again, and the retained exit has lost only the stop command's id.
    assert [row[:3] for row in stopped_entities] == [
        (0, "view_state", "current"),
        (1, "lifecycle", "1"),
        (2, "command", "keep"),
        (3, "lifecycle", "7"),
        (4, "command", "c"),
    ]
    assert stopped_entities[3][3] == {"observation": "harness_exited", "event": {"harnessExited": {"exitCode": 1}}}
    assert untouched_entities == [
        (index, kind, entity_id, state) for index, (kind, entity_id, _, state) in enumerate(entities[:3])
    ]
    assert evidence == [(2, 2), (8, 8)]
    assert checkpoint == [(8,)]


def test_pending_harness_stops_are_removed_too(db_url: str) -> None:
    """The pending stop at 4 ends its Session's history; the one at 2 is followed by a retained entry."""
    ending, middle = uuid.uuid4(), uuid.uuid4()
    sessions: dict[uuid.UUID, tuple[int, list[_Entity]]] = {
        ending: (
            4,
            [
                ("view_state", "current", 4, {}),
                ("command", "keep", 1, _command_state("submit_input", "pending")),
                ("command", "stop", 4, _command_state("stop_runner_session", "pending")),
            ],
        ),
        middle: (
            3,
            [
                ("command", "stop", 2, _command_state("stop_runner_session", "pending")),
                ("lifecycle", "3", 3, {"observation": "harness_started", "event": {}}),
            ],
        ),
    }
    engine = create_engine(RUNNER.sync_url(db_url))
    try:
        with engine.begin() as connection:
            connection.execute(text("UPDATE alembic_version SET version_num = '0021_drop_harness_stop_records'"))
            for session_id, (last_cursor, entities) in sessions.items():
                _seed_fold(connection, session_id, last_cursor, entities)
            for session_id, cursors in ((ending, (1, 4)), (middle, (2, 3))):
                for cursor in cursors:
                    connection.execute(
                        text(
                            "INSERT INTO thread_evidence (thread_id, projection_epoch, entity_cursor, observation_cursor) "
                            "VALUES (:id, :epoch, :cursor, :cursor)"
                        ),
                        {"id": session_id, "epoch": _EPOCH, "cursor": cursor},
                    )
        RUNNER.apply(db_url, "0022_drop_pending_harness_stops")
        with engine.connect() as connection:

            def read(query: str, session_id: uuid.UUID) -> list[tuple[object, ...]]:
                return [tuple(row) for row in connection.execute(text(query), {"id": session_id})]

            after = {
                session_id: {
                    "events": read(
                        "SELECT cursor FROM session_event WHERE session_id = :id ORDER BY cursor", session_id
                    ),
                    "removed": read(
                        "SELECT first_cursor, last_cursor FROM session_event_removed_range WHERE session_id = :id",
                        session_id,
                    ),
                    "entities": read(
                        "SELECT entity_index, entity_kind, entity_id FROM thread_entity WHERE thread_id = :id "
                        "ORDER BY entity_index",
                        session_id,
                    ),
                    "evidence": read("SELECT entity_cursor FROM thread_evidence WHERE thread_id = :id", session_id),
                    "committed": read(
                        "SELECT checkpoint.through_cursor, history.last_cursor FROM thread_checkpoint AS checkpoint "
                        "JOIN session_history AS history ON history.id = checkpoint.thread_id WHERE history.id = :id",
                        session_id,
                    ),
                }
                for session_id in sessions
            }
    finally:
        engine.dispose()
    assert after == {
        ending: {
            "events": [(1,), (2,), (3,)],
            "removed": [(4, 4)],
            "entities": [(0, "view_state", "current"), (1, "command", "keep")],
            "evidence": [(1,)],
            "committed": [(4, 4)],
        },
        middle: {
            "events": [(1,), (3,)],
            "removed": [(2, 2)],
            "entities": [(0, "lifecycle", "3")],
            "evidence": [(3,)],
            "committed": [(3, 3)],
        },
    }


if __name__ == "__main__":
    pytest_bazel.main()
