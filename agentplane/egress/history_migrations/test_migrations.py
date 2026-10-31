"""Goose schema creation, retained-history reruns, and concurrent migration."""

import subprocess
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_bazel
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from util.bazel.runfiles import get_required_path, own_repo_rlocation
from util.testing.postgres import DatabaseNames, create_temporary_database

# SQLAlchemy loads this URL dialect dynamically.
# gazelle:include_dep @pypi//psycopg

_SERVICE = get_required_path(own_repo_rlocation("agentplane/egress/cmd/egress-proxy_/egress-proxy"))
_VERSION = "egress_goose_version"
_INSERT = text("""
INSERT INTO egress_decision (event_id, decided_at, producer_id, subject_namespace, subject_name,
                             connection_id, phase, method, host, port, outcome, substituted)
VALUES (:id, :now, :producer, 'test-namespace', 'test-workload', 'test-connection', 'connect', 'CONNECT',
        'example.test', 443, 'allow', false)
""")


def _run(url: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_SERVICE), "--migrate-only"],
        env={"AGENTPLANE_EGRESS_DATABASE_URL": _wire_url(url)},
        text=True,
        capture_output=True,
        timeout=60,
        check=check,
    )


def _wire_url(url: str) -> str:
    # The local testcontainer has no TLS; production URLs keep their own sslmode.
    return (
        make_url(url).set(drivername="postgresql", query={"sslmode": "disable"}).render_as_string(hide_password=False)
    )


@pytest.mark.parametrize("prior_handoff", [False, True], ids=["fresh-goose", "deployed-goose-records"])
def test_fresh_database_and_repeat_preserve_history(
    postgres_admin_url: str, database_names: DatabaseNames, prior_handoff: bool
) -> None:
    with create_temporary_database(postgres_admin_url, database_names()) as url:
        _run(url)
        event_id = str(uuid4())
        engine = create_engine(url)
        try:
            with engine.begin() as conn:
                conn.execute(_INSERT, {"id": event_id, "now": datetime.now(UTC), "producer": str(uuid4())})
                if prior_handoff:
                    # Both deployed databases already record the retired Go preflight as version 1.
                    conn.execute(text(f"INSERT INTO {_VERSION} (version_id, is_applied) VALUES (1, true)"))
                    conn.execute(text("CREATE TABLE egress_alembic_version (version_num varchar(32) NOT NULL)"))
                    conn.execute(text("INSERT INTO egress_alembic_version VALUES ('0002_service_account_subject')"))
            _run(url)
            with engine.connect() as conn:
                assert conn.scalar(text("SELECT host FROM egress_decision WHERE event_id = :id"), {"id": event_id}) == (
                    "example.test"
                )
                assert conn.scalar(text(f"SELECT max(version_id) FROM {_VERSION} WHERE is_applied")) == 2
                assert conn.scalar(text(f"SELECT count(*) FROM {_VERSION} WHERE version_id=2 AND is_applied")) == 1
                if prior_handoff:
                    assert conn.scalar(text(f"SELECT count(*) FROM {_VERSION} WHERE version_id=1 AND is_applied")) == 1
                    assert conn.scalar(text("SELECT version_num FROM egress_alembic_version")) == (
                        "0002_service_account_subject"
                    )
                else:
                    assert conn.scalar(text("SELECT to_regclass('egress_alembic_version') IS NULL"))
        finally:
            engine.dispose()


def test_concurrent_runners_serialize(postgres_admin_url: str, database_names: DatabaseNames) -> None:
    with create_temporary_database(postgres_admin_url, database_names()) as url:
        argv = [str(_SERVICE), "--migrate-only"]
        first = subprocess.Popen(
            argv,
            env={"AGENTPLANE_EGRESS_DATABASE_URL": _wire_url(url)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        second = subprocess.Popen(
            argv,
            env={"AGENTPLANE_EGRESS_DATABASE_URL": _wire_url(url)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
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
