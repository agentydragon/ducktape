import pytest
import pytest_bazel
from cdk8s import ApiObjectMetadata, Testing as Cdk8sTesting
from kyverno_clusterpolicy_crds.io.kyverno import ClusterPolicySpecValidationFailureAction

from cluster.cdk8s.providers.kyverno.cluster_policy import ClusterPolicy


# cdk8s_import's jsii-generated enum for this field collapses the CRD schema's
# duplicate-cased members (`audit`/`Audit`, `enforce`/`Enforce`) to one, and serializes
# it as the lowercase spelling -- still a value the CRD schema and Kyverno itself
# accept, so the field passes straight through with no escape hatch needed.
@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (ClusterPolicySpecValidationFailureAction.AUDIT, "audit"),
        (ClusterPolicySpecValidationFailureAction.ENFORCE, "enforce"),
    ],
)
def test_validation_failure_action_passes_through(
    action: ClusterPolicySpecValidationFailureAction, expected: str
) -> None:
    chart = Cdk8sTesting.chart()
    ClusterPolicy(
        chart, "test", metadata=ApiObjectMetadata(name="test-policy"), rules=[], validation_failure_action=action
    )
    (manifest,) = Cdk8sTesting.synth(chart)
    assert manifest["spec"]["validationFailureAction"] == expected


if __name__ == "__main__":
    pytest_bazel.main()
