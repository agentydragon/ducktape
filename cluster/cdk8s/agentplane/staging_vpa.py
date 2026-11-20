"""Admission-time memory sizing for the static Agentplane staging services.

The namespace opts out of Goldilocks because it also contains dynamic Sandboxes.
These VPAs are Flux-owned and target only the listed Deployments.
"""

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

from cluster.cdk8s.agentplane.namespaces import STAGING_NAMESPACE
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "agentplane-staging-vpa"
OUTPUT_DIR = f"{GENERATED_ROOT}/agentplane-staging/vpa"

# (Deployment, container, minimum request, maximum request). These bounded
# ceilings limit the effect on OVH scheduling; the Pod memory limits are unchanged.
_WORKLOADS = (
    ("actions", "actions", "128Mi", "160Mi"),
    ("app", "app", "128Mi", "160Mi"),
    ("electric", "electric", "448Mi", "576Mi"),
    ("environments", "environments", "64Mi", "112Mi"),
    ("frontend", "frontend", "32Mi", "40Mi"),
    ("history-service", "history-service", "128Mi", "256Mi"),
    ("llm-ingress", "ingress", "128Mi", "160Mi"),
    ("mcp-edge", "mcp-edge", "128Mi", "160Mi"),
    ("notifications", "notifications", "128Mi", "160Mi"),
    ("policy-controller", "policy-controller", "64Mi", "112Mi"),
    ("sessions", "sessions", "256Mi", "320Mi"),
)


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    for deployment, container, floor, ceiling in _WORKLOADS:
        VerticalPodAutoscaler(
            chart,
            deployment,
            metadata=ApiObjectMetadata(name=deployment, namespace=STAGING_NAMESPACE),
            spec=VerticalPodAutoscalerSpec(
                target_ref=VerticalPodAutoscalerSpecTargetRef(
                    api_version="apps/v1", kind="Deployment", name=deployment
                ),
                update_policy=VerticalPodAutoscalerSpecUpdatePolicy(update_mode="Initial"),
                resource_policy=VerticalPodAutoscalerSpecResourcePolicy(
                    container_policies=[
                        VerticalPodAutoscalerSpecResourcePolicyContainerPolicies(
                            container_name=container,
                            controlled_resources=["memory"],
                            controlled_values="RequestsOnly",
                            min_allowed={
                                "memory": VerticalPodAutoscalerSpecResourcePolicyContainerPoliciesMinAllowed.from_string(
                                    floor
                                )
                            },
                            max_allowed={
                                "memory": VerticalPodAutoscalerSpecResourcePolicyContainerPoliciesMaxAllowed.from_string(
                                    ceiling
                                )
                            },
                        ),
                        # Future sidecars do not silently acquire a resource policy.
                        VerticalPodAutoscalerSpecResourcePolicyContainerPolicies(container_name="*", mode="Off"),
                    ]
                ),
            ),
        )
    return chart
