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

# (Deployment, container, memory bounds, optional CPU bounds).
# CPU floors preserve the Deployment reservations. CPU ceilings leave margin above
# measured demand while bounding the extra reservation on OVH workers.
_WORKLOADS = (
    ("actions", "actions", ("128Mi", "160Mi"), ("50m", "75m")),
    ("app", "app", ("128Mi", "160Mi"), ("50m", "350m")),
    ("electric", "electric", ("448Mi", "576Mi"), None),
    ("environments", "environments", ("64Mi", "112Mi"), ("10m", "50m")),
    ("frontend", "frontend", ("32Mi", "40Mi"), None),
    ("history-service", "history-service", ("128Mi", "256Mi"), ("20m", "1025m")),
    ("llm-ingress", "ingress", ("128Mi", "160Mi"), ("50m", "75m")),
    ("mcp-edge", "mcp-edge", ("128Mi", "160Mi"), ("50m", "75m")),
    ("notifications", "notifications", ("128Mi", "160Mi"), ("50m", "600m")),
    ("policy-controller", "policy-controller", ("64Mi", "112Mi"), ("10m", "25m")),
    ("sessions", "sessions", ("256Mi", "320Mi"), ("100m", "150m")),
)


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    for deployment, container, memory, cpu in _WORKLOADS:
        bounds = {"memory": memory}
        if cpu is not None:
            bounds["cpu"] = cpu
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
                            controlled_resources=sorted(bounds),
                            controlled_values="RequestsOnly",
                            min_allowed={
                                resource: VerticalPodAutoscalerSpecResourcePolicyContainerPoliciesMinAllowed.from_string(
                                    floor
                                )
                                for resource, (floor, _) in bounds.items()
                            },
                            max_allowed={
                                resource: VerticalPodAutoscalerSpecResourcePolicyContainerPoliciesMaxAllowed.from_string(
                                    ceiling
                                )
                                for resource, (_, ceiling) in bounds.items()
                            },
                        ),
                        # Future sidecars do not silently acquire a resource policy.
                        VerticalPodAutoscalerSpecResourcePolicyContainerPolicies(container_name="*", mode="Off"),
                    ]
                ),
            ),
        )
    return chart
