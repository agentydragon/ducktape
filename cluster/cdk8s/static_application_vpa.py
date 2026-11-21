"""VPA policies for selected static Deployments in Goldilocks-disabled namespaces."""

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

NAME = "static-application-vpa"
OUTPUT_DIR = f"{GENERATED_ROOT}/{NAME}"

# Only named Deployments are in scope. The namespace opt-outs also cover dynamic workloads.
_WORKLOADS = (
    ("agentplane-staging", "central-sampler", "central-sampler", "Off"),
    ("agentplane-staging", "egress", "proxy", "Initial"),
    ("claude-session-sync", "claude-session-sync", "claude-session-sync", "Initial"),
    ("claude-session-sync", "control", "claude-session-sync-control", "Initial"),
    ("haku-ci", "haku-ci-egress", "egress-sidecar", "Initial"),
)


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    for namespace, deployment, container, update_mode in _WORKLOADS:
        VerticalPodAutoscaler(
            chart,
            deployment,
            metadata=ApiObjectMetadata(name=deployment, namespace=namespace),
            spec=VerticalPodAutoscalerSpec(
                target_ref=VerticalPodAutoscalerSpecTargetRef(
                    api_version="apps/v1", kind="Deployment", name=deployment
                ),
                update_policy=VerticalPodAutoscalerSpecUpdatePolicy(update_mode=update_mode),
                resource_policy=VerticalPodAutoscalerSpecResourcePolicy(
                    container_policies=[
                        VerticalPodAutoscalerSpecResourcePolicyContainerPolicies(
                            container_name=container,
                            controlled_resources=["cpu", "memory"],
                            controlled_values="RequestsOnly",
                        ),
                        VerticalPodAutoscalerSpecResourcePolicyContainerPolicies(container_name="*", mode="Off"),
                    ]
                ),
            ),
        )
    return chart
