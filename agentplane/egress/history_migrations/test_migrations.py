"""Fresh egress Goose schema and the guarded handoff from Alembic 0002."""

import subprocess
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_bazel
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url

from agentplane.egress.database_migrate import RUNNER as EGRESS_ALEMBIC
from util.bazel.runfiles import get_required_path, own_repo_rlocation
from util.testing.postgres import DatabaseNames, create_temporary_database

# SQLAlchemy loads this URL dialect dynamically.
# gazelle:include_dep @pypi//psycopg

_MIGRATOR = get_required_path(
    own_repo_rlocation("agentplane/egress/history_migrations/cmd/egress-history-migrate_/egress-history-migrate")
)
_VERSION = "egress_goose_version"
_INSERT = text("""
INSERT INTO egress_decision (event_id, decided_at, producer_id, subject_namespace, subject_name,
                             connection_id, phase, method, host, port, outcome, substituted)
VALUES (:id, :now, :producer, 'test-namespace', 'test-workload', 'test-connection', 'connect', 'CONNECT',
        'example.test', 443, 'allow', false)
""")


def _run(url: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_MIGRATOR), "--database-url", _wire_url(url)], text=True, capture_output=True, timeout=60, check=check
    )


def _wire_url(url: str) -> str:
    # The local testcontainer has no TLS; production URLs keep their own sslmode.
    return (
        make_url(url).set(drivername="postgresql", query={"sslmode": "disable"}).render_as_string(hide_password=False)
    )


def _catalog(engine: Engine) -> dict[str, set[tuple]]:
    """Everything the proxy's reads and writes depend on, as the database itself describes it."""
    with engine.connect() as conn:
        return {
            "columns": set(
                conn.execute(
                    text("""
SELECT column_name, udt_name, is_nullable, column_default, is_identity
FROM information_schema.columns WHERE table_name = 'egress_decision'
""")
                ).tuples()
            ),
            "indexes": set(
                conn.execute(
                    text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'egress_decision'")
                ).tuples()
            ),
            "constraints": set(
                conn.execute(
                    text("""
SELECT conname, contype, pg_get_constraintdef(oid) FROM pg_constraint
WHERE conrelid = 'egress_decision'::regclass
""")
                ).tuples()
            ),
        }


def test_fresh_database_matches_alembic_and_repeat_is_idempotent(
    postgres_admin_url: str, database_names: DatabaseNames
) -> None:
    with (
        create_temporary_database(postgres_admin_url, database_names()) as alembic_url,
        create_temporary_database(postgres_admin_url, database_names()) as goose_url,
    ):
        EGRESS_ALEMBIC.apply(alembic_url)
        _run(goose_url)
        _run(goose_url)
        alembic_engine, goose_engine = create_engine(alembic_url), create_engine(goose_url)
        try:
            assert _catalog(goose_engine) == _catalog(alembic_engine)
            with goose_engine.connect() as conn:
                assert conn.scalar(text("SELECT to_regclass('egress_alembic_version') IS NULL"))
                assert conn.scalar(text(f"SELECT max(version_id) FROM {_VERSION}")) == 2
        finally:
            alembic_engine.dispose()
            goose_engine.dispose()


def test_alembic_head_handoff_preserves_history(postgres_admin_url: str, database_names: DatabaseNames) -> None:
    with create_temporary_database(postgres_admin_url, database_names()) as url:
        EGRESS_ALEMBIC.apply(url)
        event_id = str(uuid4())
        engine = create_engine(url)
        try:
            with engine.begin() as conn:
                conn.execute(_INSERT, {"id": event_id, "now": datetime.now(UTC), "producer": str(uuid4())})
            _run(url)
            with engine.connect() as conn:
                assert conn.scalar(text("SELECT host FROM egress_decision WHERE event_id = :id"), {"id": event_id}) == (
                    "example.test"
                )
                assert conn.scalar(text(f"SELECT max(version_id) FROM {_VERSION}")) == 2
                assert conn.scalar(text("SELECT version_num FROM egress_alembic_version")) == (
                    "0002_service_account_subject"
                )
        finally:
            engine.dispose()


@pytest.mark.parametrize("stamp", [None, "0001_decisions", "unexpected_head"])
def test_unrecognized_alembic_head_cannot_be_stamped(
    postgres_admin_url: str, database_names: DatabaseNames, stamp: str | None
) -> None:
    """`None` is a database that has the tables but no Alembic history at all."""
    with create_temporary_database(postgres_admin_url, database_names()) as url:
        EGRESS_ALEMBIC.apply(url)
        engine = create_engine(url)
        try:
            with engine.begin() as conn:
                if stamp is None:
                    conn.execute(text("DROP TABLE egress_alembic_version"))
                else:
                    conn.execute(text("UPDATE egress_alembic_version SET version_num = :head"), {"head": stamp})
            result = _run(url, check=False)
            assert result.returncode != 0
            assert (
                "exists without Alembic history" if stamp is None else "expected sole Alembic head"
            ) in result.stderr
            with engine.connect() as conn:
                # Goose may create its empty history table, but it cannot record baseline version 1.
                assert conn.scalar(text(f"SELECT max(version_id) FROM {_VERSION}")) in (0, None)
        finally:
            engine.dispose()


@pytest.mark.parametrize(
    "drift",
    [
        "DROP INDEX egress_decision_denials_recent",
        "ALTER TABLE egress_decision DROP CONSTRAINT egress_decision_subject_whole",
        "ALTER TABLE egress_decision ALTER COLUMN reason SET NOT NULL",
    ],
)
def test_schema_drift_cannot_be_baselined(postgres_admin_url: str, database_names: DatabaseNames, drift: str) -> None:
    with create_temporary_database(postgres_admin_url, database_names()) as url:
        EGRESS_ALEMBIC.apply(url)
        engine = create_engine(url)
        try:
            with engine.begin() as conn:
                conn.execute(text(drift))
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
