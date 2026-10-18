"""Run the Go history tests against the schema Alembic created and the one Goose creates fresh."""

import os
import subprocess

import pytest
import pytest_bazel
from sqlalchemy.engine import make_url

from agentplane.egress.database_migrate import RUNNER as EGRESS_ALEMBIC
from util.bazel.runfiles import get_required_path, own_repo_rlocation
from util.testing.postgres import DatabaseNames, create_temporary_database

# gazelle:include_dep @pypi//psycopg

_GO_TEST = get_required_path(
    own_repo_rlocation("agentplane/egress/history/store_integration_test_/store_integration_test")
)
_MIGRATOR = get_required_path(
    own_repo_rlocation("agentplane/egress/history_migrations/cmd/egress-history-migrate_/egress-history-migrate")
)


@pytest.mark.parametrize("alembic_first", [True, False], ids=["handed-off-from-alembic", "fresh-goose"])
def test_go_history_against_each_schema(
    postgres_admin_url: str, database_names: DatabaseNames, alembic_first: bool
) -> None:
    with create_temporary_database(postgres_admin_url, database_names()) as url:
        if alembic_first:
            EGRESS_ALEMBIC.apply(url)
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
