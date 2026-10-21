"""Goose takes over a database the Alembic history built, and leaves it the schema it builds from scratch."""

import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

import pytest_bazel
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from agentplane.action_service.database_migrate import RUNNER
from util.bazel.runfiles import get_required_path, own_repo_rlocation
from util.testing.postgres import DatabaseNames, create_temporary_database
from util.testing.postgres_schema import schema_catalog

# SQLAlchemy loads this URL dialect dynamically.
# gazelle:include_dep @pypi//psycopg

_MIGRATOR = get_required_path(
    own_repo_rlocation("agentplane/action_service/migrations/cmd/actions-migrate_/actions-migrate")
)
_GOOSE_VERSION = "action_service_goose_version"
_ALEMBIC_HEAD = "0028_drop_external_grant"
_ALEMBIC_BEFORE_HEAD = "0027_drop_reconciliation_audit"
_MIGRATOR_TABLES = ("alembic_version", _GOOSE_VERSION)

# One row in each kind of table whose shape a migrator could change: JSONB with partial indexes, a composite
# key under a cascading foreign key, a NULLS NOT DISTINCT unique and a CHECK, a column default, and a
# SET NULL foreign key.
_ROWS = {
    "action_request": """
        INSERT INTO action_request (id, idempotency_key, arguments, state, version, created_at, updated_at, action,
                                    title, caller_namespace, caller_name, history_at)
        VALUES (:request, 'test-key', '{}', 'decision_pending', 1, now(), now(), '{}', 'test-title',
                'test-namespace', 'test-caller', now())""",
    "action_event": """
        INSERT INTO action_event (request_id, sequence, state, at)
        VALUES (:request, 1, 'decision_pending', now())""",
    "action_decision": """
        INSERT INTO action_decision (id, request_id, verdict, provider, idempotency_key, decided_at)
        VALUES (:decision, :request, 'allow', 'test-provider', 'test-key', now())""",
    "action_execution": """
        INSERT INTO action_execution (id, request_id, state, created_at)
        VALUES (:execution, :request, 'dispatching', now())""",
    "external_connection": """
        INSERT INTO external_connection (id, display_name, version, created_at, updated_at)
        VALUES (:connection, 'test-connection', 1, now(), now())""",
    "mcp_oauth_token_state": """
        INSERT INTO mcp_oauth_token_state (id, server_id, access_token, token_type, scope, token_revision, updated_at,
                                           refresh_failure_count)
        VALUES (:token, 'test-server', 'test-access-token', 'Bearer', '[]', 1, now(), 0)""",
    "mcp_server_linkage": """
        INSERT INTO mcp_server_linkage (server_id, server_url, revision, scopes, token_state_id)
        VALUES ('test-server', 'https://test-server.invalid/mcp', 1, '[]', :token)""",
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
        env={"AGENTPLANE_ACTIONS_DATABASE_URL": _wire_url(url)},
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


def test_goose_builds_over_the_alembic_head_what_it_builds_from_scratch(
    postgres_admin_url: str, database_names: DatabaseNames
) -> None:
    with (
        create_temporary_database(postgres_admin_url, database_names()) as alembic_url,
        create_temporary_database(postgres_admin_url, database_names()) as goose_url,
    ):
        RUNNER.apply(alembic_url)
        _migrate(alembic_url)
        _migrate(goose_url)
        with _engine(alembic_url) as alembic, _engine(goose_url) as goose:
            expected = _catalog(goose)
            assert expected["columns"]
            assert _catalog(alembic) == expected


def test_goose_keeps_the_rows_of_an_alembic_database(postgres_admin_url: str, database_names: DatabaseNames) -> None:
    ids = {name: uuid4() for name in ("request", "decision", "execution", "connection", "token")}
    with create_temporary_database(postgres_admin_url, database_names()) as url, _engine(url) as engine:
        RUNNER.apply(url)
        with engine.begin() as conn:
            for insert in _ROWS.values():
                conn.execute(text(insert), ids)
        _migrate(url)
        _migrate(url)
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
        RUNNER.apply(url, _ALEMBIC_BEFORE_HEAD)
        assert _migrate(url, check=False).returncode != 0
        with engine.connect() as conn:
            assert conn.scalar(text(f"SELECT count(*) FROM {_GOOSE_VERSION} WHERE version_id = 1 AND is_applied")) == 0


def test_concurrent_runners_serialize(postgres_admin_url: str, database_names: DatabaseNames) -> None:
    with create_temporary_database(postgres_admin_url, database_names()) as url, _engine(url) as engine:
        env = {"AGENTPLANE_ACTIONS_DATABASE_URL": _wire_url(url)}
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
