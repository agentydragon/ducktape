"""Trusted baseline resources for the inner Agentplane test cluster."""

from __future__ import annotations

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s

from agentplane.system_tests.shared_cluster.namespaces import PREFIX
from cluster.cdk8s.agentplane_test_cluster.vm_admission import add_vm_admission

NAME = "agentplane-test-cluster-inner-security"

_POLICY_NAME = f"{NAME}-tenant-namespace-fence"
_TENANT_SERVICE_ACCOUNT_PREFIX = f"system:serviceaccount:{PREFIX}"
_TENANT_SERVICE_ACCOUNT = f"request.userInfo.username.startsWith('{_TENANT_SERVICE_ACCOUNT_PREFIX}')"
_TENANT_SERVICE_ACCOUNT_FORMAT = (
    "request.userInfo.username.matches('^system:serviceaccount:"
    f"{PREFIX}[0-9a-f]{{32}}(-credentials|-monitoring)?:[^:]+$')"
)
_TENANT_ACTOR_NAMESPACE = "request.userInfo.username.split(':')[2]"
_TENANT_RUN_NAMESPACE = f"{_TENANT_ACTOR_NAMESPACE}.substring(0, {len(PREFIX) + 32})"
_TENANT_NAMESPACE = (
    f"{_TENANT_SERVICE_ACCOUNT_FORMAT} && has(request.namespace) && "
    f"request.namespace in [{_TENANT_RUN_NAMESPACE}, "
    f"{_TENANT_RUN_NAMESPACE} + '-credentials', {_TENANT_RUN_NAMESPACE} + '-monitoring'] && "
    "!(request.resource.group == '' && request.resource.resource == 'namespaces')"
)
_DENIAL_MESSAGE = "Tenant ServiceAccounts may only write namespaced resources in their tenant namespaces."


def _tenant_namespace_fence(chart: Chart) -> None:
    k8s.KubeValidatingAdmissionPolicy(
        chart,
        "tenant-namespace-fence",
        metadata=k8s.ObjectMeta(
            name=_POLICY_NAME,
            annotations={
                "description": (
                    "Restricts per-run tenant ServiceAccounts to namespaced writes in their three allocated namespaces."
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


def _namespace_cleanup_rbac(chart: Chart) -> None:
    name = "agentplane-test-namespace-cleanup"
    k8s.KubeClusterRole(
        chart,
        "namespace-cleanup-role",
        metadata=k8s.ObjectMeta(name=name),
        rules=[k8s.PolicyRule(api_groups=[""], resources=["namespaces"], verbs=["get", "list", "watch", "delete"])],
    )
    k8s.KubeClusterRoleBinding(
        chart,
        "namespace-cleanup-binding",
        metadata=k8s.ObjectMeta(name=name),
        role_ref=k8s.RoleRef(kind="ClusterRole", name=name, api_group="rbac.authorization.k8s.io"),
        subjects=[k8s.Subject(kind="ServiceAccount", name="kyverno-cleanup-controller", namespace="kyverno")],
    )


def chart(app: App) -> Chart:
    """Build inner-cluster admission policies, independently of outer resources."""
    result = Chart(app, NAME, disable_resource_name_hashes=True)
    _tenant_namespace_fence(result)
    _namespace_cleanup_rbac(result)
    add_vm_admission(result)
    return result
