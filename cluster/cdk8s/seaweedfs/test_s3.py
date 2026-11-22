import pytest
import pytest_bazel
from cdk8s import Chart

from cluster.cdk8s.seaweedfs import cluster, namespace, s3

_TENANT = min(cluster.TENANTS)
_NOT_A_TENANT = "test-not-a-tenant"


@pytest.mark.parametrize(
    ("owner", "expected_ref"),
    [(namespace.NAME, {"name": cluster.NAME}), (_TENANT, {"name": cluster.NAME, "namespace": namespace.NAME})],
)
def test_s3_objects_reference_the_seaweed_cluster(
    scratch_chart: Chart, owner: str, expected_ref: dict[str, str]
) -> None:
    identity = s3.identity(scratch_chart, "identity", name="test", namespace=owner)
    credentials = s3.credentials(
        scratch_chart, "credentials", identity=identity.name, namespace=owner, secret="test", key_fields=None
    )
    for resource in (identity, credentials):
        assert resource.to_json()["spec"]["seaweedRef"] == expected_ref


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
