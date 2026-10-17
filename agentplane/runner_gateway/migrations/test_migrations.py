"""Fresh Dispatcher Goose schema and guarded handoff from Alembic 0007."""

import subprocess
from datetime import UTC, datetime
from uuid import uuid4

import pytest_bazel
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from agentplane.session_dispatcher.database_migrate import RUNNER as DISPATCHER_ALEMBIC
from util.bazel.runfiles import get_required_path, own_repo_rlocation
from util.testing.postgres import DatabaseNames, create_temporary_database

# SQLAlchemy loads this URL dialect dynamically.
# gazelle:include_dep @pypi//psycopg

_MIGRATOR = get_required_path(
    own_repo_rlocation(
        "agentplane/runner_gateway/migrations/cmd/runner-gateway-queue-migrate_/runner-gateway-queue-migrate"
    )
)
_VERSION = "runner_gateway_dispatcher_goose_version"


def _run(url: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_MIGRATOR), "--database-url", _wire_url(url)], text=True, capture_output=True, timeout=60, check=check
    )


def _wire_url(url: str) -> str:
    # The local testcontainer has no TLS; production URLs keep their own sslmode.
    return (
        make_url(url).set(drivername="postgresql", query={"sslmode": "disable"}).render_as_string(hide_password=False)
    )


def test_fresh_database_and_repeat_are_idempotent(postgres_admin_url: str, database_names: DatabaseNames) -> None:
    with create_temporary_database(postgres_admin_url, database_names()) as url:
        _run(url)
        _run(url)
        engine = create_engine(url)
        try:
            with engine.connect() as conn:
                assert conn.scalar(text("SELECT to_regclass('dispatch') IS NOT NULL"))
                assert conn.scalar(text("SELECT to_regclass('alembic_version') IS NULL"))
                assert (
                    conn.scalar(
                        text(
                            "SELECT count(*) FROM information_schema.columns WHERE table_name='session_opening' AND column_name='attached'"
                        )
                    )
                    == 0
                )
                assert conn.scalar(text(f"SELECT max(version_id) FROM {_VERSION}")) == 2
        finally:
            engine.dispose()


def test_alembic_head_handoff_preserves_queued_work(postgres_admin_url: str, database_names: DatabaseNames) -> None:
    with create_temporary_database(postgres_admin_url, database_names()) as url:
        DISPATCHER_ALEMBIC.apply(url)
        row_id = str(uuid4())
        engine = create_engine(url)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text("""
INSERT INTO dispatch (command_id, session_id, sandbox_name, sandbox_uid, operation,
                      command, state, created_at, updated_at)
VALUES (:id, 'session-a', 'sandbox-a', :uid, 'command', :body, 'queued', :now, :now)
"""),
                    {"id": row_id, "uid": str(uuid4()), "body": b"authored command", "now": datetime.now(UTC)},
                )
            _run(url)
            with engine.connect() as conn:
                assert (
                    conn.scalar(text("SELECT command FROM dispatch WHERE command_id=:id"), {"id": row_id})
                    == b"authored command"
                )
                assert conn.scalar(text(f"SELECT max(version_id) FROM {_VERSION}")) == 2
                assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0007_drop_opening_attached"
        finally:
            engine.dispose()


def test_unrecognized_alembic_head_cannot_be_stamped(postgres_admin_url: str, database_names: DatabaseNames) -> None:
    with create_temporary_database(postgres_admin_url, database_names()) as url:
        DISPATCHER_ALEMBIC.apply(url)
        engine = create_engine(url)
        try:
            with engine.begin() as conn:
                conn.execute(text("UPDATE alembic_version SET version_num='unexpected_head'"))
            result = _run(url, check=False)
            assert result.returncode != 0
            assert "expected sole Alembic head" in result.stderr
            with engine.connect() as conn:
                # Goose may create its empty history table, but it cannot record baseline version 1.
                assert conn.scalar(text(f"SELECT max(version_id) FROM {_VERSION}")) in (0, None)
        finally:
            engine.dispose()


def test_schema_drift_cannot_be_baselined(postgres_admin_url: str, database_names: DatabaseNames) -> None:
    with create_temporary_database(postgres_admin_url, database_names()) as url:
        DISPATCHER_ALEMBIC.apply(url)
        engine = create_engine(url)
        try:
            with engine.begin() as conn:
                conn.execute(text("DROP INDEX ix_dispatch_session_state"))
            result = _run(url, check=False)
            assert result.returncode != 0
            assert "differs from Alembic baseline" in result.stderr
        finally:
            engine.dispose()


def test_concurrent_runners_serialize(postgres_admin_url: str, database_names: DatabaseNames) -> None:
    with create_temporary_database(postgres_admin_url, database_names()) as url:
        argv = [str(_MIGRATOR), "--database-url", _wire_url(url)]
        first = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        second = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for process in (first, second):
            stdout, stderr = process.communicate(timeout=60)
            assert process.returncode == 0, (stdout, stderr)
        engine = create_engine(url)
        try:
            with engine.connect() as conn:
                assert conn.scalar(text(f"SELECT count(*) FROM {_VERSION} WHERE version_id=2 AND is_applied")) == 1
        finally:
            engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
