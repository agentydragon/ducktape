"""The Goose baseline equals the schema the Alembic history built, and takes over a database that history built."""

import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

import pytest_bazel
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from agentplane.notification_service.database_migrate import RUNNER
from util.bazel.runfiles import get_required_path, own_repo_rlocation
from util.testing.postgres import DatabaseNames, create_temporary_database
from util.testing.postgres_schema import schema_catalog

# SQLAlchemy loads this URL dialect dynamically.
# gazelle:include_dep @pypi//psycopg

_MIGRATOR = get_required_path(
    own_repo_rlocation("agentplane/notification_service/migrations/cmd/notifications-migrate_/notifications-migrate")
)
_GOOSE_VERSION = "notification_service_goose_version"
_ALEMBIC_HEAD = "0010_drop_write_only_columns"
_MIGRATOR_TABLES = ("alembic_version", _GOOSE_VERSION)

# One row in each kind of table whose shape a migrator could change: plain, JSONB-checked, identity-keyed
# and sequence-keyed.
_ROWS = {
    "inbox": """
        INSERT INTO inbox (id, owner_namespace, owner_name, destination_key, destination_ref, session_id, last_cursor,
                           acknowledged, covered, expired_through, retired, updated_at, claim_until)
        VALUES (:inbox, 'test-namespace', 'test-owner', 'test-destination', '{}', 'test-session', 1, 0, 0, 0, false,
                now(), now())""",
    "subscription": """
        INSERT INTO subscription (id, inbox_id, idempotency_key, creation, version, actions_after_sequence, cancelled,
                                  expires_at, generation)
        VALUES (:subscription, :inbox, 'test-key', '{"source": {"provider": "actions"}}', 1, 0, false, now(), 0)""",
    "entry": """
        INSERT INTO entry (inbox_id, cursor, event, created_at)
        VALUES (:inbox, 1, '{"provider": "actions", "request_id": "test-request", "sequence": 1}', now())""",
    "notice": """
        INSERT INTO notice (inbox_id, command_id, through_cursor, text, attempted, admitted, confirmed, runner_cursor)
        VALUES (:inbox, :subscription, 1, 'test-notice', false, false, false, 0)""",
    "github_repository": "INSERT INTO github_repository (full_name) VALUES ('test-org/test-repository')",
    "github_delivery": """
        INSERT INTO github_delivery (app_id, delivery_id, installation_id, event, digest, payload)
        VALUES (1, :subscription, 1, 'test-event', '\\x00'::bytea, '{}')""",
}


def _catalog(engine: Engine) -> dict[str, list[tuple[object, ...]]]:
    return schema_catalog(engine, migrator_tables=_MIGRATOR_TABLES, migrator_sequence_prefix=_GOOSE_VERSION)


def _wire_url(url: str) -> str:
    # The local testcontainer has no TLS; production URLs keep their own sslmode.
    return (
        make_url(url).set(drivername="postgresql", query={"sslmode": "disable"}).render_as_string(hide_password=False)
    )


def _migrate(url: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_MIGRATOR)],
        env={"AGENTPLANE_NOTIFICATIONS_DATABASE_URL": _wire_url(url)},
        text=True,
        capture_output=True,
        timeout=60,
        check=check,
    )


@contextmanager
def _engine(url: str) -> Iterator[Engine]:
    engine = create_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()


def test_baseline_matches_the_alembic_head(postgres_admin_url: str, database_names: DatabaseNames) -> None:
    with (
        create_temporary_database(postgres_admin_url, database_names()) as alembic_url,
        create_temporary_database(postgres_admin_url, database_names()) as goose_url,
    ):
        RUNNER.apply(alembic_url)
        _migrate(goose_url)
        with _engine(alembic_url) as alembic, _engine(goose_url) as goose:
            expected = _catalog(alembic)
            assert expected["columns"]
            assert _catalog(goose) == expected


def test_goose_leaves_an_alembic_database_and_its_rows_as_they_were(
    postgres_admin_url: str, database_names: DatabaseNames
) -> None:
    ids = {"inbox": uuid4(), "subscription": uuid4()}
    with create_temporary_database(postgres_admin_url, database_names()) as url, _engine(url) as engine:
        RUNNER.apply(url)
        with engine.begin() as conn:
            for insert in _ROWS.values():
                conn.execute(text(insert), ids)
        before = _catalog(engine)
        _migrate(url)
        _migrate(url)
        assert _catalog(engine) == before
        with engine.connect() as conn:
            assert {table: conn.scalar(text(f"SELECT count(*) FROM {table}")) for table in _ROWS} == dict.fromkeys(
                _ROWS, 1
            )
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == _ALEMBIC_HEAD
            assert conn.scalar(text(f"SELECT count(*) FROM {_GOOSE_VERSION} WHERE version_id = 1 AND is_applied")) == 1


def test_goose_refuses_an_alembic_database_behind_its_head(
    postgres_admin_url: str, database_names: DatabaseNames
) -> None:
    with create_temporary_database(postgres_admin_url, database_names()) as url, _engine(url) as engine:
        RUNNER.apply(url, "0009_github_issue_subject")
        assert _migrate(url, check=False).returncode != 0
        with engine.connect() as conn:
            assert conn.scalar(text(f"SELECT count(*) FROM {_GOOSE_VERSION} WHERE version_id = 1 AND is_applied")) == 0


def test_concurrent_runners_serialize(postgres_admin_url: str, database_names: DatabaseNames) -> None:
    with create_temporary_database(postgres_admin_url, database_names()) as url, _engine(url) as engine:
        env = {"AGENTPLANE_NOTIFICATIONS_DATABASE_URL": _wire_url(url)}
        runners = [
            subprocess.Popen([str(_MIGRATOR)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for _ in range(2)
        ]
        for runner in runners:
            stdout, stderr = runner.communicate(timeout=60)
            assert runner.returncode == 0, (stdout, stderr)
        with engine.connect() as conn:
            assert conn.scalar(text(f"SELECT count(*) FROM {_GOOSE_VERSION} WHERE version_id = 1 AND is_applied")) == 1


if __name__ == "__main__":
    pytest_bazel.main()
