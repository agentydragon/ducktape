"""Reusable cdk8s constructs for the Agentplane staging/testing environments'
Namespace and operator RBAC -- see cluster/k8s/agentplane-{staging,testing}/README.md
for what the rest of each environment (db, egress, llm-ingress, actions, app) does.

cdk8s_plus_33 has no typed ResourceQuota/LimitRange at all, so those stay raw
ApiObjects with their /spec patched in. Namespace, Role, and RoleBinding are typed
constructs; only the Role's rules need the same patch escape hatch ha_mcp_constructs.py
uses for its own Role, since cdk8s_plus_33's RolePolicyRule has no resourceNames field.
"""

from __future__ import annotations

from dataclasses import dataclass

from cdk8s import ApiObject, ApiObjectMetadata, JsonPatch
from cdk8s_plus_33 import Group, Namespace, Role, RoleBinding, ServiceAccount
from constructs import Construct

from cluster.cdk8s.metadata import metadata

_SANDBOX_RULES = [
    {"apiGroups": ["extensions.agents.x-k8s.io"], "resources": ["sandboxtemplates"], "verbs": ["get"]},
    {
        "apiGroups": ["agents.x-k8s.io"],
        "resources": ["sandboxes"],
        "verbs": ["create", "get", "list", "watch", "patch", "delete"],
    },
    {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch"]},
    {"apiGroups": [""], "resources": ["pods/exec", "pods/portforward"], "verbs": ["create"]},
    {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
]

# The credential the agent presents to the app's own API: a token scoped to the
# app's audience, which TokenReview resolves to
# system:serviceaccount:<namespace>:agentplane-agent. The app accepts that subject
# because its Deployment names it; the audience is no gate on its own, since a token
# minted for any other account would carry it just as well. Minting it is not
# assuming that account -- it holds no RoleBinding, so the token is an identity for
# the app and nothing else in the cluster.
_TOKEN_RULE = {
    "apiGroups": [""],
    "resources": ["serviceaccounts/token"],
    "resourceNames": ["agentplane-agent"],
    "verbs": ["create"],
}

# testing's MCP acceptance scenario additionally creates, expires, and deletes the
# ActionPolicySet/ActionPolicyBinding its Sandbox is auto-approved under, reading
# their Ready condition to know the Action Service has seen each edit. Inserted right
# after the sandbox-lifecycle rule to match the hand-written original's ordering
# (immaterial to RBAC evaluation, but keeps the generated diff readable).
_ACTION_POLICY_RULE = {
    "apiGroups": ["agentplane.allegedly.works"],
    "resources": ["actionpolicysets", "actionpolicybindings"],
    "verbs": ["create", "get", "patch", "delete"],
}


@dataclass(frozen=True)
class AgentplaneEnvSpec:
    """Per-environment values for the namespace + operator RBAC."""

    namespace: str
    description: str
    include_action_policy_rule: bool = False


class AgentplaneNamespace(Construct):
    """Namespace, ResourceQuota, and LimitRange bounding what Sandbox runner Pods
    and the integration app may consume.
    """

    def __init__(self, scope: Construct, id: str, spec: AgentplaneEnvSpec) -> None:
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
        ApiObject(
            self,
            "resourcequota",
            api_version="v1",
            kind="ResourceQuota",
            metadata=metadata(f"{spec.namespace}-quota", spec.namespace),
        ).add_json_patch(
            JsonPatch.add(
                "/spec",
                {
                    "hard": {
                        "requests.cpu": "4",
                        "requests.memory": "8Gi",
                        "limits.cpu": "12",
                        "limits.memory": "24Gi",
                        "requests.storage": "80Gi",
                    }
                },
            )
        )
        ApiObject(
            self,
            "limitrange",
            api_version="v1",
            kind="LimitRange",
            metadata=metadata(f"{spec.namespace}-limits", spec.namespace),
        ).add_json_patch(
            JsonPatch.add(
                "/spec",
                {
                    "limits": [
                        {
                            "type": "Container",
                            "max": {"cpu": "2", "memory": "4Gi"},
                            "min": {"cpu": "10m", "memory": "16Mi"},
                            "default": {"cpu": "500m", "memory": "512Mi"},
                            "defaultRequest": {"cpu": "100m", "memory": "128Mi"},
                        },
                        {"type": "Pod", "max": {"cpu": "4", "memory": "8Gi"}},
                    ]
                },
            )
        )


class AgentplaneAgentRbac(Construct):
    """The operator Role/RoleBinding an agent needs to drive Agentplane without a
    human: Sandbox lifecycle, exec/port-forward into runner Pods, and the token used
    to call the app's own API.
    """

    def __init__(self, scope: Construct, id: str, spec: AgentplaneEnvSpec) -> None:
        super().__init__(scope, id)
        rules = list(_SANDBOX_RULES[:2])
        if spec.include_action_policy_rule:
            rules.append(_ACTION_POLICY_RULE)
        rules.extend(_SANDBOX_RULES[2:])
        rules.append(_TOKEN_RULE)

        role = Role(self, "role", metadata=metadata("agentplane-operator", spec.namespace))
        ApiObject.of(role).add_json_patch(JsonPatch.add("/rules", rules))

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
