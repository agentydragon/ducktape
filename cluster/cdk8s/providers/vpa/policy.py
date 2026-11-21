"""Typed policies for explicitly owned VerticalPodAutoscalers."""

from vpa_crds.io.k8s.autoscaling import (
    VerticalPodAutoscalerSpec,
    VerticalPodAutoscalerSpecResourcePolicy,
    VerticalPodAutoscalerSpecResourcePolicyContainerPolicies,
    VerticalPodAutoscalerSpecTargetRef,
    VerticalPodAutoscalerSpecUpdatePolicy,
)


def initial_requests_spec(
    target_ref: VerticalPodAutoscalerSpecTargetRef, *, container_name: str
) -> VerticalPodAutoscalerSpec:
    """Size a single container's CPU and memory requests at admission, leaving other containers alone."""
    return VerticalPodAutoscalerSpec(
        target_ref=target_ref,
        update_policy=VerticalPodAutoscalerSpecUpdatePolicy(update_mode="Initial"),
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
