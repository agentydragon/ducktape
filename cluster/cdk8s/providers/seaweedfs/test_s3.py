import pytest_bazel
from cdk8s import Testing as Cdk8sTesting

from cluster.cdk8s.providers.seaweedfs.s3 import AWS_ENV_KEY_FIELDS, Bucket, Identity, IdentityRef

_CLUSTER_NAME = "test-seaweed"
_CLUSTER_NAMESPACE = "test-seaweedfs"


def _of_kind(manifests: list[dict], kind: str) -> list[dict]:
    return [manifest for manifest in manifests if manifest["kind"] == kind]


def test_bucket_in_cluster_namespace_creates_no_grant() -> None:
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
    assert not _of_kind(Cdk8sTesting.synth(chart), "ResourceReferenceGrant")


def test_bucket_outside_cluster_namespace_creates_grant() -> None:
    chart = Cdk8sTesting.chart()
    Bucket(
        chart,
        "bucket",
        name="my-bucket",
        namespace="tenant",
        cluster_name=_CLUSTER_NAME,
        cluster_namespace=_CLUSTER_NAMESPACE,
        adopt_existing=False,
    )
    (grant,) = _of_kind(Cdk8sTesting.synth(chart), "ResourceReferenceGrant")
    assert grant["metadata"]["namespace"] == _CLUSTER_NAMESPACE
    assert grant["spec"]["from"] == [{"group": "seaweed.seaweedfs.com", "kind": "Bucket", "namespace": "tenant"}]
    assert grant["spec"]["to"] == [{"group": "seaweed.seaweedfs.com", "kind": "Seaweed", "name": _CLUSTER_NAME}]


def test_constructs_in_the_same_namespace_share_one_grant() -> None:
    chart = Cdk8sTesting.chart()
    Bucket(
        chart,
        "bucket",
        name="my-bucket",
        namespace="tenant",
        cluster_name=_CLUSTER_NAME,
        cluster_namespace=_CLUSTER_NAMESPACE,
        adopt_existing=False,
    )
    Identity(
        chart,
        "identity",
        name="my-identity",
        namespace="tenant",
        cluster_name=_CLUSTER_NAME,
        cluster_namespace=_CLUSTER_NAMESPACE,
    )
    (grant,) = _of_kind(Cdk8sTesting.synth(chart), "ResourceReferenceGrant")
    assert [entry["kind"] for entry in grant["spec"]["from"]] == ["Bucket", "S3Identity"]


def test_identity_seaweed_ref_omits_namespace_within_the_cluster_namespace() -> None:
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


def test_identity_ref_credentials_seaweed_ref_names_the_cluster_namespace_from_a_tenant() -> None:
    chart = Cdk8sTesting.chart()
    identity_ref = IdentityRef(
        chart, "identity", name="external-identity", cluster_name=_CLUSTER_NAME, cluster_namespace=_CLUSTER_NAMESPACE
    )
    identity_ref.credentials(namespace="tenant", secret="creds", key_fields=AWS_ENV_KEY_FIELDS)
    (credentials,) = _of_kind(Cdk8sTesting.synth(chart), "S3Credentials")
    assert credentials["spec"]["seaweedRef"] == {"name": _CLUSTER_NAME, "namespace": _CLUSTER_NAMESPACE}
    assert credentials["spec"]["secretRef"]["accessKeyField"] == "AWS_ACCESS_KEY_ID"
    assert credentials["spec"]["secretRef"]["secretKeyField"] == "AWS_SECRET_ACCESS_KEY"


def test_bucket_grant_read_write_and_grant_read_append_access_entries() -> None:
    chart = Cdk8sTesting.chart()
    bucket = Bucket(
        chart,
        "bucket",
        name="my-bucket",
        namespace=_CLUSTER_NAMESPACE,
        cluster_name=_CLUSTER_NAME,
        cluster_namespace=_CLUSTER_NAMESPACE,
        adopt_existing=False,
    )
    reader = IdentityRef(
        chart, "reader", name="reader-identity", cluster_name=_CLUSTER_NAME, cluster_namespace=_CLUSTER_NAMESPACE
    )
    bucket.grant_read(reader)
    bucket.grant_read_write("anonymous")
    (rendered,) = _of_kind(Cdk8sTesting.synth(chart), "Bucket")
    assert rendered["spec"]["access"] == [
        {"user": "reader-identity", "actions": ["Read", "List"]},
        {"user": "anonymous", "actions": ["Read", "Write", "List", "Tagging"]},
    ]


if __name__ == "__main__":
    pytest_bazel.main()
