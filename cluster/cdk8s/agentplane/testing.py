"""agentplane-testing: one replica of everything, its own Dex for operator login, and
credentialless MCP fixtures in place of the real action groups.
"""

from __future__ import annotations

from cdk8s import Chart
from cdk8s_plus_34 import DeploymentStrategy

from cluster.cdk8s.agentplane import actions_constructs, cilium_helpers, dex_constructs, testing_config
from cluster.cdk8s.agentplane.actions_testing_fixtures import (
    MCP_EVERYTHING_NAME,
    MCP_EVERYTHING_PORT,
    OAUTH_FIXTURE_NAME,
    OAUTH_FIXTURE_PORT,
    add_testing_fixtures,
)
from cluster.cdk8s.agentplane.environment import (
    DEPENDS_ON,
    ActionsProps,
    AppProps,
    DbProps,
    EgressProps,
    Environment,
    LlmIngressProps,
    ReplicaProfile,
)

_NAMESPACE = "agentplane-testing"
_HOSTNAME = "agentplane-testing.allegedly.works"
_DEX_HOSTNAME = "agentplane-dex-testing.allegedly.works"
_DEX_ISSUER = f"https://{_DEX_HOSTNAME}/dex"
_LITELLM_KEY_SECRET = "litellm-key-cheap-experiments"
_OAUTH_FIXTURE_MCP_URL = f"http://{OAUTH_FIXTURE_NAME}.{_NAMESPACE}.svc.cluster.local:{OAUTH_FIXTURE_PORT}/mcp"

_ACTIONS_SETTINGS = {
    "allowed_service_account_namespaces": [_NAMESPACE],
    "mcp_servers": {
        "example": {
            "server_id": "example",
            "provider": "example",
            "server_url": _OAUTH_FIXTURE_MCP_URL,
            "client_id": "agentplane-testing-mcp",
            "client_secret_file": "/etc/agentplane-mcp/client-secret",
            "redirect_uri": f"https://{_HOSTNAME}/mcp-linkage/callback",
            "scopes": ["openid"],
        }
    },
    "action_groups": {
        "everything": {
            "title": "Upstream Everything",
            "description": "Credentialless MCP reference server for testing acceptance.",
            "executor": {
                "kind": "mcp",
                "description": "Community-built Everything image; no user account, workload token, or mounted credentials.",
                "config": {
                    "transport": "streamable-http",
                    "url": f"http://{MCP_EVERYTHING_NAME}.{_NAMESPACE}.svc.cluster.local:{MCP_EVERYTHING_PORT}/mcp",
                    "auth": "none",
                },
            },
        },
        "example": {
            "title": "OAuth Example",
            "description": "Dex-backed OAuth-linked MCP fixture for testing acceptance of the linkage flow.",
            "executor": {
                "kind": "mcp",
                "description": "MCP tool protected by Dex-issued JWTs; no real credentials.",
                "config": {
                    "transport": "streamable-http",
                    "url": _OAUTH_FIXTURE_MCP_URL,
                    "server_id": "example",
                    "auth": "oauth",
                },
            },
        },
    },
}
_FEDERATION_TARGET = {
    "issuer": _DEX_ISSUER,
    "audience": "agentplane-testing",
    "jwks_uri": f"{_DEX_ISSUER}/keys",
    "token_profile": "dex",
}


def _extra(chart: Chart) -> None:
    add_testing_fixtures(chart)
    dex_constructs.Dex(chart, "dex")


ENV = Environment(
    namespace=_NAMESPACE,
    description=(
        "Agentplane testing - sandboxed runner Pods (one per Sandbox) and the integration app that drives them."
    ),
    flux_description=(
        "Complete Agentplane testing environment, including namespace, database, Dex, egress, LLM ingress, "
        "Actions fixtures, app, runner template, and operator RBAC."
    ),
    depends_on=DEPENDS_ON,
    extra_resources=(),
    provided_secrets={_LITELLM_KEY_SECRET: "litellm-keys-tf"},
    include_action_policy_rule=True,
    replicas=ReplicaProfile(
        count=1, strategy=DeploymentStrategy.recreate(), topology_spread=False, min_ready=None, pdb_min_available=None
    ),
    app_config=testing_config.config(),
    db=DbProps(instances=1, pod_anti_affinity=False),
    llm_ingress=LlmIngressProps(litellm_key_secret_name=_LITELLM_KEY_SECRET),
    egress=EgressProps(ca_secret_name="agentplane-testing-egress-ca"),
    app=AppProps(hostname=_HOSTNAME, oidc_issuer=_DEX_ISSUER, reach_incluster_authentik=False, runner_zone=None),
    actions=ActionsProps(
        hostname="agentplane-actions-testing.allegedly.works",
        settings=_ACTIONS_SETTINGS,
        action_federation={
            "mode": "direct",
            "service_url": f"http://agentplane-actions.{_NAMESPACE}.svc.cluster.local:{actions_constructs.CONTAINER_PORT}",
            "login_jwks_uri": f"{_DEX_ISSUER}/keys",
            "login_token_profile": "dex",
            "target": _FEDERATION_TARGET,
            "scope": "openid",
        },
        action_federation_description="Direct Dex operator federation pins for the isolated testing Action Service.",
        operator_oidc=_FEDERATION_TARGET,
        extra_egress=[
            # The direct federation verifier fetches Dex's JWKS over the public-origin Gateway path.
            cilium_helpers.egress_via_gateway(_DEX_HOSTNAME),
            # MCP OAuth discovery/token exchange/tool calls for the linked "example" fixture:
            # cluster-internal only, unlike the real GitHub/Kubernetes MCP OAuth providers
            # linked in staging.
            cilium_helpers.egress_to(
                cilium_helpers.endpoint_labels(_NAMESPACE, OAUTH_FIXTURE_NAME), OAUTH_FIXTURE_PORT
            ),
        ],
    ),
    extra=_extra,
)
