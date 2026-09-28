"""`ImagePolicy` rejects at construction the `interval`/`digestReflectionPolicy` combinations
the CRD's validation rejects at apply."""

import pytest
import pytest_bazel
from cdk8s import ApiObjectMetadata, Testing as Cdk8sTesting
from flux_imagepolicy_crds.io.fluxcd.toolkit.image import (
    ImagePolicySpecDigestReflectionPolicy,
    ImagePolicySpecImageRepositoryRef,
    ImagePolicySpecPolicy,
    ImagePolicySpecPolicyAlphabetical,
)

from cluster.cdk8s.providers.flux.image_policy import ImagePolicy


def _policy(
    *, digest_reflection_policy: ImagePolicySpecDigestReflectionPolicy | None, interval: str | None
) -> ImagePolicy:
    return ImagePolicy(
        Cdk8sTesting.chart(),
        "policy",
        metadata=ApiObjectMetadata(name="test-image", namespace="test-namespace"),
        image_repository_ref=ImagePolicySpecImageRepositoryRef(name="test-image"),
        policy=ImagePolicySpecPolicy(alphabetical=ImagePolicySpecPolicyAlphabetical()),
        digest_reflection_policy=digest_reflection_policy,
        interval=interval,
    )


@pytest.mark.parametrize(
    ("digest_reflection_policy", "interval"),
    [
        (None, "5m"),
        (ImagePolicySpecDigestReflectionPolicy.IF_NOT_PRESENT, "5m"),
        (ImagePolicySpecDigestReflectionPolicy.ALWAYS, None),
    ],
)
def test_interval_without_always_digest_reflection_raises(
    digest_reflection_policy: ImagePolicySpecDigestReflectionPolicy | None, interval: str | None
) -> None:
    with pytest.raises(ValueError, match="interval"):
        _policy(digest_reflection_policy=digest_reflection_policy, interval=interval)


def test_always_digest_reflection_renders_its_interval() -> None:
    (policy,) = Cdk8sTesting.synth(
        _policy(digest_reflection_policy=ImagePolicySpecDigestReflectionPolicy.ALWAYS, interval="5m").chart
    )
    assert policy["spec"]["digestReflectionPolicy"] == "Always"
    assert policy["spec"]["interval"] == "5m"


if __name__ == "__main__":
    pytest_bazel.main()
