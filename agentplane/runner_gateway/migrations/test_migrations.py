"""Dispatcher Goose schema creation, idempotency and concurrent migration."""

import subprocess

import pytest_bazel
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from util.bazel.runfiles import get_required_path, own_repo_rlocation
from util.testing.postgres import DatabaseNames, create_temporary_database

# SQLAlchemy loads this URL dialect dynamically.
# gazelle:include_dep @pypi//psycopg

_SERVICE = get_required_path(own_repo_rlocation("agentplane/runner_gateway/runner-gateway_/runner-gateway"))
_VERSION = "runner_gateway_dispatcher_goose_version"


def _run(url: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_SERVICE), "--migrate-only"],
        env={"AGENTPLANE_RUNNER_GATEWAY_DISPATCHER_DATABASE_URL": _wire_url(url)},
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


def test_fresh_database_and_repeat_are_idempotent(postgres_admin_url: str, database_names: DatabaseNames) -> None:
    with create_temporary_database(postgres_admin_url, database_names()) as url:
        _run(url)
        _run(url)
        engine = create_engine(url)
        try:
            with engine.connect() as conn:
                assert conn.scalar(text("SELECT to_regclass('dispatch') IS NOT NULL"))
                assert conn.scalar(text(f"SELECT max(version_id) FROM {_VERSION}")) == 5
        finally:
            engine.dispose()


def test_concurrent_runners_serialize(postgres_admin_url: str, database_names: DatabaseNames) -> None:
    with create_temporary_database(postgres_admin_url, database_names()) as url:
        argv = [str(_SERVICE), "--migrate-only"]
        first = subprocess.Popen(
            argv,
            env={"AGENTPLANE_RUNNER_GATEWAY_DISPATCHER_DATABASE_URL": _wire_url(url)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        second = subprocess.Popen(
            argv,
            env={"AGENTPLANE_RUNNER_GATEWAY_DISPATCHER_DATABASE_URL": _wire_url(url)},
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
                assert conn.scalar(text(f"SELECT count(*) FROM {_VERSION} WHERE version_id=5 AND is_applied")) == 1
        finally:
            engine.dispose()


def test_stop_receipts_are_dropped_and_other_dispatches_kept(
    postgres_admin_url: str, database_names: DatabaseNames
) -> None:
    with create_temporary_database(postgres_admin_url, database_names()) as url:
        _run(url)
        engine = create_engine(url)
        try:
            with engine.begin() as conn:
                for command_id, operation in (("stop-1", "stop_runner_session"), ("send-1", "submit_input")):
                    conn.execute(
                        text(
                            "INSERT INTO dispatch (command_id, session_id, sandbox_name, sandbox_uid, operation,"
                            " command, state, created_at, updated_at) VALUES (:command_id, 'session', 'sandbox',"
                            " gen_random_uuid(), :operation, '\\x'::bytea, 'RUNNER_ADMITTED', now(), now())"
                        ),
                        {"command_id": command_id, "operation": operation},
                    )
                # Reapply the migration over data it did not see at its first run; Goose refuses to fill
                # a gap below an applied version, so later ones run again too.
                conn.execute(text(f"DELETE FROM {_VERSION} WHERE version_id >= 3"))
            _run(url)
            with engine.connect() as conn:
                assert conn.scalars(text("SELECT command_id FROM dispatch")).all() == ["send-1"]
        finally:
            engine.dispose()


def test_empty_admissions_are_cleared_and_runner_admissions_kept(
    postgres_admin_url: str, database_names: DatabaseNames
) -> None:
    with create_temporary_database(postgres_admin_url, database_names()) as url:
        _run(url)
        engine = create_engine(url)
        try:
            with engine.begin() as conn:
                for command_id, state, admission in (
                    ("claimed", "DELIVERING_TO_RUNNER", b""),
                    ("admitted", "RUNNER_ADMITTED", b"\x08\x01"),
                ):
                    conn.execute(
                        text(
                            "INSERT INTO dispatch (command_id, session_id, sandbox_name, sandbox_uid, operation,"
                            " command, state, admission, created_at, updated_at) VALUES (:command_id, 'session',"
                            " 'sandbox', gen_random_uuid(), 'submit_input', '\\x'::bytea, :state, :admission,"
                            " now(), now())"
                        ),
                        {"command_id": command_id, "state": state, "admission": admission},
                    )
                # Reapply the migration over data it did not see at its first run.
                conn.execute(text(f"DELETE FROM {_VERSION} WHERE version_id >= 4"))
            _run(url)
            with engine.connect() as conn:
                assert dict(conn.execute(text("SELECT command_id, admission FROM dispatch")).tuples().all()) == {
                    "claimed": None,
                    "admitted": b"\x08\x01",
                }
        finally:
            engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
