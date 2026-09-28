import pytest_bazel
from cdk8s import Testing as Cdk8sTesting  # pytest auto-collects classes named Test*

from cluster.cdk8s.seaweedfs import cluster, namespace, s3


def test_seaweed_ref_names_the_seaweedfs_namespace_only_from_a_tenant() -> None:
    chart = Cdk8sTesting.chart()
    s3.identity(chart, "local", name="test-local")
    s3.credentials(
        chart, "local-credentials", identity="test-local", namespace=namespace.NAME, secret="test", key_fields=None
    )
    s3.identity(chart, "tenant", name="test-tenant", namespace="test-tenant")
    s3.credentials(
        chart, "tenant-credentials", identity="test-tenant", namespace="test-tenant", secret="test", key_fields=None
    )
    in_cluster_namespace = {"name": cluster.NAME}
    from_tenant = {"name": cluster.NAME, "namespace": namespace.NAME}
    assert [(o["kind"], o["spec"]["seaweedRef"]) for o in Cdk8sTesting.synth(chart)] == [
        ("S3Identity", in_cluster_namespace),
        ("S3Credentials", in_cluster_namespace),
        ("S3Identity", from_tenant),
        ("S3Credentials", from_tenant),
    ]


if __name__ == "__main__":
    pytest_bazel.main()
