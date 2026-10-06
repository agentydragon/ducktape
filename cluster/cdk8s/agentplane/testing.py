"""agentplane-testing: one replica of everything, its own Dex for operator login, and
credentialless MCP fixtures in place of the real action groups.

One Flux Kustomization (`agentplane_testing`) applies the whole environment: the
generated file, holding this chart and `litellm/credentials.py`'s, and the hand-written
`image_pins` Component its root Kustomization includes across the roots.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import DeploymentStrategy
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpecDeletionPolicy,
    KustomizationSpecHealthCheckExprs,
    KustomizationSpecHealthChecks,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from agentplane.action_service.catalog import ActionGroup, McpExecutorBinding
from agentplane.action_service.main import ActionServiceDeploymentSettings
from agentplane.action_service.mcp_linkage import McpOAuthServer
from agentplane.action_service.operator_oidc import OperatorOidcSettings, OperatorTokenProfile
from agentplane.app.action_federation import DirectFederationSettings
from cluster.cdk8s import cilium
from cluster.cdk8s.agentplane import actions, app as app_component, dex, egress, rbac, testing_config
from cluster.cdk8s.agentplane.actions_testing_fixtures import (
    MCP_EVERYTHING_NAME,
    MCP_EVERYTHING_PORT,
    OAUTH_FIXTURE_NAME,
    OAUTH_FIXTURE_PORT,
    add_testing_fixtures,
)
from cluster.cdk8s.agentplane.chart import environment_chart
from cluster.cdk8s.agentplane.egress_credentials import TESTING_NAMESPACE, EgressCredentials
from cluster.cdk8s.agentplane.egress_testing_credentials import add_testing_egress_credentials
from cluster.cdk8s.agentplane.environment import (
    ActionsProps,
    AppProps,
    DbProps,
    EgressProps,
    Environment,
    LlmIngressProps,
    ReplicaProfile,
)
from cluster.cdk8s.agentplane.grpc_channel_config import LARGE_EVENT_GRPC_CHANNEL_OPTIONS
from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.generation import CNPG_DATABASE_READY
from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT
from cluster.cdk8s.model_selections import TESTING_APP_MODELS
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, NetworkPolicy

_NAMESPACE = "agentplane-testing"
_HOSTNAME = "agentplane-testing.allegedly.works"
_DEX_HOSTNAME = "agentplane-dex-testing.allegedly.works"
_DEX_ISSUER = f"https://{_DEX_HOSTNAME}/dex"
# The Terraform-owned key, replicated into this namespace by `litellm/credentials.py`'s
# ExternalSecret.
_LITELLM_KEY_SECRET = "litellm-key-cheap-experiments"
_OAUTH_FIXTURE_MCP_URL = f"http://{OAUTH_FIXTURE_NAME}.{_NAMESPACE}.svc.cluster.local:{OAUTH_FIXTURE_PORT}/mcp"

_FEDERATION_TARGET = OperatorOidcSettings(
    issuer=_DEX_ISSUER,
    audience="agentplane-testing",
    jwks_uri=f"{_DEX_ISSUER}/keys",
    token_profile=OperatorTokenProfile.DEX,
)
_ACTION_FEDERATION = DirectFederationSettings(
    mode="direct",
    service_url=actions.service(_NAMESPACE).url,
    login_jwks_uri=f"{_DEX_ISSUER}/keys",
    login_token_profile=OperatorTokenProfile.DEX,
    target=_FEDERATION_TARGET,
    scope="openid",
)
_ACTIONS_SETTINGS = ActionServiceDeploymentSettings(
    operator_oidc=_FEDERATION_TARGET,
    allowed_service_account_namespaces=frozenset({_NAMESPACE}),
    direct_wait_seconds=30,
    max_wait_seconds=180,
    mcp_servers={
        "example": McpOAuthServer(
            server_id="example",
            server_url=_OAUTH_FIXTURE_MCP_URL,
            client_id="agentplane-testing-mcp",
            client_secret_file=Path("/etc/agentplane-mcp/client-secret"),
            redirect_uri=f"https://{_HOSTNAME}/mcp-linkage/callback",
            scopes=["openid"],
        )
    },
    action_groups={
        "everything": ActionGroup(
            title="Upstream Everything",
            description="Credentialless MCP reference server for testing acceptance.",
            executor=McpExecutorBinding(
                kind="mcp",
                description="Community-built Everything image; no user account, workload token, or mounted credentials.",
                config={
                    "transport": "streamable-http",
                    "url": f"http://{MCP_EVERYTHING_NAME}.{_NAMESPACE}.svc.cluster.local:{MCP_EVERYTHING_PORT}/mcp",
                    "auth": "none",
                },
            ),
        ),
        "example": ActionGroup(
            title="OAuth Example",
            description="Dex-backed OAuth-linked MCP fixture for testing acceptance of the linkage flow.",
            executor=McpExecutorBinding(
                kind="mcp",
                description="MCP tool protected by Dex-issued JWTs; no real credentials.",
                config={
                    "transport": "streamable-http",
                    "url": _OAUTH_FIXTURE_MCP_URL,
                    "server_id": "example",
                    "auth": "oauth",
                },
            ),
        ),
    },
)


ENV = Environment(
    namespace=_NAMESPACE,
    description=(
        "Agentplane testing - sandboxed runner Pods (one per Sandbox) and the integration app that drives them."
    ),
    flux_description=(
        "Complete Agentplane testing environment, including namespace, database, Dex, egress, LLM ingress, "
        "Actions fixtures, app, runner template, and operator RBAC."
    ),
    output_dir=f"{GENERATED_ROOT}/{_NAMESPACE}",
    image_pins=f"{HAND_WRITTEN_ROOT}/agentplane-testing-image-pins",
    extra_resources=(),
    replicas=ReplicaProfile(count=1, strategy=DeploymentStrategy.recreate(), min_ready=None, pdb_min_available=None),
    model_routes=TESTING_APP_MODELS,
    app_config=testing_config.config(
        action_federation=_ACTION_FEDERATION, sandbox_service_grpc_channel_options=LARGE_EVENT_GRPC_CHANNEL_OPTIONS
    ),
    runner_grpc_channel_options=LARGE_EVENT_GRPC_CHANNEL_OPTIONS,
    db=DbProps(instances=1),
    llm_ingress=LlmIngressProps(litellm_key_secret_name=_LITELLM_KEY_SECRET),
    egress=EgressProps(ca_secret_name="agentplane-testing-egress-ca", credentials_namespace=TESTING_NAMESPACE),
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
            EgressRule.to_endpoints(cilium.endpoint_labels(_NAMESPACE, OAUTH_FIXTURE_NAME), OAUTH_FIXTURE_PORT),
        ],
    ),
)


def chart(app: App) -> Chart:
    chart = environment_chart(app, ENV)
    # Only this environment's chart gets the agent-operator Role/RoleBinding -- see
    # `rbac.AgentRbac`'s own docstring for why it must not be in staging's.
    rbac.AgentRbac(chart, "rbac", ENV)
    rbac.AcceptanceToken(chart, "acceptance-token", ENV)
    # claude-ai's boxes reach this app through staging's egress proxy, by its Service rather than its
    # public name, which would hairpin out through the Gateway and back.
    app_service = app_component.service(ENV.namespace)
    NetworkPolicy(
        chart,
        "networkpolicy-app-from-staging-egress",
        metadata=ApiObjectMetadata(name=f"{app_service.name}-from-staging-egress", namespace=ENV.namespace),
        endpoint_selector=app_service.pods.selector,
        ingress=[egress.proxy("agentplane-staging").pods.admit(app_service.pod_port)],
    )
    add_testing_fixtures(chart)
    dex.Dex(chart, "dex")
    EgressCredentials(
        chart, "egress-credentials", namespace=ENV.egress.credentials_namespace, proxy_namespace=ENV.namespace
    )
    add_testing_egress_credentials(chart, credentials_namespace=ENV.egress.credentials_namespace)
    return chart


def agentplane_testing(
    flux_chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    health_checks: list[KustomizationSpecHealthChecks],
    agentplane_crds: Kustomization,
    agent_sandbox_controller: Kustomization,
    cert_manager_trust: Kustomization,
    cnpg: Kustomization,
    external_secrets_operator: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        flux_chart,
        ENV.namespace,
        artifact,
        description=ENV.flux_description,
        wait=None,
        timeout="10m",
        # This one Kustomization owns the CNPG Cluster's PVCs; pruning on
        # deletion would take the database with them.
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        health_checks=health_checks,
        health_check_exprs=[
            KustomizationSpecHealthCheckExprs(
                api_version="postgresql.cnpg.io/v1", kind="Database", current=CNPG_DATABASE_READY
            )
        ],
        depends_on=flux_kustomization_depends_on_many(
            agentplane_crds, agent_sandbox_controller, cert_manager_trust, cnpg, external_secrets_operator
        ),
    )
