"""The production migrator serializes init containers on a fresh database."""

import subprocess

import pytest_bazel
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from util.bazel.runfiles import get_required_path, own_repo_rlocation
from util.testing.postgres import DatabaseNames, create_temporary_database

# SQLAlchemy loads this URL dialect dynamically.
# gazelle:include_dep @pypi//psycopg

_MIGRATOR = get_required_path(
    own_repo_rlocation("agentplane/action_service/migrations/cmd/actions-migrate_/actions-migrate")
)


def test_concurrent_and_repeated_init_containers(postgres_admin_url: str, database_names: DatabaseNames) -> None:
    with create_temporary_database(postgres_admin_url, database_names()) as url:
        wire_url = (
            make_url(url)
            .set(drivername="postgresql", query={"sslmode": "disable"})
            .render_as_string(hide_password=False)
        )
        env = {"AGENTPLANE_ACTIONS_DATABASE_URL": wire_url}
        runners = [
            subprocess.Popen([str(_MIGRATOR)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for _ in range(2)
        ]
        for runner in runners:
            stdout, stderr = runner.communicate(timeout=60)
            assert runner.returncode == 0, (stdout, stderr)

        repeated = subprocess.run([str(_MIGRATOR)], env=env, capture_output=True, text=True, check=False)
        assert repeated.returncode == 0, (repeated.stdout, repeated.stderr)

        engine = create_engine(url)
        try:
            with engine.connect() as conn:
                assert (
                    conn.scalar(
                        text("SELECT count(*) FROM action_service_goose_version WHERE version_id = 7 AND is_applied")
                    )
                    == 1
                )
                assert (
                    conn.scalar(text("SELECT max(version_id) FROM action_service_goose_version WHERE is_applied")) == 7
                )
                assert (
                    conn.scalar(
                        text(
                            "SELECT count(*) FROM information_schema.columns WHERE table_schema = current_schema() "
                            "AND (table_name, column_name) IN "
                            "(('action_request', 'action'), ('action_event', 'actor'), "
                            "('action_decision', 'policy_evidence'))"
                        )
                    )
                    == 0
                )
        finally:
            engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
