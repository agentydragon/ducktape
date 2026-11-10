import pytest_bazel
from cdk8s import App, Testing as Cdk8sTesting

from cluster.cdk8s.seaweedfs import pr_visuals_bucket


def test_record_cache_bucket_is_private_and_reuses_the_existing_writer(scratch_app: App) -> None:
    resources = Cdk8sTesting.synth(pr_visuals_bucket.chart(scratch_app))
    record_cache = next(
        resource
        for resource in resources
        if resource["kind"] == "Bucket" and resource["metadata"]["name"] == pr_visuals_bucket.RECORD_CACHE_BUCKET
    )
    assert record_cache["metadata"]["namespace"] == "flux-system"
    assert record_cache["spec"]["access"] == [
        {"actions": ["Read", "Write", "List", "Tagging"], "user": "pr-visuals-writer"}
    ]
    assert record_cache["spec"].get("adoptExisting", False) is False
    assert record_cache["spec"]["reclaimPolicy"] == "Retain"

    identities = [resource for resource in resources if resource["kind"] == "S3Identity"]
    credentials = [resource for resource in resources if resource["kind"] == "S3Credentials"]
    assert [resource["metadata"]["name"] for resource in identities] == ["pr-visuals-writer"]
    assert [resource["metadata"]["name"] for resource in credentials] == ["pr-visuals-writer"]
    assert identities[0]["metadata"]["namespace"] == "flux-system"
    assert credentials[0]["metadata"]["namespace"] == "flux-system"
    assert credentials[0]["spec"]["identityRef"]["name"] == identities[0]["metadata"]["name"]
    assert credentials[0]["spec"]["secretRef"]["name"] == "pr-visuals-s3-credentials"


if __name__ == "__main__":
    pytest_bazel.main()
