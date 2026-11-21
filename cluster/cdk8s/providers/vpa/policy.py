"""Typed policies for explicitly owned VerticalPodAutoscalers."""

from vpa_crds.io.k8s.autoscaling import (
    VerticalPodAutoscalerSpec,
    VerticalPodAutoscalerSpecResourcePolicy,
    VerticalPodAutoscalerSpecResourcePolicyContainerPolicies,
    VerticalPodAutoscalerSpecTargetRef,
    VerticalPodAutoscalerSpecUpdatePolicy,
)


def requests_only_spec(
    target_ref: VerticalPodAutoscalerSpecTargetRef, *, container_name: str, update_mode: str = "Initial"
) -> VerticalPodAutoscalerSpec:
    """Size one container's CPU and memory requests, with admission-only updates by default."""
    return VerticalPodAutoscalerSpec(
        target_ref=target_ref,
        update_policy=VerticalPodAutoscalerSpecUpdatePolicy(update_mode=update_mode),
        resource_policy=VerticalPodAutoscalerSpecResourcePolicy(
            container_policies=[
                VerticalPodAutoscalerSpecResourcePolicyContainerPolicies(
                    container_name=container_name,
                    controlled_resources=["cpu", "memory"],
                    controlled_values="RequestsOnly",
                ),
                VerticalPodAutoscalerSpecResourcePolicyContainerPolicies(container_name="*", mode="Off"),
            ]
        ),
    )
