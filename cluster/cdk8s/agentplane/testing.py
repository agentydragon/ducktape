"""agentplane-testing: one replica of everything, its own Dex for operator login, and
credentialless MCP fixtures in place of the real action groups.
"""

from __future__ import annotations

from cdk8s import App, Chart
from cdk8s_plus_34 import DeploymentStrategy
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    Kustomization,
    KustomizationSpec,
    KustomizationSpecDeletionPolicy,
    KustomizationSpecHealthCheckExprs,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s import cilium
from cluster.cdk8s.agentplane import actions, dex, testing_config
from cluster.cdk8s.agentplane.actions_testing_fixtures import (
    MCP_EVERYTHING_NAME,
    MCP_EVERYTHING_PORT,
    OAUTH_FIXTURE_NAME,
    OAUTH_FIXTURE_PORT,
    add_testing_fixtures,
)
from cluster.cdk8s.agentplane.chart import environment_chart
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
from cluster.cdk8s.flux import NAMESPACE as FLUX_NAMESPACE, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.generation import CNPG_DATABASE_READY, sops_decryption

_NAMESPACE = "agentplane-testing"
_HOSTNAME = "agentplane-testing.allegedly.works"
_DEX_HOSTNAME = "agentplane-dex-testing.allegedly.works"
_DEX_ISSUER = f"https://{_DEX_HOSTNAME}/dex"
_LITELLM_KEY_SECRET = "litellm-key-cheap-experiments"
# The ESO ExternalSecret replicating the Terraform-owned key into this namespace,
# a sibling resource in the same Kustomization -- see litellm/credentials.py.
_LITELLM_CREDENTIALS_DIR = "litellm-credentials/"
_OAUTH_FIXTURE_MCP_URL = f"http://{OAUTH_FIXTURE_NAME}.{_NAMESPACE}.svc.cluster.local:{OAUTH_FIXTURE_PORT}/mcp"

_FEDERATION_TARGET = {
    "issuer": _DEX_ISSUER,
    "audience": "agentplane-testing",
    "jwks_uri": f"{_DEX_ISSUER}/keys",
    "token_profile": "dex",
}
_ACTION_FEDERATION = {
    "mode": "direct",
    "service_url": f"http://agentplane-actions.{_NAMESPACE}.svc.cluster.local:{actions.CONTAINER_PORT}",
    "login_jwks_uri": f"{_DEX_ISSUER}/keys",
    "login_token_profile": "dex",
    "target": _FEDERATION_TARGET,
    "scope": "openid",
}
_ACTIONS_SETTINGS = {
    "operator_oidc": _FEDERATION_TARGET,
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
    extra_resources=(_LITELLM_CREDENTIALS_DIR,),
    include_action_policy_rule=True,
    replicas=ReplicaProfile(count=1, strategy=DeploymentStrategy.recreate(), min_ready=None, pdb_min_available=None),
    app_config={**testing_config.config(), "action_federation": _ACTION_FEDERATION},
    db=DbProps(instances=1, pod_anti_affinity=False),
    llm_ingress=LlmIngressProps(litellm_key_secret_name=_LITELLM_KEY_SECRET),
    egress=EgressProps(ca_secret_name="agentplane-testing-egress-ca", credentials_namespace=None),
    app=AppProps(hostname=_HOSTNAME, oidc_issuer=_DEX_ISSUER, reach_incluster_authentik=False, runner_zone=None),
    actions=ActionsProps(
        hostname="agentplane-actions-testing.allegedly.works",
        settings=_ACTIONS_SETTINGS,
        extra_egress=[
            # The direct federation verifier fetches Dex's JWKS over the public-origin Gateway path.
            cilium.egress_via_gateway(_DEX_HOSTNAME),
            # MCP OAuth discovery/token exchange/tool calls for the linked "example" fixture:
            # cluster-internal only, unlike the real GitHub/Kubernetes MCP OAuth providers
            # linked in staging.
            cilium.egress_to(cilium.endpoint_labels(_NAMESPACE, OAUTH_FIXTURE_NAME), OAUTH_FIXTURE_PORT),
        ],
    ),
)


def chart(app: App) -> Chart:
    chart = environment_chart(app, ENV)
    add_testing_fixtures(chart)
    dex.Dex(chart, "dex")
    return chart


def agentplane_testing(
    flux_chart: Chart,
    health_checks: list[KustomizationSpecHealthChecks],
    agentplane_crds: Kustomization,
    agent_sandbox_controller: Kustomization,
    cert_manager_environment: Kustomization,
    cert_manager_trust: Kustomization,
    claude_rbac: Kustomization,
    cnpg: Kustomization,
    external_creds: Kustomization,
    external_secrets_config: Kustomization,
    forgejo_images: Kustomization,
    gateway: Kustomization,
    litellm_keys_tf: Kustomization,
    local_path_provisioner: Kustomization,
    reflector: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        flux_chart,
        ENV.namespace,
        description=ENV.flux_description,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="10m",
            path=f"./cluster/k8s/{ENV.namespace}",
            prune=True,
            # This one Kustomization owns the CNPG Cluster's PVCs; pruning on
            # deletion would take the database with them.
            deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
            health_checks=health_checks,
            health_check_exprs=[
                KustomizationSpecHealthCheckExprs(
                    api_version="postgresql.cnpg.io/v1", kind="Database", current=CNPG_DATABASE_READY
                )
            ],
            decryption=sops_decryption(ENV.extra_resources),
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=ENV.namespace, namespace=FLUX_NAMESPACE
            ),
            depends_on=flux_kustomization_depends_on_many(
                agentplane_crds,
                agent_sandbox_controller,
                cert_manager_environment,
                cert_manager_trust,
                claude_rbac,
                cnpg,
                external_creds,
                external_secrets_config,
                forgejo_images,
                gateway,
                litellm_keys_tf,
                local_path_provisioner,
                reflector,
            ),
        ),
    )
