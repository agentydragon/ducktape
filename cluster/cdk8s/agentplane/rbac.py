"""The shared Agentplane Namespace and operator RBAC.

A resourceNames-scoped rule needs an `IApiResource` whose `resourceName` is set:
`ApiResource.custom()` never sets one and no cdk8s-plus type covers the
`serviceaccounts/token` subresource, so `_NamedApiResource` implements the interface for
that one case (see AGENTS.md).
"""

from __future__ import annotations

from typing import cast

from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import ApiResource, IApiResource, Role, RoleBinding, RolePolicyRule, ServiceAccount
from constructs import Construct

from cluster.cdk8s import agent_access_profiles as access, namespaces
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.api_resource import custom_resource, named_resource
from cluster.cdk8s.namespaces import Vpa

TESTING_OPERATOR_ROLE_NAME = "agentplane-testing-operator"

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
    resources=[named_resource("", "serviceaccounts/token", "agentplane-agent")], verbs=["create"]
)

# testing's MCP acceptance scenario additionally creates, expires, and deletes the
# ActionPolicySet/ActionPolicyBinding its Sandbox is auto-approved under, reading
# their Ready condition to know the Action Service has seen each edit.
_ACTION_POLICY_RULE = RolePolicyRule(
    resources=[
        custom_resource("agentplane.allegedly.works", "actionpolicysets"),
        custom_resource("agentplane.allegedly.works", "actionpolicybindings"),
    ],
    verbs=["create", "get", "patch", "delete"],
)


class Namespace(Construct):
    """The namespace shared by an Agentplane environment."""

    def __init__(self, scope: Construct, id: str, env: Environment) -> None:
        super().__init__(scope, id)
        namespaces.namespace(
            self,
            "namespace",
            name=env.namespace,
            # Runner Pods are Sandbox-owned, not Deployments; nothing here is VPA-managed.
            vpa=Vpa.DISABLED,
            # Standing agent access to metadata and logs (Kyverno-generated bindings);
            # write access lives in the operator Role below.
            labels={"name": env.namespace},
            annotations={"description": env.description},
        )


class AgentRbac(Construct):
    """The operator Role/RoleBinding an agent needs to drive Agentplane **testing**
    without a human: Sandbox lifecycle, exec/port-forward into runner Pods, and the
    token used to call the app's own API.

    **Testing only, deliberately.** `agentplane-testing` runs Dex-backed fake OAuth and
    credentialless MCP fixtures -- nothing here reaches a real account. `agentplane-staging`
    is the opposite: real Authentik-federated operator login, real GitHub/Kubernetes MCP
    OAuth linkage, and `claude-ai` Sandboxes carry the real read-only Google
    `google-readonly` egress credential (`egress_staging_credentials.py`). An agent identity holding this
    Role there could stamp a Sandbox under that ServiceAccount and reach the operator's
    real external accounts with no human in the loop -- the opposite of what "testing"
    fixtures are for. So only `testing.chart` instantiates this construct; `staging.chart`
    (via the shared `chart.environment_chart`) must not.
    """

    def __init__(self, scope: Construct, id: str, env: Environment) -> None:
        super().__init__(scope, id)
        Role(
            self,
            "role",
            metadata=ApiObjectMetadata(name=TESTING_OPERATOR_ROLE_NAME, namespace=env.namespace),
            rules=[*_SANDBOX_RULES, _ACTION_POLICY_RULE, _TOKEN_RULE],
        )

        RoleBinding(
            self,
            "rolebinding",
            metadata=ApiObjectMetadata(name="agent-agentplane-testing-operator", namespace=env.namespace),
            role=Role.from_role_name(self, "role-ref", TESTING_OPERATOR_ROLE_NAME),
        ).add_subjects(
            *[
                subject.imported(self, f"operator-subject-{index}")
                for index, subject in enumerate(access.TESTING_OPERATOR_SUBJECTS)
            ]
        )


class AcceptanceToken(Construct):
    """Lets `agentplane-staging`'s `claude-ai` and `haku-agent` mint this namespace's app token,
    so their sandboxes can run the acceptance suite's harness scenarios
    (`agentplane/acceptance/README.md`), which ask the API server for nothing else. None of
    `AgentRbac`'s Sandbox lifecycle, exec or ActionPolicy writes are granted by this Role:
    the token is an identity for the app, as `_TOKEN_RULE` says. `claude-ai` separately
    receives `AgentRbac` in testing; `haku-agent` does not.
    """

    def __init__(self, scope: Construct, id: str, env: Environment) -> None:
        super().__init__(scope, id)
        role = Role(
            self,
            "role",
            metadata=ApiObjectMetadata(name="agentplane-acceptance-token", namespace=env.namespace),
            rules=[_TOKEN_RULE],
        )
        RoleBinding(
            self,
            "rolebinding",
            metadata=ApiObjectMetadata(name="claude-ai-acceptance-token", namespace=env.namespace),
            role=role,
        ).add_subjects(
            ServiceAccount.from_service_account_name(
                self, "claude-ai-sa", "claude-ai", namespace_name="agentplane-staging"
            ),
            ServiceAccount.from_service_account_name(
                self, "haku-agent-sa", "haku-agent", namespace_name="agentplane-staging"
            ),
        )
