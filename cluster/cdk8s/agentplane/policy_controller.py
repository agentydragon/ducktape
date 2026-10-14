"""The Policy Controller (agentplane/policy_controller): it deletes expired PolicyBindings and keeps
the native RoleBindings and ClusterRoleBindings their Kubernetes rules expand to. Its `bind`
authority covers exactly the roles the Flux-owned PolicySets rendered here reference; RoleBinding
writes outside the environment's own namespace come from binding_delegation.py.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import cast

from agentplane_policyset_crds.works.allegedly.agentplane import PolicySetSpecRules, PolicySetSpecRulesRoleKind
from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import (
    ApiResource,
    ContainerResources,
    Cpu,
    CpuResources,
    IApiResource,
    MemoryResources,
    Role,
    RoleBinding,
    RolePolicyRule,
    k8s,
)
from constructs import Construct
from prometheus_operator_crds.com.coreos.monitoring import ServiceMonitorSpecSelector

from agentplane.sandboxes.kubernetes_grants import KubernetesGrant, RoleBindingGrant
from cluster.cdk8s import cilium
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.service_workload import add_service_workload
from cluster.cdk8s.api_resource import custom_resource, named_resource
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.providers.agentplane import policy_set
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, NetworkPolicy
from cluster.cdk8s.providers.prometheus_operator.prometheus_rule import PrometheusRule, Rule, group
from cluster.cdk8s.providers.prometheus_operator.service_monitor import Endpoint, ServiceMonitor
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

NAME = "agentplane-policy-controller"
_LABELS = {"app.kubernetes.io/name": NAME}
_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-policy-controller"
_RBAC = "rbac.authorization.k8s.io"
_GROUP = "agentplane.allegedly.works"
_HEALTH_PORT = 8081
# How long a PolicyBinding may outlive its expiresAt before it alerts: the controller deletes it
# within seconds, so this is the controller being down or its deletes failing.
_OVERDUE_SECONDS = 300
_ROLE_KINDS = {"Role": PolicySetSpecRulesRoleKind.ROLE, "ClusterRole": PolicySetSpecRulesRoleKind.CLUSTER_ROLE}


def metrics(namespace: str) -> ServiceRef:
    return ServiceRef(
        name=NAME, port=Port(name="metrics", number=8080), pods=Pods(namespace=namespace, labels=tuple(_LABELS.items()))
    )


def _rules(grants: Sequence[KubernetesGrant]) -> list[PolicySetSpecRules]:
    """One rule per role: a set's RoleBinding grants of one role merge into one rule over all their
    namespaces."""
    namespaces: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
    cluster_roles: set[str] = set()
    for grant in grants:
        if isinstance(grant, RoleBindingGrant):
            namespaces[(grant.role_ref.kind, grant.role_ref.name)].add(grant.namespace)
        else:
            cluster_roles.add(grant.role_ref.name)
    return [
        *(
            policy_set.Rule.kubernetes_role_binding(
                kind=_ROLE_KINDS[kind], name=name, namespaces=sorted(namespaces[(kind, name)])
            )
            for kind, name in sorted(namespaces)
        ),
        *(policy_set.Rule.kubernetes_cluster_role_binding(cluster_role=name) for name in sorted(cluster_roles)),
    ]


class PolicyController(Construct):
    """The controller's Deployment, RBAC and network policy, the PolicySets `sets` names (each a
    set of catalog grants), and the alert on an overdue expiry."""

    def __init__(
        self, scope: Construct, id: str, env: Environment, *, sets: Mapping[str, Sequence[KubernetesGrant]]
    ) -> None:
        super().__init__(scope, id)
        namespace = env.namespace
        for name, grants in sorted(sets.items()):
            policy_set.PolicySet(
                self, f"set-{name}", metadata=ApiObjectMetadata(name=name, namespace=namespace), rules=_rules(grants)
            )
        endpoint = metrics(namespace)
        workload = add_service_workload(
            self,
            env,
            serves=[endpoint],
            container="policy-controller",
            image=f"{_IMAGE}:unset",
            args=[f"--policy-namespace={namespace}", f"--subject-namespaces={namespace}"],
            ports=[endpoint.port.container_port(), Port(name="health", number=_HEALTH_PORT).container_port()],
            readiness=http_probe("/readyz", port=_HEALTH_PORT, initial_delay_seconds=3),
            liveness=http_probe("/healthz", port=_HEALTH_PORT, initial_delay_seconds=10, period_seconds=30),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(10)),
                memory=MemoryResources(request=Size.mebibytes(64), limit=Size.mebibytes(256)),
            ),
            termination_grace=Duration.seconds(30),
        )
        all_grants = [grant for grants in sets.values() for grant in grants]
        own_roles = sorted(
            {
                grant.role_ref.name
                for grant in all_grants
                if isinstance(grant, RoleBindingGrant)
                and grant.role_ref.kind == "Role"
                and grant.namespace == namespace
            }
        )
        cluster_roles = sorted({grant.role_ref.name for grant in all_grants if grant.role_ref.kind == "ClusterRole"})
        Role(
            self,
            "role",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
            rules=[
                RolePolicyRule(resources=[custom_resource(_GROUP, "policysets")], verbs=["get", "list", "watch"]),
                # Deleting is how a binding expires.
                RolePolicyRule(
                    resources=[custom_resource(_GROUP, "policybindings")], verbs=["get", "list", "watch", "delete"]
                ),
                RolePolicyRule(resources=[custom_resource(_GROUP, "policybindings/status")], verbs=["patch"]),
                RolePolicyRule(
                    resources=[cast(IApiResource, ApiResource.SERVICE_ACCOUNTS)], verbs=["get", "list", "watch"]
                ),
                # Leader election.
                RolePolicyRule(
                    resources=[custom_resource("coordination.k8s.io", "leases")], verbs=["get", "create", "update"]
                ),
                RolePolicyRule(resources=[custom_resource("", "events")], verbs=["create", "patch"]),
                # RoleBindings in its own namespace; binding_delegation.py grants the same elsewhere.
                RolePolicyRule(resources=[custom_resource(_RBAC, "rolebindings")], verbs=["create", "delete"]),
                *(
                    RolePolicyRule(resources=[named_resource(_RBAC, "roles", name)], verbs=["bind"])
                    for name in own_roles
                ),
            ],
        )
        RoleBinding(
            self,
            "role-binding",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
            role=Role.from_role_name(self, "role-ref", NAME),
        ).add_subjects(workload.account)
        cluster_role_name = f"{namespace}-policy-controller"
        k8s.KubeClusterRole(
            self,
            "cluster-role",
            metadata=k8s.ObjectMeta(name=cluster_role_name),
            rules=[
                # Its labeled native bindings, and role metadata to retry a binding whose role is missing.
                k8s.PolicyRule(
                    api_groups=[_RBAC],
                    resources=["rolebindings", "clusterrolebindings", "roles", "clusterroles"],
                    verbs=["get", "list", "watch"],
                ),
                k8s.PolicyRule(api_groups=[_RBAC], resources=["clusterrolebindings"], verbs=["create", "delete"]),
                *(
                    [
                        k8s.PolicyRule(
                            api_groups=[_RBAC], resources=["clusterroles"], resource_names=cluster_roles, verbs=["bind"]
                        )
                    ]
                    if cluster_roles
                    else []
                ),
            ],
        )
        k8s.KubeClusterRoleBinding(
            self,
            "cluster-role-binding",
            metadata=k8s.ObjectMeta(name=cluster_role_name),
            role_ref=k8s.RoleRef(api_group=_RBAC, kind="ClusterRole", name=cluster_role_name),
            subjects=[k8s.Subject(kind="ServiceAccount", name=NAME, namespace=namespace)],
        )
        NetworkPolicy(
            self,
            "network-policy",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
            endpoint_selector=endpoint.pods.selector,
            ingress=[cilium.SCRAPERS.admit(endpoint.pod_port)],
            egress=[cilium.dns_egress(), EgressRule.to_entities(Entity.KUBE_APISERVER)],
        )
        ServiceMonitor(
            self,
            "service-monitor",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
            selector=ServiceMonitorSpecSelector(match_labels=endpoint.pods.selector),
            endpoints=[Endpoint.plain(port=endpoint.port.name)],
        )
        # kube-state-metrics reports every PolicyBinding's expiresAt (monitoring/stack.py), so this
        # fires even while the controller is down.
        PrometheusRule(
            self,
            "prometheus-rule",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
            groups=[
                group(
                    NAME,
                    [
                        Rule.alert(
                            "AgentplanePolicyBindingExpiryOverdue",
                            f'time() - kube_customresource_agentplane_policybinding_expires_at{{namespace="{namespace}"}}'
                            f" > {_OVERDUE_SECONDS}",
                            labels={"severity": "warning"},
                            summary="PolicyBinding {{ $labels.name }} still grants after its expiresAt",
                            description=(
                                "The Policy Controller deletes a PolicyBinding at its expiresAt; this one has "
                                "outlived it, so its grants are still live. Check the agentplane-policy-controller "
                                "Deployment and its logs."
                            ),
                        )
                    ],
                )
            ],
        )
