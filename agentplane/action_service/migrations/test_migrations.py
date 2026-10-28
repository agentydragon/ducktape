"""The Goose migrator serializes concurrent startup against a new database."""

import subprocess
from collections.abc import Iterator
from contextlib import contextmanager

import pytest_bazel
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from util.bazel.runfiles import get_required_path, own_repo_rlocation
from util.testing.postgres import DatabaseNames, create_temporary_database

# SQLAlchemy loads this URL dialect dynamically.
# gazelle:include_dep @pypi//psycopg

_MIGRATOR = get_required_path(
    own_repo_rlocation("agentplane/action_service/migrations/cmd/actions-migrate_/actions-migrate")
)
_GOOSE_VERSION = "action_service_goose_version"


def _wire_url(url: str) -> str:
    # The local testcontainer has no TLS; production URLs keep their own sslmode.
    return (
        make_url(url).set(drivername="postgresql", query={"sslmode": "disable"}).render_as_string(hide_password=False)
    )


@contextmanager
def _engine(url: str) -> Iterator[Engine]:
    engine = create_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()


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


def test_storage_backfill_requires_writer_rollout_confirmation(
    postgres_admin_url: str, database_names: DatabaseNames
) -> None:
    with create_temporary_database(postgres_admin_url, database_names()) as url:
        runner = subprocess.run(
            [str(_MIGRATOR), "backfill-storage"],
            env={"AGENTPLANE_ACTIONS_DATABASE_URL": _wire_url(url)},
            capture_output=True,
            text=True,
            check=False,
        )
        assert runner.returncode == 2, (runner.stdout, runner.stderr)
        assert "--confirm-all-writers-dual-write" in runner.stderr


if __name__ == "__main__":
    pytest_bazel.main()
