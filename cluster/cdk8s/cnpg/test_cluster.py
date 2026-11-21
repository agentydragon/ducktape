import pytest
import pytest_bazel
from cdk8s import ApiObjectMetadata, Chart
from cnpg_database_crds.io.cnpg.postgresql import DatabaseSpecDatabaseReclaimPolicy

from cluster.cdk8s.cnpg import cluster


def test_database_cluster_namespace_and_reclaim_policy(scratch_chart: Chart) -> None:
    postgres = cluster.cluster(
        scratch_chart,
        "postgres",
        ref=cluster.PostgresRef.generated(name="test-postgres", namespace="test-data"),
        profile=cluster.OVH_PIRAEUS_SSD,
        size="1Gi",
        initdb=None,
        wal_archive=False,
    )
    with pytest.raises(ValueError, match="same namespace"):
        cluster.Database(
            scratch_chart,
            "wrong-namespace",
            metadata=ApiObjectMetadata(name="test-database", namespace="test-other"),
            cluster=postgres,
            name="test_db",
            owner="test_owner",
        )

    disposable = cluster.Database(
        scratch_chart,
        "disposable",
        metadata=ApiObjectMetadata(name="test-database", namespace="test-data"),
        cluster=postgres,
        name="test_db",
        owner="test_owner",
        database_reclaim_policy=DatabaseSpecDatabaseReclaimPolicy.DELETE,
    )
    assert disposable.to_json()["spec"]["databaseReclaimPolicy"] == "delete"


if __name__ == "__main__":
    pytest_bazel.main()
