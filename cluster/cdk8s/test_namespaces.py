import json

import pytest
import pytest_bazel
from cdk8s import Chart, Testing as Cdk8sTesting

from cluster.cdk8s.namespace_access import AgentReadable
from cluster.cdk8s.namespaces import VPA_UPDATE_MODE_LABEL, Vpa, VpaProfile, namespace
from cluster.cdk8s.vpa_policy import VPA_RESOURCE_POLICY_ANNOTATION, ContainerPolicy, ControlledValues


@pytest.mark.parametrize("key", [AgentReadable.LOGS, "goldilocks.fairwinds.com/enabled"])
def test_extra_labels_cannot_restate_the_policy_labels(scratch_chart: Chart, key: str) -> None:
    with pytest.raises(ValueError, match=key):
        namespace(scratch_chart, "namespace", name="test-ns", vpa=Vpa.AUTO, labels={key: "false"})


def test_vpa_profile_renders_explicit_container_policies(scratch_chart: Chart) -> None:
    namespace(
        scratch_chart,
        "namespace",
        name="test-ns",
        vpa=VpaProfile(
            Vpa.AUTO,
            (
                ContainerPolicy(
                    controlled_values=ControlledValues.REQUESTS_AND_LIMITS,
                    controlled_resources=("memory",),
                    min_allowed={"memory": "128Mi"},
                ),
                ContainerPolicy(container_name="sidecar", controlled_values=ControlledValues.REQUESTS_ONLY, mode="Off"),
            ),
            min_replicas=1,
        ),
    )

    [rendered] = Cdk8sTesting.synth(scratch_chart)
    metadata = rendered["metadata"]
    assert metadata["labels"][VPA_UPDATE_MODE_LABEL] == "auto"
    assert metadata["annotations"]["goldilocks.fairwinds.com/vpa-min-replicas"] == "1"
    assert json.loads(metadata["annotations"][VPA_RESOURCE_POLICY_ANNOTATION]) == {
        "containerPolicies": [
            {
                "containerName": "*",
                "controlledResources": ["memory"],
                "controlledValues": "RequestsAndLimits",
                "minAllowed": {"memory": "128Mi"},
            },
            {"containerName": "sidecar", "controlledValues": "RequestsOnly", "mode": "Off"},
        ]
    }


if __name__ == "__main__":
    pytest_bazel.main()
