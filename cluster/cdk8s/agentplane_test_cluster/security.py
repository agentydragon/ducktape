"""Trusted baseline resources for the inner Agentplane test cluster."""

from __future__ import annotations

import json

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s

from cluster.cdk8s.agentplane_test_cluster.vm_admission import add_vm_admission

NAME = "agentplane-test-cluster-inner-security"
TENANT_NAMESPACES = ("systest", "systest-egress-credentials", "monitoring")

_POLICY_NAME = f"{NAME}-tenant-namespace-fence"
_TENANT_SERVICE_ACCOUNT = " || ".join(
    f"request.userInfo.username.startsWith('system:serviceaccount:{namespace}:')" for namespace in TENANT_NAMESPACES
)
_TENANT_NAMESPACE = (
    f"has(request.namespace) && request.namespace in {json.dumps(TENANT_NAMESPACES)}"
    " && !(request.resource.group == '' && request.resource.resource == 'namespaces')"
)
_DENIAL_MESSAGE = "Tenant ServiceAccounts may only write namespaced resources in their tenant namespaces."


def _namespace(chart: Chart, name: str) -> None:
    # These labels and the PSA policy are owned by the inner cluster administrator. The VAP below
    # denies namespace writes from tenant ServiceAccounts, including attempts to change these labels.
    labels = {
        "app.kubernetes.io/managed-by": "agentplane-test-cluster",
        "app.kubernetes.io/part-of": "agentplane-test-cluster",
        "pod-security.kubernetes.io/enforce": "baseline",
        "pod-security.kubernetes.io/enforce-version": "v1.34",
        "pod-security.kubernetes.io/audit": "baseline",
        "pod-security.kubernetes.io/audit-version": "v1.34",
        "pod-security.kubernetes.io/warn": "baseline",
        "pod-security.kubernetes.io/warn-version": "v1.34",
    }
    k8s.KubeNamespace(chart, f"namespace-{name}", metadata=k8s.ObjectMeta(name=name, labels=labels))


def _tenant_namespace_fence(chart: Chart) -> None:
    k8s.KubeValidatingAdmissionPolicy(
        chart,
        "tenant-namespace-fence",
        metadata=k8s.ObjectMeta(
            name=_POLICY_NAME,
            annotations={
                "description": (
                    "Restricts tenant ServiceAccounts to namespaced writes in the fixed inner test namespaces."
                )
            },
        ),
        spec=k8s.ValidatingAdmissionPolicySpec(
            failure_policy="Fail",
            match_constraints=k8s.MatchResources(
                match_policy="Equivalent",
                resource_rules=[
                    k8s.NamedRuleWithOperations(
                        api_groups=["*"],
                        api_versions=["*"],
                        operations=["CREATE", "UPDATE", "DELETE", "CONNECT"],
                        resources=["*/*"],
                        scope="*",
                    )
                ],
                exclude_resource_rules=[
                    k8s.NamedRuleWithOperations(
                        api_groups=["authentication.k8s.io"],
                        api_versions=["v1"],
                        operations=["CREATE"],
                        resources=["tokenreviews", "selfsubjectreviews"],
                        scope="*",
                    ),
                    k8s.NamedRuleWithOperations(
                        api_groups=["authorization.k8s.io"],
                        api_versions=["v1"],
                        operations=["CREATE"],
                        resources=[
                            "subjectaccessreviews",
                            "localsubjectaccessreviews",
                            "selfsubjectaccessreviews",
                            "selfsubjectrulesreviews",
                        ],
                        scope="*",
                    ),
                ],
            ),
            match_conditions=[k8s.MatchCondition(name="tenant-service-account", expression=_TENANT_SERVICE_ACCOUNT)],
            validations=[k8s.Validation(expression=_TENANT_NAMESPACE, message=_DENIAL_MESSAGE)],
        ),
    )
    # Keep this binding cluster-wide so CEL can reject cluster-scoped resources. Namespace writes
    # get a separate resource check because request.namespace may contain the Namespace name.
    k8s.KubeValidatingAdmissionPolicyBinding(
        chart,
        "tenant-namespace-fence-binding",
        metadata=k8s.ObjectMeta(name=_POLICY_NAME),
        spec=k8s.ValidatingAdmissionPolicyBindingSpec(policy_name=_POLICY_NAME, validation_actions=["Deny"]),
    )


def chart(app: App) -> Chart:
    """Build inner-cluster namespaces and admission policy, independently of outer resources."""
    result = Chart(app, NAME, disable_resource_name_hashes=True)
    for namespace in TENANT_NAMESPACES:
        _namespace(result, namespace)
    _tenant_namespace_fence(result)
    add_vm_admission(result, namespaces=TENANT_NAMESPACES)
    return result
