"""Reusable cdk8s constructs for the Agentplane staging/testing environments'
Namespace and operator RBAC -- see cluster/k8s/agentplane-{staging,testing}/README.md
for what the rest of each environment (db, egress, llm-ingress, actions, app) does.

cdk8s_plus_34's hand-written fluent layer has no ResourceQuota/LimitRange builder, but
its `k8s` submodule -- the same schema-generated layer `cdk8s_import` produces for
CRDs, pre-generated here for every core Kubernetes kind -- has fully typed
`KubeResourceQuota`/`KubeLimitRange` classes with real `ResourceQuotaSpec`/
`LimitRangeSpec`/`LimitRangeItem` structs. Used directly below instead of a raw
`ApiObject` + `JsonPatch`; see AGENTS.md.

Namespace, Role, and RoleBinding are fully typed constructs, including every rule:
`Role(rules=[RolePolicyRule(resources=[...], verbs=[...])])` takes real
`IApiResource` objects, not raw dicts. A resourceNames-scoped rule needs an
`IApiResource` whose `resourceName` property is set -- `ApiResource.custom()` never
sets one, and no built-in cdk8s-plus type covers the `serviceaccounts/token`
subresource, so `_NamedApiResource` below implements the (public, documented)
`IApiResource` interface directly for that one case, the same extension point
`Secret.from_secret_name()` and friends use internally for their own
resourceName-scoped references (see AGENTS.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import jsii
from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import (
    ApiResource,
    Group,
    IApiResource,
    Namespace,
    Role,
    RoleBinding,
    RolePolicyRule,
    ServiceAccount,
    k8s,
)
from constructs import Construct

from cluster.cdk8s.metadata import metadata


@jsii.implements(IApiResource)
class _NamedApiResource:
    """An `IApiResource` naming one specific object, for a rule's `resourceNames` --
    the manual form of what `Secret.from_secret_name()` and its siblings generate
    for their own typed resource kinds, needed here because no cdk8s-plus type
    covers the `serviceaccounts/token` subresource.
    """

    def __init__(self, *, api_group: str, resource_type: str, resource_name: str) -> None:
        self._api_group = api_group
        self._resource_type = resource_type
        self._resource_name = resource_name

    @property
    def api_group(self) -> str:
        return self._api_group

    @property
    def resource_type(self) -> str:
        return self._resource_type

    @property
    def resource_name(self) -> str | None:
        return self._resource_name


# cdk8s_plus_34's Python stub doesn't declare ApiResource as implementing
# IApiResource's `resource_name` member (TS's `@jsii.implements(IApiResource, ...)`
# on the class doesn't reach the generated .pyi), even though every ApiResource
# instance satisfies the interface at runtime (resource_name is always None,
# verified via the actual generated rules above). Cast at the one boundary that
# needs it rather than widening every call site's inferred type.
def _custom(api_group: str, resource_type: str) -> IApiResource:
    return cast(IApiResource, ApiResource.custom(api_group=api_group, resource_type=resource_type))


_SANDBOX_RULES = [
    RolePolicyRule(resources=[_custom("extensions.agents.x-k8s.io", "sandboxtemplates")], verbs=["get"]),
    RolePolicyRule(
        resources=[_custom("agents.x-k8s.io", "sandboxes")], verbs=["create", "get", "list", "watch", "patch", "delete"]
    ),
    RolePolicyRule(resources=[cast(IApiResource, ApiResource.PODS)], verbs=["get", "list", "watch"]),
    RolePolicyRule(resources=[_custom("", "pods/exec"), _custom("", "pods/portforward")], verbs=["create"]),
    RolePolicyRule(resources=[_custom("", "pods/log")], verbs=["get"]),
]

# The credential the agent presents to the app's own API: a token scoped to the
# app's audience, which TokenReview resolves to
# system:serviceaccount:<namespace>:agentplane-agent. The app accepts that subject
# because its Deployment names it; the audience is no gate on its own, since a token
# minted for any other account would carry it just as well. Minting it is not
# assuming that account -- it holds no RoleBinding, so the token is an identity for
# the app and nothing else in the cluster.
_TOKEN_RULE = RolePolicyRule(
    resources=[
        _NamedApiResource(api_group="", resource_type="serviceaccounts/token", resource_name="agentplane-agent")
    ],
    verbs=["create"],
)

# testing's MCP acceptance scenario additionally creates, expires, and deletes the
# ActionPolicySet/ActionPolicyBinding its Sandbox is auto-approved under, reading
# their Ready condition to know the Action Service has seen each edit. Inserted right
# after the sandbox-lifecycle rule to match the hand-written original's ordering
# (immaterial to RBAC evaluation, but keeps the generated diff readable).
_ACTION_POLICY_RULE = RolePolicyRule(
    resources=[
        _custom("agentplane.allegedly.works", "actionpolicysets"),
        _custom("agentplane.allegedly.works", "actionpolicybindings"),
    ],
    verbs=["create", "get", "patch", "delete"],
)


@dataclass(frozen=True)
class EnvSpec:
    """Per-environment values for the namespace + operator RBAC."""

    namespace: str
    description: str
    include_action_policy_rule: bool = False


class NamespaceQuota(Construct):
    """Namespace, ResourceQuota, and LimitRange bounding what Sandbox runner Pods
    and the integration app may consume.
    """

    def __init__(self, scope: Construct, id: str, spec: EnvSpec) -> None:
        super().__init__(scope, id)
        Namespace(
            self,
            "namespace",
            metadata=ApiObjectMetadata(
                name=spec.namespace,
                labels={
                    "name": spec.namespace,
                    # Runner Pods are Sandbox-owned, not Deployments; nothing here is VPA-managed.
                    "goldilocks.fairwinds.com/enabled": "false",
                    # Standing agent access to metadata and logs (Kyverno-generated bindings);
                    # write access lives in the operator Role below.
                    "rbac.ducktape.io/agent-readable-logs": "true",
                },
                annotations={"description": spec.description},
            ),
        )
        # Bounds what runner sandboxes take from the node: the app stamps a Sandbox
        # per user request, each costing 2500m of limits.cpu (2 for the runner, 500m
        # the LimitRange default for the egress sidecar) and a 10Gi state PVC.
        # limits.cpu binds first, at roughly four concurrent sandboxes -- raise that,
        # not a count, for more headroom.
        #
        # Aggregate resources only. A cap per object kind bounds an untrusted creator,
        # and only Flux and the integration app create objects here.
        #
        # Load-bearing pair with the LimitRange below: it supplies the requests and
        # limits that several containers omit (the CNPG postgres container declares
        # none), and LimitRanger mutates before quota validates. Narrowing it while
        # these compute dimensions stand rejects those pods outright.
        k8s.KubeResourceQuota(
            self,
            "resourcequota",
            metadata=k8s.ObjectMeta(name=f"{spec.namespace}-quota", namespace=spec.namespace),
            spec=k8s.ResourceQuotaSpec(
                hard={
                    "requests.cpu": k8s.Quantity.from_string("4"),
                    "requests.memory": k8s.Quantity.from_string("8Gi"),
                    "limits.cpu": k8s.Quantity.from_string("12"),
                    "limits.memory": k8s.Quantity.from_string("24Gi"),
                    "requests.storage": k8s.Quantity.from_string("80Gi"),
                }
            ),
        )
        k8s.KubeLimitRange(
            self,
            "limitrange",
            metadata=k8s.ObjectMeta(name=f"{spec.namespace}-limits", namespace=spec.namespace),
            spec=k8s.LimitRangeSpec(
                limits=[
                    k8s.LimitRangeItem(
                        type="Container",
                        max={"cpu": k8s.Quantity.from_string("2"), "memory": k8s.Quantity.from_string("4Gi")},
                        min={"cpu": k8s.Quantity.from_string("10m"), "memory": k8s.Quantity.from_string("16Mi")},
                        default={"cpu": k8s.Quantity.from_string("500m"), "memory": k8s.Quantity.from_string("512Mi")},
                        default_request={
                            "cpu": k8s.Quantity.from_string("100m"),
                            "memory": k8s.Quantity.from_string("128Mi"),
                        },
                    ),
                    k8s.LimitRangeItem(
                        type="Pod",
                        max={"cpu": k8s.Quantity.from_string("4"), "memory": k8s.Quantity.from_string("8Gi")},
                    ),
                ]
            ),
        )


class AgentRbac(Construct):
    """The operator Role/RoleBinding an agent needs to drive Agentplane without a
    human: Sandbox lifecycle, exec/port-forward into runner Pods, and the token used
    to call the app's own API.
    """

    def __init__(self, scope: Construct, id: str, spec: EnvSpec) -> None:
        super().__init__(scope, id)
        rules = list(_SANDBOX_RULES[:2])
        if spec.include_action_policy_rule:
            rules.append(_ACTION_POLICY_RULE)
        rules.extend(_SANDBOX_RULES[2:])
        rules.append(_TOKEN_RULE)

        Role(self, "role", metadata=metadata("agentplane-operator", spec.namespace), rules=rules)

        RoleBinding(
            self,
            "rolebinding",
            metadata=metadata("agent-agentplane-operator", spec.namespace),
            role=Role.from_role_name(self, "role-ref", "agentplane-operator"),
        ).add_subjects(
            # Haku and public-coder agent identities plus the interactive
            # kubectl-sandbox group. public-coder is listed explicitly because this
            # Role grants beyond the read-only diagnostics bindings Kyverno generates
            # from the namespace label.
            Group.from_name(self, "haku-oidc-group", "oidc-ksbx-groups:haku"),
            Group.from_name(self, "haku-access-profile-group", "haku:access-profile:haku"),
            Group.from_name(self, "public-coder-access-profile-group", "haku:access-profile:public-coder"),
            ServiceAccount.from_service_account_name(self, "haku-sandbox-sa", "haku", namespace_name="haku-sandbox"),
            Group.from_name(self, "kubectl-sandbox-users-group", "oidc-ksbx-groups:kubectl-sandbox-users"),
        )
