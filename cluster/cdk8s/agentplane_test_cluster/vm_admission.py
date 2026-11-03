"""Bound tenant VM specifications in the inner test cluster.

This policy is scoped to the fixed tenant namespaces. It does not protect operator or other
infrastructure namespaces; the inner cluster's RBAC must keep tenant identities out of those namespaces.
It also complements rather than replaces KubeVirt's validating webhook.
"""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s_plus_34 import k8s
from constructs import Construct

_NAME = "agentplane-test-cluster-tenant-vm-fence"
_DENIAL_MESSAGE = "Tenant VM specifications may not use host devices, guest service accounts, or KubeVirt hooks."
_HOOK_PREFIX = "hooks.kubevirt.io/"


def _tenant_selector(namespaces: Sequence[str]) -> k8s.LabelSelector:
    return k8s.LabelSelector(
        match_expressions=[
            k8s.LabelSelectorRequirement(key="kubernetes.io/metadata.name", operator="In", values=list(namespaces))
        ]
    )


def _annotations_are_safe(path: str) -> str:
    return f"(!has({path}) || {path}.all(key, !key.startsWith('{_HOOK_PREFIX}')))"


def _spec_is_safe(spec: str) -> str:
    volumes = f"{spec}.volumes"
    devices = f"{spec}.domain.devices"
    return " && ".join(
        (
            f"(!has({volumes}) || !{volumes}.exists(volume, has(volume.hostDisk) || has(volume.serviceAccount)))",
            f"(!has({devices}.hostDevices) || size({devices}.hostDevices) == 0)",
            f"(!has({devices}.gpus) || size({devices}.gpus) == 0)",
            f"!has({devices}.clientPassthrough)",
        )
    )


def _validating_policy(
    scope: Construct, *, resource: str, spec: str, template: bool, namespaces: Sequence[str]
) -> None:
    name = f"{_NAME}-{resource}"
    safe = [_annotations_are_safe("object.metadata.annotations"), _spec_is_safe(spec)]
    if template:
        safe.append(_annotations_are_safe("object.spec.template.metadata.annotations"))

    k8s.KubeValidatingAdmissionPolicy(
        scope,
        f"{resource}-policy",
        metadata=k8s.ObjectMeta(name=name),
        spec=k8s.ValidatingAdmissionPolicySpec(
            failure_policy="Fail",
            match_constraints=k8s.MatchResources(
                match_policy="Equivalent",
                resource_rules=[
                    k8s.NamedRuleWithOperations(
                        api_groups=["kubevirt.io"],
                        api_versions=["v1"],
                        operations=["CREATE", "UPDATE"],
                        resources=[resource],
                        scope="Namespaced",
                    )
                ],
            ),
            validations=[k8s.Validation(expression=" && ".join(safe), message=_DENIAL_MESSAGE)],
        ),
    )
    k8s.KubeValidatingAdmissionPolicyBinding(
        scope,
        f"{resource}-binding",
        metadata=k8s.ObjectMeta(name=name),
        spec=k8s.ValidatingAdmissionPolicyBindingSpec(
            policy_name=name,
            validation_actions=["Deny"],
            match_resources=k8s.MatchResources(namespace_selector=_tenant_selector(namespaces)),
        ),
    )


def add_vm_admission(scope: Construct, *, namespaces: Sequence[str]) -> None:
    """Deny tenant VM/VMI specs that can expose host devices, guest tokens, or KubeVirt hooks.

    Separate policies keep each expression type-checked against the pinned KubeVirt schema for
    its resource. `disableHotplug` remains available: it disables disk hotplug and does not grant
    access to host resources.
    """
    if not namespaces:
        raise ValueError("VM admission requires at least one tenant namespace")
    _validating_policy(
        scope, resource="virtualmachines", spec="object.spec.template.spec", template=True, namespaces=namespaces
    )
    _validating_policy(
        scope, resource="virtualmachineinstances", spec="object.spec", template=False, namespaces=namespaces
    )
