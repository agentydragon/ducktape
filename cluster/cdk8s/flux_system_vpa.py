"""Admission-only sizing for the bootstrapped Flux controllers."""

from cdk8s import ApiObjectMetadata, App, Chart
from vpa_crds.io.k8s.autoscaling import (
    VerticalPodAutoscaler,
    VerticalPodAutoscalerSpec,
    VerticalPodAutoscalerSpecResourcePolicy,
    VerticalPodAutoscalerSpecResourcePolicyContainerPolicies,
    VerticalPodAutoscalerSpecTargetRef,
    VerticalPodAutoscalerSpecUpdatePolicy,
)

from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "flux-system-vpa"
OUTPUT_DIR = f"{GENERATED_ROOT}/{NAME}"

_DEPLOYMENTS = (
    "helm-controller",
    "image-automation-controller",
    "image-reflector-controller",
    "kustomize-controller",
    "notification-controller",
    "source-controller",
    "source-watcher",
)


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    for deployment in _DEPLOYMENTS:
        VerticalPodAutoscaler(
            chart,
            deployment,
            metadata=ApiObjectMetadata(name=deployment, namespace="flux-system"),
            spec=VerticalPodAutoscalerSpec(
                target_ref=VerticalPodAutoscalerSpecTargetRef(
                    api_version="apps/v1", kind="Deployment", name=deployment
                ),
                update_policy=VerticalPodAutoscalerSpecUpdatePolicy(update_mode="Initial"),
                resource_policy=VerticalPodAutoscalerSpecResourcePolicy(
                    container_policies=[
                        VerticalPodAutoscalerSpecResourcePolicyContainerPolicies(
                            container_name="manager",
                            controlled_resources=["cpu", "memory"],
                            controlled_values="RequestsOnly",
                        ),
                        VerticalPodAutoscalerSpecResourcePolicyContainerPolicies(container_name="*", mode="Off"),
                    ]
                ),
            ),
        )
    return chart
