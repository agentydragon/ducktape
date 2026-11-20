"""Native audit for workload writes that bypass the known GitOps paths."""

from __future__ import annotations

from cdk8s_plus_34 import k8s
from constructs import Construct

# These identities reconcile GitOps objects or write their controller-owned children.
# Kyverno and the native audit share the list while both signals are live.
EXEMPT_SERVICE_ACCOUNTS = (
    ("kustomize-controller", "flux-system"),
    ("helm-controller", "flux-system"),
    ("source-controller", "flux-system"),
    ("haku-state-reconciler", "flux-system"),
    ("kyverno-admission-controller", "kyverno"),
    ("monitoring-operator", "monitoring"),
    ("grafana-operator", "monitoring"),
    ("seaweedfs-operator", "seaweedfs"),
    ("authentik", "authentik"),
    ("reloader-reloader", "kube-system"),
)
EXEMPT_NAMESPACES = ("kube-system", "kyverno", "flux-system")
CROSSPLANE_SERVICE_ACCOUNT = "crossplane"
CROSSPLANE_NAMESPACE = "crossplane-system"


def add_gitops_write_audit(scope: Construct) -> None:
    name = "audit-direct-workload-writes"
    operators = ", ".join(f"'system:serviceaccount:{ns}:{account}'" for account, ns in EXEMPT_SERVICE_ACCOUNTS)
    # Include the extra exclusions from Kyverno's live resourceFilters.
    namespaces = ", ".join(f"'{ns}'" for ns in (*EXEMPT_NAMESPACES, "kube-public", "kube-node-lease"))
    crossplane = f"system:serviceaccount:{CROSSPLANE_NAMESPACE}:{CROSSPLANE_SERVICE_ACCOUNT}"

    k8s.KubeValidatingAdmissionPolicy(
        scope,
        "gitops-audit",
        metadata=k8s.ObjectMeta(name=name),
        spec=k8s.ValidatingAdmissionPolicySpec(
            failure_policy="Ignore",
            match_constraints=k8s.MatchResources(
                resource_rules=[
                    k8s.NamedRuleWithOperations(
                        api_groups=["apps"],
                        api_versions=["v1"],
                        operations=["CREATE", "UPDATE"],
                        resources=["daemonsets", "deployments", "statefulsets"],
                        scope="Namespaced",
                    )
                ]
            ),
            match_conditions=[
                k8s.MatchCondition(name="application-namespace", expression=f"!(request.namespace in [{namespaces}])"),
                k8s.MatchCondition(
                    name="direct-writer",
                    expression=(
                        f"!(request.userInfo.username in [{operators}])"
                        f" && !(request.namespace == '{CROSSPLANE_NAMESPACE}'"
                        f" && request.userInfo.username == '{crossplane}')"
                        " && !('system:nodes' in request.userInfo.?groups.orValue([]))"
                    ),
                ),
            ],
            validations=[
                k8s.Validation(
                    expression="false", message="Workload write outside the known GitOps and operator paths."
                )
            ],
        ),
    )
    k8s.KubeValidatingAdmissionPolicyBinding(
        scope,
        "gitops-audit-binding",
        metadata=k8s.ObjectMeta(name=name),
        spec=k8s.ValidatingAdmissionPolicyBindingSpec(policy_name=name, validation_actions=["Audit"]),
    )
