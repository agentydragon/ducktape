import pytest
import pytest_bazel
from cdk8s import Testing as Cdk8sTesting  # pytest auto-collects classes named Test*

from cluster.cdk8s.seaweedfs import cluster, namespace, s3

_TENANT = min(cluster.TENANTS)
_NOT_A_TENANT = "test-not-a-tenant"


def test_seaweed_ref_names_the_seaweedfs_namespace_only_from_a_tenant() -> None:
    chart = Cdk8sTesting.chart()
    s3.identity(chart, "local", name="test-local")
    s3.credentials(
        chart, "local-credentials", identity="test-local", namespace=namespace.NAME, secret="test", key_fields=None
    )
    s3.identity(chart, "tenant", name="test-tenant", namespace=_TENANT)
    s3.credentials(
        chart, "tenant-credentials", identity="test-tenant", namespace=_TENANT, secret="test", key_fields=None
    )
    in_cluster_namespace = {"name": cluster.NAME}
    from_tenant = {"name": cluster.NAME, "namespace": namespace.NAME}
    assert [(o["kind"], o["spec"]["seaweedRef"]) for o in Cdk8sTesting.synth(chart)] == [
        ("S3Identity", in_cluster_namespace),
        ("S3Credentials", in_cluster_namespace),
        ("S3Identity", from_tenant),
        ("S3Credentials", from_tenant),
    ]


def test_s3_objects_refuse_a_namespace_the_tenants_grant_does_not_admit() -> None:
    chart = Cdk8sTesting.chart()
    with pytest.raises(ValueError, match="TENANTS"):
        s3.bucket(chart, "bucket", name="test", namespace=_NOT_A_TENANT, access=[], adopt_existing=False)
    with pytest.raises(ValueError, match="TENANTS"):
        s3.identity(chart, "identity", name="test", namespace=_NOT_A_TENANT)
    with pytest.raises(ValueError, match="TENANTS"):
        s3.credentials(chart, "credentials", identity="test", namespace=_NOT_A_TENANT, secret="test", key_fields=None)


if __name__ == "__main__":
    pytest_bazel.main()
