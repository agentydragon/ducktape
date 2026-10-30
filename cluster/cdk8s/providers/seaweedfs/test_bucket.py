from collections.abc import Callable

import pytest_bazel
from cdk8s import ApiObjectMetadata, Chart, Testing as Cdk8sTesting  # pytest auto-collects classes named Test*
from seaweed_bucket_crds.com.seaweedfs.seaweed import BucketSpecClusterRef

from cluster.cdk8s.providers.seaweedfs.bucket import Bucket


def test_bucket_is_physically_named_after_its_metadata_and_leaves_empty_fields_unset(
    scratch_chart: Callable[[], Chart],
) -> None:
    chart = scratch_chart()
    Bucket(
        chart,
        "bucket",
        metadata=ApiObjectMetadata(name="test-bucket", namespace="test-tenant"),
        cluster_ref=BucketSpecClusterRef(name="test-seaweed"),
        access=[],
        adopt_existing=False,
        reclaim_policy=None,
    )
    (bucket,) = Cdk8sTesting.synth(chart)
    assert bucket["spec"] == {"name": "test-bucket", "clusterRef": {"name": "test-seaweed"}}


if __name__ == "__main__":
    pytest_bazel.main()
