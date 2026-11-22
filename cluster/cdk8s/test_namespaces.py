import json
from collections.abc import Callable

import pytest
import pytest_bazel
from cdk8s import Chart
from cdk8s_plus_34 import Namespace, k8s

from cluster.cdk8s.namespace_access import AgentReadable
from cluster.cdk8s.namespaces import VPA_UPDATE_MODE_LABEL, Vpa, VpaProfile, namespace, namespace_patch
from cluster.cdk8s.vpa_policy import VPA_RESOURCE_POLICY_ANNOTATION, ContainerPolicy, ControlledValues


@pytest.mark.parametrize("key", [AgentReadable.LOGS, "goldilocks.fairwinds.com/enabled"])
def test_extra_labels_cannot_restate_the_policy_labels(scratch_chart: Chart, key: str) -> None:
    with pytest.raises(ValueError, match=key):
        namespace(scratch_chart, "namespace", name="test-ns", vpa=Vpa.AUTO, labels={key: "false"})


@pytest.mark.parametrize("construct", [namespace, namespace_patch])
@pytest.mark.parametrize("vpa", [Vpa.AUTO, VpaProfile(Vpa.AUTO, min_replicas=1)])
def test_auto_defaults_to_requests_only(
    scratch_chart: Chart, construct: Callable[..., Namespace | k8s.KubeNamespace], vpa: Vpa | VpaProfile
) -> None:
    rendered = construct(scratch_chart, "namespace", name="test-ns", vpa=vpa)
    annotations = rendered.metadata.to_json()["annotations"]
    assert json.loads(annotations[VPA_RESOURCE_POLICY_ANNOTATION]) == {
        "containerPolicies": [{"containerName": "*", "controlledValues": "RequestsOnly"}]
    }
    if isinstance(vpa, VpaProfile):
        assert annotations["goldilocks.fairwinds.com/vpa-min-replicas"] == "1"


@pytest.mark.parametrize("construct", [namespace, namespace_patch])
def test_auto_preserves_explicit_annotation(
    scratch_chart: Chart, construct: Callable[..., Namespace | k8s.KubeNamespace]
) -> None:
    explicit = '{"containerPolicies": [{"containerName": "*", "controlledValues": "RequestsAndLimits"}]}'
    rendered = construct(
        scratch_chart, "namespace", name="test-ns", vpa=Vpa.AUTO, annotations={VPA_RESOURCE_POLICY_ANNOTATION: explicit}
    )
    assert rendered.metadata.to_json()["annotations"][VPA_RESOURCE_POLICY_ANNOTATION] == explicit


@pytest.mark.parametrize("vpa", [Vpa.DISABLED, Vpa.RECOMMEND, Vpa.INITIAL])
def test_non_auto_does_not_acquire_a_default_policy(scratch_chart: Chart, vpa: Vpa) -> None:
    rendered = namespace(scratch_chart, "namespace", name="test-ns", vpa=vpa)
    assert VPA_RESOURCE_POLICY_ANNOTATION not in rendered.metadata.to_json().get("annotations", {})


def test_vpa_profile_renders_explicit_container_policies(scratch_chart: Chart) -> None:
    rendered = namespace(
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

    metadata = rendered.metadata.to_json()
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
