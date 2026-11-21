"""Resource request sizing for the static Agentplane staging services.

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

# (Deployment, container, memory floor, memory ceiling, controlled resources).
_CPU_AND_MEMORY = ("cpu", "memory")
_MEMORY = ("memory",)
_WORKLOADS = (
    ("actions", "actions", "128Mi", "160Mi", _CPU_AND_MEMORY),
    ("app", "app", "128Mi", "160Mi", _CPU_AND_MEMORY),
    ("electric", "electric", "448Mi", "576Mi", _MEMORY),
    ("environments", "environments", "64Mi", "112Mi", _CPU_AND_MEMORY),
    ("frontend", "frontend", None, None, _CPU_AND_MEMORY),
    ("history-service", "history-service", "128Mi", "256Mi", _CPU_AND_MEMORY),
    ("llm-ingress", "ingress", "128Mi", "160Mi", _CPU_AND_MEMORY),
    ("mcp-edge", "mcp-edge", "128Mi", "160Mi", _CPU_AND_MEMORY),
    ("notifications", "notifications", "128Mi", "160Mi", _CPU_AND_MEMORY),
    ("policy-controller", "policy-controller", "64Mi", "112Mi", _CPU_AND_MEMORY),
    ("sessions", "sessions", "256Mi", "320Mi", _CPU_AND_MEMORY),
)


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    for deployment, container, memory_floor, memory_ceiling, resources in _WORKLOADS:
        VerticalPodAutoscaler(
            chart,
            deployment,
            metadata=ApiObjectMetadata(name=deployment, namespace=STAGING_NAMESPACE),
            spec=VerticalPodAutoscalerSpec(
                target_ref=VerticalPodAutoscalerSpecTargetRef(
                    api_version="apps/v1", kind="Deployment", name=deployment
                ),
                update_policy=VerticalPodAutoscalerSpecUpdatePolicy(
                    update_mode="Recreate" if deployment == "frontend" else "Initial"
                ),
                resource_policy=VerticalPodAutoscalerSpecResourcePolicy(
                    container_policies=[
                        VerticalPodAutoscalerSpecResourcePolicyContainerPolicies(
                            container_name=container,
                            controlled_resources=list(resources),
                            controlled_values="RequestsOnly",
                            min_allowed={
                                "memory": VerticalPodAutoscalerSpecResourcePolicyContainerPoliciesMinAllowed.from_string(
                                    memory_floor
                                )
                            }
                            if memory_floor is not None
                            else None,
                            max_allowed={
                                "memory": VerticalPodAutoscalerSpecResourcePolicyContainerPoliciesMaxAllowed.from_string(
                                    memory_ceiling
                                )
                            }
                            if memory_ceiling is not None
                            else None,
                        ),
                        # Future sidecars do not silently acquire a resource policy.
                        VerticalPodAutoscalerSpecResourcePolicyContainerPolicies(container_name="*", mode="Off"),
                    ]
                ),
            ),
        )
    return chart
