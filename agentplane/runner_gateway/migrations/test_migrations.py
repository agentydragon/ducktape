"""Dispatcher Goose schema creation, idempotency and concurrent migration."""

import subprocess

import pytest_bazel
from sqlalchemy import create_engine, text

from util.bazel.runfiles import get_required_path, own_repo_rlocation
from util.db_url import render_postgresql_url
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
    return render_postgresql_url(url, "postgresql", query={"sslmode": "disable"})


def test_fresh_database_and_repeat_are_idempotent(postgres_admin_url: str, database_names: DatabaseNames) -> None:
    with create_temporary_database(postgres_admin_url, database_names()) as url:
        _run(url)
        _run(url)
        engine = create_engine(url)
        try:
            with engine.connect() as conn:
                assert conn.scalar(text("SELECT to_regclass('dispatch') IS NOT NULL"))
                assert conn.scalar(text(f"SELECT max(version_id) FROM {_VERSION}")) == 7
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
                assert conn.scalar(text(f"SELECT count(*) FROM {_VERSION} WHERE version_id=7 AND is_applied")) == 1
        finally:
            engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
