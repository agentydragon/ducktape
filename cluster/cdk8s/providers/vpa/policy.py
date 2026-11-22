"""Typed policies for explicitly owned VerticalPodAutoscalers."""

from cdk8s import ApiObjectMetadata
from constructs import Construct
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


def requests_only_vpa(
    scope: Construct,
    name: str,
    *,
    namespace: str,
    container_name: str,
    update_mode: str,
    target_kind: str = "Deployment",
    min_memory: str | None = None,
    max_memory: str | None = None,
) -> VerticalPodAutoscaler:
    """Manage one container's CPU and memory requests and leave sidecars alone."""
    return VerticalPodAutoscaler(
        scope,
        name,
        metadata=ApiObjectMetadata(name=name, namespace=namespace),
        spec=VerticalPodAutoscalerSpec(
            target_ref=VerticalPodAutoscalerSpecTargetRef(api_version="apps/v1", kind=target_kind, name=name),
            update_policy=VerticalPodAutoscalerSpecUpdatePolicy(update_mode=update_mode),
            resource_policy=VerticalPodAutoscalerSpecResourcePolicy(
                container_policies=[
                    VerticalPodAutoscalerSpecResourcePolicyContainerPolicies(
                        container_name=container_name,
                        controlled_resources=["cpu", "memory"],
                        controlled_values="RequestsOnly",
                        min_allowed={
                            "memory": VerticalPodAutoscalerSpecResourcePolicyContainerPoliciesMinAllowed.from_string(
                                min_memory
                            )
                        }
                        if min_memory is not None
                        else None,
                        max_allowed={
                            "memory": VerticalPodAutoscalerSpecResourcePolicyContainerPoliciesMaxAllowed.from_string(
                                max_memory
                            )
                        }
                        if max_memory is not None
                        else None,
                    ),
                    VerticalPodAutoscalerSpecResourcePolicyContainerPolicies(container_name="*", mode="Off"),
                ]
            ),
        ),
    )
