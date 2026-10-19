"""Run the Go history tests against the Goose-owned schema."""

import os
import subprocess

import pytest_bazel
from sqlalchemy.engine import make_url

from util.bazel.runfiles import get_required_path, own_repo_rlocation
from util.testing.postgres import DatabaseNames, create_temporary_database

# gazelle:include_dep @pypi//psycopg

_GO_TEST = get_required_path(
    own_repo_rlocation("agentplane/egress/history/store_integration_test_/store_integration_test")
)
_MIGRATOR = get_required_path(
    own_repo_rlocation("agentplane/egress/history_migrations/cmd/egress-history-migrate_/egress-history-migrate")
)


def test_go_history_against_goose_schema(postgres_admin_url: str, database_names: DatabaseNames) -> None:
    with create_temporary_database(postgres_admin_url, database_names()) as url:
        wire_url = (
            make_url(url)
            .set(drivername="postgresql", query={"sslmode": "disable"})
            .render_as_string(hide_password=False)
        )
        subprocess.run(
            [str(_MIGRATOR), "--database-url", wire_url], capture_output=True, text=True, timeout=60, check=True
        )
        result = subprocess.run(
            [str(_GO_TEST), "-test.v"],
            env={**os.environ, "AGENTPLANE_TEST_DATABASE_URL": wire_url},
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        assert result.returncode == 0, f"Go history integration tests failed:\n{result.stdout}\n{result.stderr}"


if __name__ == "__main__":
    pytest_bazel.main()
