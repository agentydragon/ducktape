import pytest
import pytest_bazel
from cdk8s import (
    Chart,
    Testing as Cdk8sTesting,  # pytest auto-collects classes named Test*
)

from cluster.cdk8s.seaweedfs import cluster, namespace, s3

_TENANT = min(cluster.TENANTS)
_NOT_A_TENANT = "test-not-a-tenant"


def test_seaweed_ref_names_the_seaweedfs_namespace_only_from_a_tenant(scratch_chart: Chart) -> None:
    s3.identity(scratch_chart, "local", name="test-local")
    s3.credentials(
        scratch_chart,
        "local-credentials",
        identity="test-local",
        namespace=namespace.NAME,
        secret="test",
        key_fields=None,
    )
    s3.identity(scratch_chart, "tenant", name="test-tenant", namespace=_TENANT)
    s3.credentials(
        scratch_chart, "tenant-credentials", identity="test-tenant", namespace=_TENANT, secret="test", key_fields=None
    )
    in_cluster_namespace = {"name": cluster.NAME}
    from_tenant = {"name": cluster.NAME, "namespace": namespace.NAME}
    assert [(o["kind"], o["spec"]["seaweedRef"]) for o in Cdk8sTesting.synth(scratch_chart)] == [
        ("S3Identity", in_cluster_namespace),
        ("S3Credentials", in_cluster_namespace),
        ("S3Identity", from_tenant),
        ("S3Credentials", from_tenant),
    ]


def test_s3_objects_refuse_a_namespace_the_tenants_grant_does_not_admit(scratch_chart: Chart) -> None:
    with pytest.raises(ValueError, match="TENANTS"):
        s3.bucket(scratch_chart, "bucket", name="test", namespace=_NOT_A_TENANT, access=[], adopt_existing=False)
    with pytest.raises(ValueError, match="TENANTS"):
        s3.identity(scratch_chart, "identity", name="test", namespace=_NOT_A_TENANT)
    with pytest.raises(ValueError, match="TENANTS"):
        s3.credentials(
            scratch_chart, "credentials", identity="test", namespace=_NOT_A_TENANT, secret="test", key_fields=None
        )


if __name__ == "__main__":
    pytest_bazel.main()
