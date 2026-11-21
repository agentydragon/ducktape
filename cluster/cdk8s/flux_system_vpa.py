"""Admission-only sizing for the bootstrapped Flux controllers."""

from cdk8s import ApiObjectMetadata, App, Chart
from vpa_crds.io.k8s.autoscaling import (
    VerticalPodAutoscaler,
    VerticalPodAutoscalerSpec,
    VerticalPodAutoscalerSpecResourcePolicy,
    VerticalPodAutoscalerSpecResourcePolicyContainerPolicies,
    VerticalPodAutoscalerSpecResourcePolicyContainerPoliciesMaxAllowed,
    VerticalPodAutoscalerSpecResourcePolicyContainerPoliciesMinAllowed,
    VerticalPodAutoscalerSpecTargetRef,
    VerticalPodAutoscalerSpecUpdatePolicy,
)

from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "flux-system-vpa"
OUTPUT_DIR = f"{GENERATED_ROOT}/{NAME}"

# Floors are the bootstrap Deployment requests. Ceilings use the 30-day
# observed peaks plus room up to the existing 1 CPU and 1 GiB container limits.
_WORKLOADS = (
    ("helm-controller", "100m", "175m", "384Mi"),
    ("image-automation-controller", "100m", "1", "1Gi"),
    ("image-reflector-controller", "100m", "125m", "864Mi"),
    ("kustomize-controller", "100m", "1", "704Mi"),
    ("notification-controller", "100m", "125m", "192Mi"),
    ("source-controller", "50m", "1", "1Gi"),
    ("source-watcher", "50m", "100m", "128Mi"),
)


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    for deployment, cpu_floor, cpu_ceiling, memory_ceiling in _WORKLOADS:
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
                            min_allowed={
                                "cpu": VerticalPodAutoscalerSpecResourcePolicyContainerPoliciesMinAllowed.from_string(
                                    cpu_floor
                                ),
                                "memory": VerticalPodAutoscalerSpecResourcePolicyContainerPoliciesMinAllowed.from_string(
                                    "64Mi"
                                ),
                            },
                            max_allowed={
                                "cpu": VerticalPodAutoscalerSpecResourcePolicyContainerPoliciesMaxAllowed.from_string(
                                    cpu_ceiling
                                ),
                                "memory": VerticalPodAutoscalerSpecResourcePolicyContainerPoliciesMaxAllowed.from_string(
                                    memory_ceiling
                                ),
                            },
                        ),
                        VerticalPodAutoscalerSpecResourcePolicyContainerPolicies(container_name="*", mode="Off"),
                    ]
                ),
            ),
        )
    return chart
