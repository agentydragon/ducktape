"""Exercise a committed Dispatcher queue row through the Go Gateway tunnel."""

import os
import subprocess

import pytest_bazel
from sqlalchemy.engine import make_url

from util.bazel.runfiles import get_required_path, own_repo_rlocation
from util.testing.postgres import DatabaseNames, create_temporary_database

# gazelle:include_dep @pypi//psycopg

_GO_TEST = get_required_path(
    own_repo_rlocation(
        "agentplane/runner_gateway/go/gateway/queue_tunnel_integration_test_/queue_tunnel_integration_test"
    )
)
_MIGRATOR = get_required_path(
    own_repo_rlocation(
        "agentplane/runner_gateway/go/migrations/cmd/runner-gateway-queue-migrate_/runner-gateway-queue-migrate"
    )
)


def test_queued_delivery_to_runner_tunnel(postgres_admin_url: str, database_names: DatabaseNames) -> None:
    with create_temporary_database(postgres_admin_url, database_names()) as url:
        wire_url = (
            make_url(url)
            .set(drivername="postgresql", query={"sslmode": "disable"})
            .render_as_string(hide_password=False)
        )
        subprocess.run(
            [str(_MIGRATOR), "--database-url", wire_url], capture_output=True, text=True, timeout=60, check=True
        )
        subprocess.run(
            [str(_GO_TEST), "-test.run", "^TestQueuedDeliveryThroughGatewayTunnel$", "-test.v"],
            env={**os.environ, "AGENTPLANE_TEST_DATABASE_URL": wire_url},
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )


if __name__ == "__main__":
    pytest_bazel.main()
