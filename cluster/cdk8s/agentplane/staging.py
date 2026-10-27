"""agentplane-staging: replicated services, operator login federated through the
shared Authentik, and the reviewed GitHub/Kubernetes/Grocy SF/SSH/Home Assistant/Tana/Gmail/
Google Calendar MCP action groups.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart, Duration
from cdk8s_plus_34 import DeploymentStrategy, PercentOrAbsolute, ServiceAccount
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpecDeletionPolicy,
    KustomizationSpecHealthCheckExprs,
    KustomizationSpecHealthChecks,
)
from pydantic import AnyHttpUrl
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from agentplane.action_service.catalog import ActionGroup, McpExecutorBinding
from agentplane.action_service.mcp_settings import McpClientMetadataSettings, McpOAuthServer
from agentplane.action_service.operator_oidc_settings import OperatorOidcSettings
from agentplane.action_service.sandbox.actions import SandboxAction
from agentplane.action_service.sandbox.binding import PodExecutorBinding
from agentplane.app.action_federation_settings import ExchangeFederationSettings
from agentplane.llm_ingress.configpb import config_pb2
from agentplane.runner.configuration import GuestResources, ResourceLimits
from agentplane.sandboxes.kubernetes_grants import KubernetesGrant
from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s import (
    agent_access_profiles,
    cilium,
    cnpg,
    external_creds,
    ha_mcp,
    kubectl_passthrough_mcp,
    node_scheduling,
)
from cluster.cdk8s.agentplane import (
    actions,
    app as app_component,
    command_sandbox,
    mcp_edge,
    notifications,
    policy_controller,
    policy_controller_rbac,
    staging_config,
    staging_grants,
)
from cluster.cdk8s.agentplane.actions_staging_policies import add_staging_action_policies, coinbase_egress_rules
from cluster.cdk8s.agentplane.chart import environment_chart
from cluster.cdk8s.agentplane.database import DbProps
from cluster.cdk8s.agentplane.egress_credentials import (
    EXTERNAL_CREDS_STORE,
    STAGING_CREDENTIALS_NAMESPACE,
    EgressCredentials,
    credential_external_secret,
)
from cluster.cdk8s.agentplane.egress_staging_credentials import PLAID_PGWEB_CREDENTIAL, add_staging_egress_credentials
from cluster.cdk8s.agentplane.environment import (
    ActionsProps,
    AppProps,
    BearerMcpMount,
    EgressProps,
    Environment,
    GitHubAppProps,
    LlmIngressProps,
    McpEdgeProps,
    OrchestrationProps,
    ReplicaProfile,
    WebPushProps,
)
from cluster.cdk8s.agentplane.generation import ManifestLayout
from cluster.cdk8s.agentplane.namespaces import STAGING_NAMESPACE
from cluster.cdk8s.agentplane.runner_sandbox import KeyFile
from cluster.cdk8s.agentplane.sandbox_pod import SECRET_NAME as SANDBOX_SECRET_NAME, TOOL_CONFIG_GRANT
from cluster.cdk8s.external_secrets.minted_secret import mint_bearer_secret
from cluster.cdk8s.external_secrets.single_secret_store import single_secret_store
from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.forgejo_registry.pull_credentials import SECRET_NAME as REGISTRY_SECRET_NAME
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.generation import CNPG_DATABASE_READY, sops_decryption
from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT
from cluster.cdk8s.model_selections import STAGING_APP_MODELS, model_configs
from cluster.cdk8s.providers.agentplane.policy_set import PolicySet, Rule
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, IngressRule, NetworkPolicy
from cluster.cdk8s.providers.external_secrets.external_secret import ExternalSecret, SecretStoreRef, remote_data
from cluster.cdk8s.public_coder import egress as public_coder_egress
from cluster.cdk8s.ssh_mcp.config import BEARER_SECRET_KEY, BEARER_SECRET_NAME, MCP_URL

_HOSTNAME = "agentplane-staging.allegedly.works"
_ACTIONS_HOSTNAME = "agentplane-actions-staging.allegedly.works"
_NOTIFICATIONS_HOSTNAME = "agentplane-notifications-staging.allegedly.works"
_AUTHENTIK = "https://auth.allegedly.works"
_ACTIONS_OIDC_APP = f"{_AUTHENTIK}/application/o/agentplane-staging-actions"
# The push services web-push subscriptions may target: both the Action Service's own
# allowlist and its egress rule, so the policy cannot drift from what the app accepts.
_WEB_PUSH_ALLOWED_HOSTS = ("fcm.googleapis.com", "updates.push.services.mozilla.com")
_GITHUB_MCP_URL = "https://api.githubcopilot.com/mcp/"
_KUBERNETES_MCP_URL = "https://kubectl-passthrough-mcp.allegedly.works/mcp"
# Calls go to its Service, as to every other in-cluster backend: through the public Gateway, about one
# request in four answered 503 after 5s. Linking keeps the public URL, the resource its token is for.
_KUBERNETES_MCP_SERVICE_URL = f"{kubectl_passthrough_mcp.SERVICE.url}/mcp"
_GROCY_SF_MCP_URL = "https://grocy-mcp-sf.allegedly.works/mcp"
_MCP_CLIENT_METADATA_URL = f"https://{_ACTIONS_HOSTNAME}/oauth/client-metadata.json"

# Separate harness and executor caps leave 1 GiB below the service high watermark.
# The outer service cap leaves 1 GiB for guest services and kernel work.
STAGING_VM_RESOURCES = GuestResources(
    guest_memory_bytes=8_589_934_592,
    guest_vcpu_count=1,
    os_reserve_bytes=1_073_741_824,
    service_memory_high_bytes=6_442_450_944,
    service_memory_max_bytes=7_516_192_768,
    service_cpu_quota_micros=100_000,
    service_pids_max=2048,
    aggregate=ResourceLimits(
        cpu_quota_micros=100_000,
        memory_max_bytes=2_147_483_648,
        pids_max=768,
        read_bps=52_428_800,
        write_bps=20_971_520,
        read_iops=1000,
        write_iops=500,
    ),
    process=ResourceLimits(
        cpu_quota_micros=100_000,
        memory_max_bytes=1_610_612_736,
        pids_max=512,
        read_bps=52_428_800,
        write_bps=20_971_520,
        read_iops=1000,
        write_iops=500,
    ),
    executor=ResourceLimits(
        cpu_quota_micros=100_000,
        memory_max_bytes=3_221_225_472,
        pids_max=512,
        read_bps=52_428_800,
        write_bps=20_971_520,
        read_iops=1000,
        write_iops=500,
    ),
)
_HOME_ASSISTANT_MCP_URL = f"{ha_mcp.FACADE.url}/mcp"
_TANA_MCP_URL = "http://tana-mcp.tana-mcp.svc.cluster.local:8263/mcp"
# One google-mcp pod (cluster/cdk8s/google_mcp.py) serves both tool sets at
# distinct paths -- see that module's docstring for its Google credential.
_GMAIL_MCP_URL = "http://google-mcp.google-mcp.svc.cluster.local:8080/gmail/mcp"
_CALENDAR_MCP_URL = "http://google-mcp.google-mcp.svc.cluster.local:8080/calendar/mcp"
# ssh-mcp, ha-mcp and google-mcp each mint their bearer in their own namespace
# (cluster/cdk8s/ssh_mcp/backend.py, ha_mcp.py, google_mcp.py), and this namespace copies it
# through a store that can read that one Secret; the Tana PAT is an external-creds copy approved
# for this namespace (cluster/cdk8s/external_creds.py).
_SSH_MCP_BEARER_SECRET = "ssh-mcp-client-bearer"
_HA_MCP_BEARER_SECRET = "ha-mcp-client-bearer"
_TANA_MCP_BEARER_SECRET = "tana-agentydragon-gmail-com-account-pat"
_GOOGLE_MCP_BEARER_SECRET = "google-mcp-bearer"
_WEB_PUSH_SECRET = "agentplane-staging-web-push-vapid"
_WEB_PUSH_SECRET_FILE = "web-push-vapid.sops.yaml"
_GITHUB_MCP_CLIENT_SECRET = "haku-console-github-mcp-client-credentials"
_LITELLM_KEY_SECRET = "litellm-key-agentplane-staging"
_OIDC_SESSION_SECRET = "agentplane-staging-session-secret"
_CONNECTION_SERVICE = mcp_edge.service(STAGING_NAMESPACE)
# staging's one Pod execution environment, and the ducktape tools image it runs; `image-pins/` selects
# the published tag. Live Sandboxes and presets name this template, so it keeps the name it shipped with.
POD_TEMPLATE = "runner-ducktape"
_RUNNER_IMAGE = "git.allegedly.works/ducktape-ci/runner-ducktape"
# The canonical BuildBuddy key, projected into the runner's workload containers for real `bbr` runs and
# named there for the harnesses it spawns (agentplane/docs/buildbuddy_remote_auth.md's stopgap). ESO
# copies the secret below; the egress policy still bounds where the key can travel.
_BUILDBUDDY_KEY = KeyFile(
    secret_name="buildbuddy-api-key",
    mount_path="/run/buildbuddy",
    file_name="api-key",
    env_var="BBR_BUILDBUDDY_API_KEY_FILE",
)

# The token the app exchanges its login for, and the one the Action Service accepts
# from operators: the same Authentik application.
_FEDERATION_TARGET = OperatorOidcSettings(
    issuer=f"{_ACTIONS_OIDC_APP}/", audience="agentplane-staging-actions", jwks_uri=f"{_ACTIONS_OIDC_APP}/jwks/"
)
_ACTION_FEDERATION = ExchangeFederationSettings(
    mode="exchange",
    service_url=AnyHttpUrl(actions.service(STAGING_NAMESPACE).url),
    token_endpoint=AnyHttpUrl(f"{_AUTHENTIK}/application/o/token/"),
    login_jwks_uri=f"{_AUTHENTIK}/application/o/agentplane-staging/jwks/",
    target=_FEDERATION_TARGET,
    scope="openid",
)
_MCP_SERVERS = {
    "github": McpOAuthServer(
        server_id="github",
        server_url=_GITHUB_MCP_URL,
        client_id="configured-by-secret",
        client_secret_file=Path("/etc/agentplane-github/client_secret"),
        redirect_uri=f"https://{_HOSTNAME}/mcp-linkage/callback",
    ),
    "kubernetes_admin": McpOAuthServer(
        server_id="kubernetes_admin",
        server_url=_KUBERNETES_MCP_URL,
        client_id="kubectl-passthrough-mcp",
        redirect_uri=f"https://{_HOSTNAME}/mcp-linkage/callback",
    ),
    "grocy_sf": McpOAuthServer(
        server_id="grocy_sf",
        server_url=_GROCY_SF_MCP_URL,
        use_shared_cimd=True,
        redirect_uri=f"https://{_HOSTNAME}/mcp-linkage/callback",
    ),
}
_ACTION_GROUPS = {
    "github": ActionGroup(
        title="GitHub MCP",
        description="GitHub's operator-linked MCP tools; every Action remains subject to operator approval.",
        executor=McpExecutorBinding(
            kind="mcp",
            description="GitHub MCP executed with the linked operator GitHub account.",
            config={
                "transport": "streamable-http",
                "url": _GITHUB_MCP_URL,
                "server_id": "github",
                "auth": "oauth",
                # Actions (get_job_logs, actions_get, actions_list, ...) is not in GitHub
                # MCP's default toolset catalog. `_REPOSITORY_SCOPED_ACTIONS` in
                # actions_staging_policies.py already expects these tools; without this
                # header the server never advertises them.
                "headers": {"X-MCP-Toolsets": "default,actions"},
            },
        ),
    ),
    "kubernetes_admin": ActionGroup(
        title="Kubernetes admin",
        description=(
            "The Kubernetes API with the linked operator's own permissions. Use it only for what your own "
            "Kubernetes identity cannot do; each call waits for the operator's approval."
        ),
        executor=McpExecutorBinding(
            kind="mcp",
            description="kubectl-passthrough-mcp, run as the linked operator's Kubernetes identity.",
            config={
                "transport": "streamable-http",
                "url": _KUBERNETES_MCP_SERVICE_URL,
                "server_id": "kubernetes_admin",
                "auth": "oauth",
            },
        ),
    ),
    "grocy_sf": ActionGroup(
        title="Grocy SF MCP",
        description="Grocy SF household MCP tools; every Action remains subject to operator approval.",
        executor=McpExecutorBinding(
            kind="mcp",
            description="Grocy SF MCP executed with the linked operator Grocy account.",
            config={"transport": "streamable-http", "url": _GROCY_SF_MCP_URL, "server_id": "grocy_sf", "auth": "oauth"},
        ),
    ),
    "sandbox": ActionGroup(
        title="Sandbox",
        description=(
            "Sandboxes that run as the calling ServiceAccount, and bounded commands in them. A "
            "sandbox reaches what its caller's egress bindings allow and is admitted back to this "
            "service as that same caller, so it confers no authority the caller did not hold."
        ),
        executor=PodExecutorBinding(
            kind="pod",
            description="Created and exec'd by this service, as the caller, in its own namespace.",
            namespace=STAGING_NAMESPACE,
            templates=command_sandbox.TEMPLATES,
        ),
        # claude.ai and Claude Code reach these as MCP tools of their own, where `sandbox-self`
        # auto-approves them for the Connection's claude-ai account.
        direct_tools=frozenset(SandboxAction),
    ),
    "ssh": ActionGroup(
        title="SSH",
        description="SSH commands on configured targets; every Action remains subject to operator approval.",
        executor=McpExecutorBinding(
            kind="mcp",
            description="SSH MCP backend (ssh-mcp); Agentplane retains approval and execution authority.",
            config={
                "transport": "streamable-http",
                "url": MCP_URL,
                "auth": "static_bearer",
                "bearer_file": "/run/secrets/ssh-mcp/bearer-token",
            },
        ),
    ),
    "home_assistant": ActionGroup(
        title="Home Assistant MCP",
        description="Home Assistant tools; every Action remains subject to operator approval.",
        executor=McpExecutorBinding(
            kind="mcp",
            description="Home Assistant MCP backend (ha-mcp).",
            config={
                "transport": "streamable-http",
                "url": _HOME_ASSISTANT_MCP_URL,
                "auth": "static_bearer",
                "bearer_file": "/run/secrets/ha-mcp/bearer-token",
            },
        ),
    ),
    "tana": ActionGroup(
        title="Tana MCP",
        description="Tana read/write tools; every Action remains subject to operator approval.",
        executor=McpExecutorBinding(
            kind="mcp",
            description="Tana MCP backend (tana-mcp).",
            config={
                "transport": "streamable-http",
                "url": _TANA_MCP_URL,
                "auth": "static_bearer",
                "bearer_file": "/run/secrets/tana-mcp/bearer-token",
            },
        ),
    ),
    "gmail": ActionGroup(
        title="Gmail",
        description="Gmail read/write tools; every Action remains subject to operator approval.",
        executor=McpExecutorBinding(
            kind="mcp",
            description="Gmail MCP backend (google-mcp), on a write-scoped Google credential.",
            config={
                "transport": "streamable-http",
                "url": _GMAIL_MCP_URL,
                "auth": "static_bearer",
                "bearer_file": "/run/secrets/google-mcp/bearer-token",
            },
        ),
    ),
    "google_calendar": ActionGroup(
        title="Google Calendar",
        description="Google Calendar read/write tools; every Action remains subject to operator approval.",
        executor=McpExecutorBinding(
            kind="mcp",
            description="Google Calendar MCP backend (google-mcp), on a write-scoped Google credential.",
            config={
                "transport": "streamable-http",
                "url": _CALENDAR_MCP_URL,
                "auth": "static_bearer",
                "bearer_file": "/run/secrets/google-mcp/bearer-token",
            },
        ),
    ),
}

LAYOUT = ManifestLayout(
    flux_description=(
        "Complete Agentplane staging environment, including namespace, database, egress, LLM ingress, "
        "Actions, app, runner template, and operator RBAC."
    ),
    output_dir=f"{HAND_WRITTEN_ROOT}/{STAGING_NAMESPACE}",
    image_pins=f"{HAND_WRITTEN_ROOT}/{STAGING_NAMESPACE}/image-pins",
    delivery_output_dir=f"{GENERATED_ROOT}/{STAGING_NAMESPACE}-delivery",
    extra_resources=(_WEB_PUSH_SECRET_FILE, "github-app.sops.yaml"),
)
ENV = Environment(
    namespace=STAGING_NAMESPACE,
    description=(
        "Agentplane staging - sandboxed runner Pods (one per Sandbox) and the integration app that drives them."
    ),
    replicas=ReplicaProfile(
        count=2,
        strategy=DeploymentStrategy.rolling_update(
            max_surge=PercentOrAbsolute.absolute(1), max_unavailable=PercentOrAbsolute.absolute(0)
        ),
        min_ready=Duration.seconds(5),
        pdb_min_available=1,
    ),
    app_config=staging_config.config(
        namespace=STAGING_NAMESPACE,
        sandbox_template=POD_TEMPLATE,
        models=STAGING_APP_MODELS,
        action_federation=_ACTION_FEDERATION,
        connection_service_url=AnyHttpUrl(_CONNECTION_SERVICE.url),
    ),
    settle_deltas=True,
    notifications_github=GitHubAppProps(app_id=5188971, secret_name="agentplane-github-app"),
    kubernetes_binding_cleanup_namespaces=frozenset(agent_access_profiles.cleanup_namespaces()),
    llm_ingress=LlmIngressProps(
        litellm_key_secret_name=_LITELLM_KEY_SECRET,
        models=model_configs(STAGING_APP_MODELS),
        codex_routes=STAGING_APP_MODELS.codex,
        log_llm_requests=True,
        attribution=config_pb2.AttributionConfig(
            json_headers=[
                config_pb2.AttributionJsonHeader(
                    name="x-litellm-spend-logs-metadata",
                    fields=[
                        config_pb2.AttributionField(key="agentplane.namespace", source=config_pb2.namespace),
                        config_pb2.AttributionField(
                            key="agentplane.service_account", source=config_pb2.service_account
                        ),
                        config_pb2.AttributionField(key="agentplane.pod_name", source=config_pb2.pod_name),
                        config_pb2.AttributionField(key="agentplane.pod_uid", source=config_pb2.pod_uid),
                    ],
                )
            ],
            baggage=[
                config_pb2.AttributionField(
                    key="litellm.metadata.spend_logs_metadata.agentplane.namespace", source=config_pb2.namespace
                ),
                config_pb2.AttributionField(
                    key="litellm.metadata.spend_logs_metadata.agentplane.service_account",
                    source=config_pb2.service_account,
                ),
                config_pb2.AttributionField(
                    key="litellm.metadata.spend_logs_metadata.agentplane.pod_name", source=config_pb2.pod_name
                ),
                config_pb2.AttributionField(
                    key="litellm.metadata.spend_logs_metadata.agentplane.pod_uid", source=config_pb2.pod_uid
                ),
            ],
        ),
    ),
    egress=EgressProps(
        ca_secret_name=public_coder_egress.CA_BUNDLE_NAME,
        credentials_namespace=STAGING_CREDENTIALS_NAMESPACE,
        replicas=ReplicaProfile(
            count=3,
            strategy=DeploymentStrategy.rolling_update(
                max_surge=PercentOrAbsolute.absolute(1), max_unavailable=PercentOrAbsolute.absolute(0)
            ),
            min_ready=Duration.seconds(5),
            pdb_min_available=2,
        ),
        traffic_distribution="PreferClose",
        zones=node_scheduling.HOME_AND_OVH_ZONES,
        external_workload_namespaces=(public_coder_egress.NAMESPACE,),
    ),
    app=AppProps(
        origin=f"https://{_HOSTNAME}",
        oidc_issuer=f"{_AUTHENTIK}/application/o/agentplane-staging/",
        reach_incluster_authentik=True,
        runner_zone=node_scheduling.HIL_OVH_ZONE,
        oidc_session_secret_name=_OIDC_SESSION_SECRET,
    ),
    actions=ActionsProps(
        hostname=_ACTIONS_HOSTNAME,
        operator_oidc=_FEDERATION_TARGET,
        caller_service_account_namespaces=frozenset({STAGING_NAMESPACE, public_coder_egress.NAMESPACE}),
        mcp_servers=_MCP_SERVERS,
        action_groups=_ACTION_GROUPS,
        mcp_client_metadata=McpClientMetadataSettings(
            url=AnyHttpUrl(_MCP_CLIENT_METADATA_URL), client_name="Agentplane staging"
        ),
        web_push=WebPushProps(
            subject="mailto:agentydragon@gmail.com",
            public_base_url=f"https://{_HOSTNAME}",
            allowed_push_hosts=_WEB_PUSH_ALLOWED_HOSTS,
            secret_name=_WEB_PUSH_SECRET,
        ),
        oauth_secret_items=(),
        github_mcp_client_secret_name=_GITHUB_MCP_CLIENT_SECRET,
        bearer_mcp_mounts=[
            BearerMcpMount(name="ssh-mcp", secret_name=_SSH_MCP_BEARER_SECRET, secret_key=BEARER_SECRET_KEY),
            BearerMcpMount(name="ha-mcp", secret_name=_HA_MCP_BEARER_SECRET, secret_key="bearer-token"),
            # The Secret's own key is `token` (it's a Tana personal access token, not a
            # bearer minted for this purpose); renamed at mount time to the same
            # `bearer-token` file name every other static-bearer group uses.
            BearerMcpMount(name="tana-mcp", secret_name=_TANA_MCP_BEARER_SECRET, secret_key="token", optional=True),
            # One mount, shared by both the gmail and google_calendar ActionGroups -- one pod,
            # one caller-facing bearer.
            BearerMcpMount(name="google-mcp", secret_name=_GOOGLE_MCP_BEARER_SECRET, secret_key="bearer-token"),
        ],
        extra_egress=[
            EgressRule.to_fqdns(*_WEB_PUSH_ALLOWED_HOSTS),
            EgressRule.to_endpoints(cilium.endpoint_labels("ssh-mcp", "ssh-mcp"), 8080),
            ha_mcp.FACADE.egress(),
            EgressRule.to_endpoints(cilium.endpoint_labels("tana-mcp", "tana-mcp"), 8263),
            EgressRule.to_endpoints(cilium.endpoint_labels("google-mcp", "google-mcp"), 8080),
            # Same public-origin Gateway path as the BFF: only Authentik SNI on node:443. The
            # resolver fetches /application/o/agentplane-staging-actions/jwks/ over HTTPS.
            cilium.egress_via_gateway("auth.allegedly.works"),
            # GitHub MCP discovery advertises github.com as its OAuth authorization server.
            EgressRule.to_fqdns("api.githubcopilot.com", "github.com"),
            # Claude and Codex publish their public CIMD documents at these origins.
            EgressRule.to_fqdns("claude.ai", "chatgpt.com"),
            # `github_public_repository` policies confirm a repository is public with an
            # unauthenticated Go GitHub REST lookup (agentplane/action_service/policies/visibility.go); no credential
            # rides this path.
            EgressRule.to_fqdns("api.github.com"),
            kubectl_passthrough_mcp.SERVICE.egress(),
            # Linking probes the Kubernetes MCP server's public URL for its OAuth metadata.
            cilium.egress_via_gateway("kubectl-passthrough-mcp.allegedly.works"),
            # Grocy SF's MCP server (OAuth discovery, DCR, and the linked /mcp calls) is the
            # same public Gateway path.
            cilium.egress_via_gateway("grocy-mcp-sf.allegedly.works"),
            EgressRule.to_endpoints(cilium.AUTHENTIK_SERVER_LABELS, 9000, server_names=["auth.allegedly.works"]),
        ],
        # Every Action but launch_sandbox, which always waits for the operator: the rest are auto-approved for
        # claude-ai (list_launch_presets) or by a launcher's grants, and answer in one call.
        orchestration_v1=OrchestrationProps(
            direct_tools=frozenset(
                {
                    "list_launch_presets",
                    "open_session",
                    "submit_input",
                    "interrupt_turn",
                    "change_model",
                    "change_reasoning_effort",
                    "get_session",
                    "list_sessions",
                    "read_turns",
                    "wait_for_turn",
                }
            )
        ),
    ),
    mcp_edge=McpEdgeProps(
        replicas=ReplicaProfile(
            count=1, strategy=DeploymentStrategy.recreate(), min_ready=None, pdb_min_available=None
        ),
        operator_oidc=_FEDERATION_TARGET,
        caller_service_account_namespaces=frozenset({STAGING_NAMESPACE}),
        mintable_accounts=frozenset({ServiceAccountRef(namespace=STAGING_NAMESPACE, name="claude-ai")}),
        oauth_secret_name="agentplane-mcp-oauth",
        hostname=_ACTIONS_HOSTNAME,
        extra_egress=(cilium.egress_via_gateway("auth.allegedly.works"), EgressRule.to_fqdns("claude.ai")),
    ),
    vm_sandboxes=True,
    vm_storage_class="piraeus-ssd-2-autodiskful",
    vm_resources=STAGING_VM_RESOURCES,
    wake_caller_accounts=frozenset(
        {
            ServiceAccountRef(namespace=STAGING_NAMESPACE, name=app_component.NAME),
            ServiceAccountRef(namespace=STAGING_NAMESPACE, name=notifications.NAME),
        }
    ),
)


def assignable_policy_sets() -> dict[str, tuple[KubernetesGrant, ...]]:
    """The complete PolicySet grant inventory materialized by staging."""
    catalog = staging_grants.catalog(ENV.namespace)
    sets = agent_access_profiles.policy_set_grants(catalog)
    sets[TOOL_CONFIG_GRANT] = (catalog[TOOL_CONFIG_GRANT],)
    return sets


def assignable_grants() -> tuple[KubernetesGrant, ...]:
    return tuple(grant for grants in assignable_policy_sets().values() for grant in grants)


def chart(app: App) -> Chart:
    chart = environment_chart(
        app,
        ENV,
        db=DbProps(
            instances=2, wal_archive=True, profile=cnpg.OVH_PIRAEUS_SSD, size="16Gi", resize_in_use_volumes=True
        ),
        runner_name=POD_TEMPLATE,
        runner_image=_RUNNER_IMAGE,
        runner_key_files=(_BUILDBUDDY_KEY,),
    )
    NetworkPolicy(
        chart,
        "networkpolicy-app-to-mcp-edge",
        metadata=ApiObjectMetadata(name="app-to-mcp-edge", namespace=ENV.namespace),
        endpoint_selector=app_component.service(ENV.namespace).pods.selector,
        egress=[_CONNECTION_SERVICE.egress()],
    )
    # Reuse the canonical key via ESO; do not grant the sandbox ServiceAccount get-secrets RBAC. The
    # runner template projects `_BUILDBUDDY_KEY` into its workload containers only.
    credential_external_secret(
        chart,
        namespace=STAGING_NAMESPACE,
        target=_BUILDBUDDY_KEY.secret_name,
        source="buildbuddy-api-key",
        key=_BUILDBUDDY_KEY.file_name,
        store=EXTERNAL_CREDS_STORE,
    )
    notification_service = notifications.service(STAGING_NAMESPACE)
    https_route(
        chart,
        "notifications-github-webhook-route",
        metadata=ApiObjectMetadata(name="notifications-github-webhook", namespace=STAGING_NAMESPACE),
        hostnames=[_NOTIFICATIONS_HOSTNAME],
        backend=notification_service,
        paths=["/v1/webhooks/github"],
    )
    # Add to the workload-only policy.
    NetworkPolicy(
        chart,
        "notifications-github-webhook-policy",
        metadata=ApiObjectMetadata(name="notifications-github-webhook", namespace=STAGING_NAMESPACE),
        endpoint_selector=notification_service.pods.selector,
        ingress=[IngressRule.from_gateway(notification_service.pod_port)],
    )
    command_sandbox.CommandSandbox(
        chart, "command-sandbox", ENV, image_pull_secret_name=SANDBOX_SECRET_NAME
    ).add_platform_resources()
    # Compose staging's complete PolicySets from the shared grant definitions and its own egress
    # rules before materializing them. The controller workload and its RBAC are separate constructs.
    assignable_sets = assignable_policy_sets()
    deployment_rules = {
        agent_access_profiles.COINBASE_POLICY: coinbase_egress_rules(),
        agent_access_profiles.PLAID_DIAGNOSTICS_POLICY: agent_access_profiles.plaid_pgweb_egress_rules(
            credential=PLAID_PGWEB_CREDENTIAL
        ),
    }
    for name, grants in sorted(assignable_sets.items()):
        PolicySet(
            chart,
            f"policy-set-{name}",
            metadata=ApiObjectMetadata(name=name, namespace=ENV.namespace),
            rules=[*agent_access_profiles.rules_for_grants(grants), *deployment_rules.get(name, ())],
        )
    for group in staging_config.INBOX_GROUPS:
        PolicySet(
            chart,
            f"policy-set-{staging_config.send_to(group)}",
            metadata=ApiObjectMetadata(name=staging_config.send_to(group), namespace=ENV.namespace),
            rules=[Rule.send_message(inbox_tags=[group])],
        )
    controller = policy_controller.PolicyController(
        chart, "policy-controller", ENV, placement=node_scheduling.HIL_OVH, registry_secret_name=REGISTRY_SECRET_NAME
    )
    controller.add_platform_resources()
    policy_controller_rbac.PolicyControllerRbac(
        chart,
        "policy-controller-rbac",
        ENV,
        account=controller.account,
        assignable_grants=[grant for grants in assignable_sets.values() for grant in grants],
    )
    reader = ServiceAccount(
        chart,
        "external-creds-reader",
        metadata=ApiObjectMetadata(name="external-creds-reader", namespace=STAGING_NAMESPACE),
        automount_token=False,
    )
    ExternalSecret(
        chart,
        "litellm-key-external-secret",
        metadata=ApiObjectMetadata(
            name=_LITELLM_KEY_SECRET,
            namespace=STAGING_NAMESPACE,
            annotations={"description": "ESO copy of the Crossplane-managed Agentplane staging LiteLLM key."},
        ),
        refresh_interval="1h",
        secret_store_ref=SecretStoreRef.cluster(
            single_secret_store(
                chart,
                "agentplane-staging-litellm-key",
                reader=reader,
                source_namespace="litellm",
                source_secret=_LITELLM_KEY_SECRET,
                consumer_namespace=STAGING_NAMESPACE,
            )
        ),
        data=[remote_data(_LITELLM_KEY_SECRET, "api-key")],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
    )
    ExternalSecret(
        chart,
        "tana-pat-external-secret",
        metadata=ApiObjectMetadata(
            name=_TANA_MCP_BEARER_SECRET,
            namespace=STAGING_NAMESPACE,
            annotations={"description": "ESO copy of the canonical Tana PAT from external-creds."},
        ),
        refresh_interval="1h",
        secret_store_ref=external_creds.STORE,
        data=[remote_data(_TANA_MCP_BEARER_SECRET, "token")],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
    )
    for backend, target, source in (
        ("ssh-mcp", _SSH_MCP_BEARER_SECRET, BEARER_SECRET_NAME),
        ("google-mcp", _GOOGLE_MCP_BEARER_SECRET, _GOOGLE_MCP_BEARER_SECRET),
        ("ha-mcp", _HA_MCP_BEARER_SECRET, "ha-mcp-bearer"),
    ):
        credential_external_secret(
            chart,
            namespace=STAGING_NAMESPACE,
            target=target,
            source=source,
            key="bearer-token",
            store=single_secret_store(
                chart,
                f"agentplane-staging-{backend}-bearer",
                reader=reader,
                source_namespace=backend,
                source_secret=source,
                consumer_namespace=STAGING_NAMESPACE,
            ),
        )
    # The GitHub App's pre-registered OAuth client, whose SOPS source stays in haku-console
    # (cluster/k8s/haku/console/README.md): the id rides an env var, the secret a mounted file.
    ExternalSecret(
        chart,
        "github-mcp-client-external-secret",
        metadata=ApiObjectMetadata(name=_GITHUB_MCP_CLIENT_SECRET, namespace=STAGING_NAMESPACE),
        refresh_interval="1h",
        secret_store_ref=SecretStoreRef.cluster(
            single_secret_store(
                chart,
                "agentplane-staging-github-mcp-client",
                reader=reader,
                source_namespace="haku-console",
                source_secret=_GITHUB_MCP_CLIENT_SECRET,
                consumer_namespace=STAGING_NAMESPACE,
            )
        ),
        data=[remote_data(_GITHUB_MCP_CLIENT_SECRET, key) for key in ("client_id", "client_secret")],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
    )
    _add_session_secret(chart)
    add_staging_action_policies(chart)
    EgressCredentials(
        chart, "egress-credentials", namespace=ENV.egress.credentials_namespace, proxy_namespace=ENV.namespace
    )
    add_staging_egress_credentials(
        chart, namespace=ENV.namespace, credentials_namespace=ENV.egress.credentials_namespace
    )
    return chart


def _add_session_secret(scope: Chart) -> None:
    """Generate the staging app's local session-signing key with ESO.

    Rotating this value invalidates existing browser sessions, but does not touch the
    Authentik OAuth client credentials.
    """
    mint_bearer_secret(
        scope,
        "session-external-secret",
        name=_OIDC_SESSION_SECRET,
        namespace=STAGING_NAMESPACE,
        key="session-secret",
        length=64,
        digits=16,
        creation_policy=ExternalSecretSpecTargetCreationPolicy.ORPHAN,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
        immutable=True,
        description="ESO-generated Agentplane staging session-signing key.",
    )


def agentplane_staging(
    flux_chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    health_checks: list[KustomizationSpecHealthChecks],
    agentplane_crds: Kustomization,
    cert_manager_trust: Kustomization,
    cnpg: Kustomization,
    external_secrets_operator: Kustomization,
    kyverno: Kustomization,
    crossplane_keys: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        flux_chart,
        ENV.namespace,
        artifact,
        description=LAYOUT.flux_description,
        suspend=False,
        wait=None,
        timeout="10m",
        # This one Kustomization owns the CNPG Cluster's PVCs; pruning on
        # deletion would take the database with them.
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        health_checks=[
            *health_checks,
            *[
                KustomizationSpecHealthChecks(
                    api_version="external-secrets.io/v1",
                    kind="ExternalSecret",
                    name=name,
                    namespace=ENV.egress.credentials_namespace,
                )
                for name in (
                    public_coder_egress.CLICKHOUSE_CREDENTIAL,
                    public_coder_egress.MATRIX_CREDENTIAL,
                    public_coder_egress.BRAVE_CREDENTIAL,
                )
            ],
            KustomizationSpecHealthChecks(
                api_version="external-secrets.io/v1",
                kind="ExternalSecret",
                name=_LITELLM_KEY_SECRET,
                namespace=STAGING_NAMESPACE,
            ),
            KustomizationSpecHealthChecks(
                api_version="external-secrets.io/v1",
                kind="ExternalSecret",
                name=_OIDC_SESSION_SECRET,
                namespace=STAGING_NAMESPACE,
            ),
        ],
        health_check_exprs=[
            KustomizationSpecHealthCheckExprs(
                api_version="postgresql.cnpg.io/v1", kind="Database", current=CNPG_DATABASE_READY
            )
        ],
        decryption=sops_decryption(LAYOUT.extra_resources),
        depends_on=flux_kustomization_depends_on_many(
            agentplane_crds, cert_manager_trust, cnpg, external_secrets_operator, kyverno, crossplane_keys
        ),
    )
