import pytest_bazel
from cdk8s import Testing as Cdk8sTesting

from cluster.cdk8s.providers.seaweedfs.s3 import Bucket, Identity

_CLUSTER_NAME = "test-seaweed"
_CLUSTER_NAMESPACE = "test-seaweedfs"


def _of_kind(manifests: list[dict], kind: str) -> list[dict]:
    return [manifest for manifest in manifests if manifest["kind"] == kind]


def test_bucket_renders_expected_spec() -> None:
    chart = Cdk8sTesting.chart()
    Bucket(
        chart,
        "bucket",
        name="my-bucket",
        namespace=_CLUSTER_NAMESPACE,
        cluster_name=_CLUSTER_NAME,
        cluster_namespace=_CLUSTER_NAMESPACE,
        adopt_existing=False,
    )
    (bucket,) = _of_kind(Cdk8sTesting.synth(chart), "Bucket")
    assert bucket["spec"] == {
        "name": "my-bucket",
        "clusterRef": {"name": _CLUSTER_NAME, "namespace": _CLUSTER_NAMESPACE},
        "reclaimPolicy": "Retain",
    }


def test_bucket_adopt_existing_true_sets_flag() -> None:
    chart = Cdk8sTesting.chart()
    Bucket(
        chart,
        "bucket",
        name="my-bucket",
        namespace=_CLUSTER_NAMESPACE,
        cluster_name=_CLUSTER_NAME,
        cluster_namespace=_CLUSTER_NAMESPACE,
        adopt_existing=True,
    )
    (bucket,) = _of_kind(Cdk8sTesting.synth(chart), "Bucket")
    assert bucket["spec"]["adoptExisting"] is True


def test_identity_in_cluster_namespace_omits_seaweed_ref_namespace() -> None:
    chart = Cdk8sTesting.chart()
    Identity(
        chart,
        "identity",
        name="my-identity",
        namespace=_CLUSTER_NAMESPACE,
        cluster_name=_CLUSTER_NAME,
        cluster_namespace=_CLUSTER_NAMESPACE,
    )
    (identity,) = _of_kind(Cdk8sTesting.synth(chart), "S3Identity")
    assert identity["spec"]["seaweedRef"] == {"name": _CLUSTER_NAME}


def test_identity_in_tenant_namespace_names_the_cluster_namespace() -> None:
    chart = Cdk8sTesting.chart()
    Identity(
        chart,
        "identity",
        name="my-identity",
        namespace="tenant",
        cluster_name=_CLUSTER_NAME,
        cluster_namespace=_CLUSTER_NAMESPACE,
    )
    (identity,) = _of_kind(Cdk8sTesting.synth(chart), "S3Identity")
    assert identity["spec"]["seaweedRef"] == {"name": _CLUSTER_NAME, "namespace": _CLUSTER_NAMESPACE}


if __name__ == "__main__":
    pytest_bazel.main()
