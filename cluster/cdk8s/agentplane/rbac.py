"""The Namespace with its ResourceQuota/LimitRange, and the operator Role/RoleBinding.

A resourceNames-scoped rule needs an `IApiResource` whose `resourceName` is set:
`ApiResource.custom()` never sets one and no cdk8s-plus type covers the
`serviceaccounts/token` subresource, so `_NamedApiResource` implements the interface for
that one case (see AGENTS.md).
"""

from __future__ import annotations

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

from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.api_resource import custom_resource
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


_SANDBOX_RULES = [
    RolePolicyRule(resources=[custom_resource("extensions.agents.x-k8s.io", "sandboxtemplates")], verbs=["get"]),
    RolePolicyRule(
        resources=[custom_resource("agents.x-k8s.io", "sandboxes")],
        verbs=["create", "get", "list", "watch", "patch", "delete"],
    ),
    RolePolicyRule(resources=[cast(IApiResource, ApiResource.PODS)], verbs=["get", "list", "watch"]),
    RolePolicyRule(
        resources=[custom_resource("", "pods/exec"), custom_resource("", "pods/portforward")], verbs=["create"]
    ),
    RolePolicyRule(resources=[custom_resource("", "pods/log")], verbs=["get"]),
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
# their Ready condition to know the Action Service has seen each edit. Rule order is
# immaterial to RBAC evaluation.
_ACTION_POLICY_RULE = RolePolicyRule(
    resources=[
        custom_resource("agentplane.allegedly.works", "actionpolicysets"),
        custom_resource("agentplane.allegedly.works", "actionpolicybindings"),
    ],
    verbs=["create", "get", "patch", "delete"],
)


class NamespaceQuota(Construct):
    """Namespace, ResourceQuota, and LimitRange bounding what Sandbox runner Pods
    and the integration app may consume.
    """

    def __init__(self, scope: Construct, id: str, env: Environment) -> None:
        super().__init__(scope, id)
        Namespace(
            self,
            "namespace",
            metadata=ApiObjectMetadata(
                name=env.namespace,
                labels={
                    "name": env.namespace,
                    # Runner Pods are Sandbox-owned, not Deployments; nothing here is VPA-managed.
                    "goldilocks.fairwinds.com/enabled": "false",
                    # Standing agent access to metadata and logs (Kyverno-generated bindings);
                    # write access lives in the operator Role below.
                    "rbac.ducktape.io/agent-readable-logs": "true",
                },
                annotations={"description": env.description},
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
            metadata=k8s.ObjectMeta(name=f"{env.namespace}-quota", namespace=env.namespace),
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
            metadata=k8s.ObjectMeta(name=f"{env.namespace}-limits", namespace=env.namespace),
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
    """The operator Role/RoleBinding an agent needs to drive Agentplane **testing**
    without a human: Sandbox lifecycle, exec/port-forward into runner Pods, and the
    token used to call the app's own API.

    **Testing only, deliberately.** `agentplane-testing` runs Dex-backed fake OAuth and
    credentialless MCP fixtures -- nothing here reaches a real account. `agentplane-staging`
    is the opposite: real Authentik-federated operator login, real GitHub/Kubernetes MCP
    OAuth linkage, and `claude-ai` Sandboxes carry the real read-only Google
    `google-readonly` egress credential (`egress.py`). An agent identity holding this
    Role there could stamp a Sandbox under that ServiceAccount and reach the operator's
    real external accounts with no human in the loop -- the opposite of what "testing"
    fixtures are for. So only `testing.chart` instantiates this construct; `staging.chart`
    (via the shared `chart.environment_chart`) must not.
    """

    def __init__(self, scope: Construct, id: str, env: Environment) -> None:
        super().__init__(scope, id)
        rules = list(_SANDBOX_RULES)
        if env.include_action_policy_rule:
            rules.append(_ACTION_POLICY_RULE)
        rules.append(_TOKEN_RULE)

        Role(self, "role", metadata=metadata("agentplane-testing-operator", env.namespace), rules=rules)

        RoleBinding(
            self,
            "rolebinding",
            metadata=metadata("agent-agentplane-testing-operator", env.namespace),
            role=Role.from_role_name(self, "role-ref", "agentplane-testing-operator"),
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
