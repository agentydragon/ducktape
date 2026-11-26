"""agentplane-staging: replicated services, operator login federated through the
shared Authentik, and the reviewed GitHub/Kubernetes/Grocy SF/SSH/Home Assistant/Tana/Gmail/
Google Calendar MCP action groups.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from agentplane_kubernetesroleset_crds.works.allegedly.agentplane import KubernetesRoleSet, KubernetesRoleSetSpec
from agentplane_policyset_crds.works.allegedly.agentplane import PolicySet, PolicySetSpec
from cdk8s import ApiObjectMetadata, App, Chart, Duration
from cdk8s_plus_34 import DeploymentStrategy, PercentOrAbsolute, ServiceAccount
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy, KustomizationSpecHealthChecks
from pydantic import AnyHttpUrl
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from agentplane.action_service.catalog import ActionGroup, McpExecutorBinding
from agentplane.action_service.mcp_settings import McpClientMetadataSettings, McpOAuthServer
from agentplane.action_service.sandbox.binding import PodExecutorBinding
from agentplane.action_service.sandbox.contract.actions import Action
from agentplane.app.action_federation_settings import ExchangeFederationSettings
from agentplane.env.config import Executor, GuestResources, ResourceLimits, SystemdExecutorOwner
from agentplane.identity.operator.settings import OperatorOidcSettings
from agentplane.llm_ingress.models import AttributionConfig, AttributionField, AttributionJsonHeader, VerifiedClaim
from agentplane.net.peertls.config import ACTIONS_URI, CENTRAL_SAMPLER_URI, LLM_INGRESS_URI, TLS
from agentplane.notification_service.settings import NoticeDebounceSettings
from agentplane.sampler.settings import (
    ActionsConfig,
    AnthropicBlockBindingConfig,
    AnthropicCompactionConfig,
    AnthropicMessagesConfig,
    AnthropicPromptCacheConfig,
    AnthropicThinkingConfig,
    AuthConfig,
    ChatCompactionConfig,
    ChatCompactionReplacement,
    ConnectionConfig,
    ModelConfig,
    ResponsesConfig,
)
from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s import (
    agent_access_profiles,
    cilium,
    external_creds,
    google_mcp,
    ha_mcp,
    kubectl_passthrough_mcp,
    node_scheduling,
    piraeus_storage,
    tana_mcp,
)
from cluster.cdk8s.agentplane import (
    actions,
    app as app_component,
    app_settings,
    command_sandbox,
    electric,
    environment_firecracker,
    environment_vm,
    kubernetes_account_ref,
    llm_ingress,
    mcp_edge,
    notifications,
    policy_controller,
    policy_controller_rbac,
    runner_gateway_workload,
    sandbox_controller,
    staging_config,
    workload_certificate_admission,
    workload_tls,
)
from cluster.cdk8s.agentplane.actions_staging_policies import (
    ASSIGNABLE_EGRESS_RULES,
    CLAUDE_AI,
    HAKU_AGENT,
    add_staging_action_policies,
    caller_role_sets,
    identity_egress_set_name,
)
from cluster.cdk8s.agentplane.app_settings import runtime_catalog
from cluster.cdk8s.agentplane.chart import environment_chart
from cluster.cdk8s.agentplane.codex_models import codex_auto_compact_tokens
from cluster.cdk8s.agentplane.database import DbProps
from cluster.cdk8s.agentplane.egress_credentials import (
    EXTERNAL_CREDS_STORE,
    STAGING_CREDENTIALS_NAMESPACE,
    EgressCredentials,
    credential_external_secret,
)
from cluster.cdk8s.agentplane.egress_staging_credentials import add_staging_egress_credentials
from cluster.cdk8s.agentplane.environment import (
    ActionsProps,
    AppProps,
    BearerMcpMount,
    CentralSamplerProps,
    EgressProps,
    Environment,
    GitHubAppProps,
    LlmIngressProps,
    McpEdgeProps,
    OrchestrationProps,
    ReplicaProfile,
    SessionDelegationProps,
    WebPushProps,
)
from cluster.cdk8s.agentplane.environment_sandbox import EnvironmentSandboxTemplate, KeyFile
from cluster.cdk8s.agentplane.generation import ManifestLayout
from cluster.cdk8s.agentplane.kubernetes_account_ref import shared_diagnostics
from cluster.cdk8s.agentplane.kubernetes_accounts import KubernetesAccount, KubernetesAccounts
from cluster.cdk8s.agentplane.kubernetes_grants import KubernetesGrant
from cluster.cdk8s.agentplane.model_selections import STAGING_APP_MODELS, chat_completions_id, model_configs
from cluster.cdk8s.agentplane.namespaces import STAGING_NAMESPACE, kubernetes_accounts_namespace
from cluster.cdk8s.agentplane.sandbox_pod import (
    SECRET_NAME as SANDBOX_SECRET_NAME,
    TOOL_CONFIG_GRANT,
    tool_config_grant,
)
from cluster.cdk8s.cnpg import cluster
from cluster.cdk8s.cnpg.health import database_health
from cluster.cdk8s.external_secrets.minted_secret import mint_bearer_secret
from cluster.cdk8s.external_secrets.single_secret_store import single_secret_store
from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.forgejo_registry.pull_credentials import SECRET_NAME as REGISTRY_SECRET_NAME
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.generation import sops_decryption
from cluster.cdk8s.haku import namespace as haku_namespace, ui_egress as haku_ui_egress
from cluster.cdk8s.haku_ci import egress_gateway, runner
from cluster.cdk8s.litellm.secrets import ANTHROPIC_API_KEY
from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT
from cluster.cdk8s.providers.agentplane.identity import Identity
from cluster.cdk8s.providers.agentplane.policy_set import Rule
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, IngressRule, NetworkPolicy
from cluster.cdk8s.providers.external_secrets.external_secret import ExternalSecret, SecretStoreRef, remote_data
from cluster.cdk8s.public_coder import egress as public_coder_egress
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.ssh_mcp import config as ssh_mcp
from model_catalog.catalog import TABBYAPI_QWEN_ABLITERATED, TABBYAPI_QWEN_STOCK, Provider, Route, TokenLimits

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
_GOOGLE_MCP_ORIGIN = f"http://{google_mcp.SERVICE.authority}"
_MCP_CLIENT_METADATA_URL = f"https://{_ACTIONS_HOSTNAME}/oauth/client-metadata.json"
# Action-attributed Dispatcher sends use the Actions service's independent broker grant. The
# Environments service keeps its own wake allowlist from sandbox_controller.WAKE_SERVICE_CALLERS.
DISPATCHER_WAKE_SERVICE_CALLERS = sandbox_controller.WAKE_SERVICE_CALLERS | frozenset({ACTIONS_URI})


# Official OpenAI routes use Responses Lite and freeform Code mode.
# New third-party coding routes use standard Responses with JSON exec.
_LITE_RESPONSES = ResponsesConfig(dialect="lite")
_STANDARD_RESPONSES = ResponsesConfig(dialect="standard")
_ANTHROPIC_MESSAGES = AnthropicMessagesConfig(
    max_tokens=32_768,
    thinking=AnthropicThinkingConfig(
        type="adaptive", block_binding=AnthropicBlockBindingConfig(prefix_mismatch_behavior="error")
    ),
    prompt_cache=AnthropicPromptCacheConfig(ttl="5m"),
    cache_diagnostics=True,
    per_message_effort=True,
    strict_tool_input=True,
    eager_input_streaming=True,
    compaction=AnthropicCompactionConfig(max_tokens=8_192),
)


# Separate harness, executor, and command caps leave 1 GiB below the service high watermark.
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
        memory_max_bytes=1_073_741_824,
        pids_max=128,
        read_bps=52_428_800,
        write_bps=20_971_520,
        read_iops=1000,
        write_iops=500,
    ),
    executor_workload=ResourceLimits(
        cpu_quota_micros=100_000,
        memory_max_bytes=2_147_483_648,
        pids_max=384,
        read_bps=52_428_800,
        write_bps=20_971_520,
        read_iops=1000,
        write_iops=500,
    ),
)
_WEB_PUSH_SECRET = "agentplane-staging-web-push-vapid"
_WEB_PUSH_SECRET_FILE = "web-push-vapid.sops.yaml"
_GITHUB_MCP_CLIENT_SECRET = "haku-console-github-mcp-client-credentials"
_LITELLM_KEY_SECRET = "litellm-key-agentplane-staging"
_ANTHROPIC_API_KEY_SECRET = "agentplane-staging-anthropic-api-key"
_OIDC_SESSION_SECRET = "agentplane-staging-session-secret"
_CONNECTION_SERVICE = mcp_edge.tls_service(STAGING_NAMESPACE)
# Keep the existing template and its claim spec for retained workspaces. New Sandboxes can
# select the SSD template in the launch form.
POD_TEMPLATE = "environment-tools"
SSD_POD_TEMPLATE = "environment-tools-ssd"
_RUNTIME_CATALOG = runtime_catalog(
    STAGING_APP_MODELS,
    templates=(POD_TEMPLATE, SSD_POD_TEMPLATE, environment_vm.NAME, environment_firecracker.NAME),
    workspace_root_enforced_templates=(environment_vm.NAME, environment_firecracker.NAME),
    central_sampler_enabled=True,
)
_ENVIRONMENT_TOOLS_IMAGE = "git.allegedly.works/ducktape-ci/environment-tools"
# The canonical BuildBuddy key, projected into the Environment Runtime workload containers for real `bbr` runs and
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
        client_id_file=Path("/etc/agentplane-github/client_id"),
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
# One mount per static-bearer MCP backend: the Action Service mounts the Secret in `mount_dir`, and its
# group's `bearer_file` names `path` there.
_SSH_MCP_MOUNT = BearerMcpMount(name="ssh-mcp", secret_name="ssh-mcp-client-bearer", secret_key=ssh_mcp.BEARER.key)
_HA_MCP_MOUNT = BearerMcpMount(name="ha-mcp", secret_name="ha-mcp-client-bearer", secret_key=ha_mcp.BEARER.key)
# The Secret's own key is `token` (it's a Tana personal access token, not a
# bearer minted for this purpose); renamed at mount time to the same
# `bearer-token` file name every other static-bearer group uses.
_TANA_MCP_MOUNT = BearerMcpMount(
    name="tana-mcp",
    secret_name=external_creds.TANA_PAT.secret.name,
    secret_key=external_creds.TANA_PAT.key,
    optional=True,
)
# One mount, shared by both the gmail and google_calendar ActionGroups -- one pod,
# one caller-facing bearer.
_GOOGLE_MCP_MOUNT = BearerMcpMount(
    name="google-mcp", secret_name=google_mcp.BEARER.secret.name, secret_key=google_mcp.BEARER.key
)


def _static_bearer_group(
    mount: BearerMcpMount, *, title: str, description: str, executor_description: str, url: str
) -> ActionGroup:
    return ActionGroup(
        title=title,
        description=description,
        executor=McpExecutorBinding(
            kind="mcp",
            description=executor_description,
            config={"transport": "streamable-http", "url": url, "auth": "static_bearer", "bearer_file": mount.path},
        ),
    )


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
    actions.SANDBOX_GROUP: ActionGroup(
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
        direct_tools=frozenset(action.value for action in Action),
    ),
    "ssh": _static_bearer_group(
        _SSH_MCP_MOUNT,
        title="SSH",
        description="SSH commands on configured targets; every Action remains subject to operator approval.",
        executor_description="SSH MCP backend (ssh-mcp); Agentplane retains approval and execution authority.",
        url=ssh_mcp.MCP_URL,
    ),
    "home_assistant": _static_bearer_group(
        _HA_MCP_MOUNT,
        title="Home Assistant MCP",
        description="Home Assistant tools; every Action remains subject to operator approval.",
        executor_description="Home Assistant MCP backend (ha-mcp).",
        url=f"{ha_mcp.FACADE.url}/mcp",
    ),
    "tana": _static_bearer_group(
        _TANA_MCP_MOUNT,
        title="Tana MCP",
        description="Tana read/write tools; every Action remains subject to operator approval.",
        executor_description="Tana MCP backend (tana-mcp).",
        url=f"http://{tana_mcp.MCP_PROXY.authority}/mcp",
    ),
    "gmail": _static_bearer_group(
        _GOOGLE_MCP_MOUNT,
        title="Gmail",
        description="Gmail read/write tools; every Action remains subject to operator approval.",
        executor_description="Gmail MCP backend (google-mcp), on a write-scoped Google credential.",
        url=f"{_GOOGLE_MCP_ORIGIN}/gmail/mcp",
    ),
    "google_calendar": _static_bearer_group(
        _GOOGLE_MCP_MOUNT,
        title="Google Calendar",
        description="Google Calendar read/write tools; every Action remains subject to operator approval.",
        executor_description="Google Calendar MCP backend (google-mcp), on a write-scoped Google credential.",
        url=f"{_GOOGLE_MCP_ORIGIN}/calendar/mcp",
    ),
}

LAYOUT = ManifestLayout(
    flux_description=(
        "Complete Agentplane staging environment, including namespace, database, egress, LLM ingress, "
        "Actions, app, environment-tools template, and operator RBAC."
    ),
    output_dir=f"{HAND_WRITTEN_ROOT}/{STAGING_NAMESPACE}",
    image_pins=f"{HAND_WRITTEN_ROOT}/{STAGING_NAMESPACE}/image-pins",
    delivery_output_dir=f"{GENERATED_ROOT}/{STAGING_NAMESPACE}-delivery",
    extra_resources=(_WEB_PUSH_SECRET_FILE, "github-app.sops.yaml"),
)


# The verified caller fields LiteLLM records as each request's spend-log metadata, sent both as one JSON
# header and as W3C baggage under `litellm.metadata.spend_logs_metadata.`.
_SPEND_LOGS_FIELDS = {
    "agentplane.namespace": VerifiedClaim.NAMESPACE,
    "agentplane.service_account": VerifiedClaim.SERVICE_ACCOUNT,
    "agentplane.pod_name": VerifiedClaim.POD_NAME,
    "agentplane.pod_uid": VerifiedClaim.POD_UID,
    "agentplane.identity": VerifiedClaim.IDENTITY,
    "agentplane.identity_uid": VerifiedClaim.IDENTITY_UID,
}


def _anthropic_auto_compact_tokens(route: Route) -> int:
    limits = route.model.limits
    if not isinstance(limits, TokenLimits):
        raise ValueError(f"Missing Anthropic API token limits for {route.id}")
    # The direct API catalog records Anthropic's published context window as the input ceiling.
    return limits.max_input_tokens * 9 // 10


def environment(*, identity_catalog: bool) -> Environment:
    """Build staging with the selected policy catalog for both app and orchestration presets."""
    openai_compaction_tokens = codex_auto_compact_tokens(
        [route for route in STAGING_APP_MODELS.central_sampler if route.upstream.provider is Provider.CHATGPT]
    )
    app_origin = f"https://{_HOSTNAME}"
    oidc_issuer = f"{_AUTHENTIK}/application/o/agentplane-staging/"
    config = staging_config.config(
        namespace=STAGING_NAMESPACE,
        sandbox_template=POD_TEMPLATE,
        firecracker_template=environment_firecracker.NAME,
        runtime=_RUNTIME_CATALOG,
        action_federation=_ACTION_FEDERATION,
        connection_service_url=AnyHttpUrl(f"https://{_CONNECTION_SERVICE.authority}"),
        app_origin=app_origin,
        oidc_issuer=oidc_issuer,
        electric_url=AnyHttpUrl(electric.service(STAGING_NAMESPACE).url),
        identity_catalog=identity_catalog,
    )
    return Environment(
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
        runtime_catalog=_RUNTIME_CATALOG,
        hostname_spread_services=frozenset(
            {
                "actions",
                "app",
                "environments",
                "frontend",
                "history-service",
                "llm-ingress",
                "mcp-edge",
                "notifications",
                "policy-controller",
                "sessions",
            }
        ),
        identity_catalog=identity_catalog,
        central_sampler=CentralSamplerProps(
            context_source_writes=True,
            event_block_writes=True,
            actions=ActionsConfig(
                target=actions.service(STAGING_NAMESPACE).authority,
                tls=TLS(
                    cert_file=workload_tls.CERT_FILE,
                    key_file=workload_tls.KEY_FILE,
                    ca_file=workload_tls.TRUST_BUNDLE_FILE,
                ),
            ),
            models={
                route.id: (
                    ModelConfig(
                        connection="anthropic-api",
                        provider_model=route.model.id,
                        api="anthropic_messages",
                        anthropic_messages=_ANTHROPIC_MESSAGES,
                        auto_compact_tokens=_anthropic_auto_compact_tokens(route),
                        exec_encoding="json",
                        reasoning_efforts=set(route.reasoning_efforts),
                    )
                    if route.upstream.provider == Provider.ANTHROPIC_API
                    else ModelConfig(
                        connection="llm-ingress",
                        provider_model=route.id,
                        response_header_timeout_seconds=360 if route == TABBYAPI_QWEN_STOCK else None,
                        api="responses",
                        auto_compact_tokens=openai_compaction_tokens.get(route),
                        responses=(
                            _LITE_RESPONSES if route.upstream.provider == Provider.CHATGPT else _STANDARD_RESPONSES
                        ),
                        exec_encoding="freeform" if route.upstream.provider == Provider.CHATGPT else "json",
                        reasoning_efforts={"", *route.reasoning_efforts},
                    )
                )
                for route in STAGING_APP_MODELS.central_sampler
            }
            | {
                chat_completions_id(route, STAGING_APP_MODELS.central_sampler): ModelConfig(
                    connection="llm-ingress",
                    provider_model=route.id,
                    response_header_timeout_seconds=360
                    if route in (TABBYAPI_QWEN_STOCK, TABBYAPI_QWEN_ABLITERATED)
                    else None,
                    api="chat_completions",
                    exec_encoding="json",
                    reasoning_efforts={"", *route.reasoning_efforts},
                    auto_compact_tokens=192 * 1024,
                    chat_compaction=ChatCompactionConfig(
                        max_tokens=8_192,
                        keep_recent_turns=2,
                        summary_prompt_template=(
                            "Write a handoff for the assistant continuing this conversation. "
                            "Summarize the conversation preceding this request, including any earlier handoff.\n\n"
                            "The continuation will receive the same system instructions and tool definitions again. "
                            "Omit restatements of them. Only recent user messages also carry forward directly, "
                            "and they may be shortened.\n\n"
                            "Preserve the user's current goals and constraints, decisions, completed and pending work, "
                            "relevant tool results, exact identifiers needed to resume, and unresolved questions or "
                            "uncertainties. Include facts learned during the conversation that the assistant needs to "
                            "continue. Preserve exact wording when it is needed to continue. Treat the preceding "
                            "messages as source material. Return only the handoff."
                        ),
                        replacement=ChatCompactionReplacement(
                            role="assistant", template="Summary of earlier conversation:\n{{.Summary}}"
                        ),
                    ),
                )
                for route in STAGING_APP_MODELS.central_sampler_chat
            },
            connections={
                "llm-ingress": ConnectionConfig(
                    base_url=f"https://{llm_ingress.service(STAGING_NAMESPACE).authority}",
                    auth=AuthConfig(type="agentplane_ingress"),
                    tls=workload_tls.service_client_tls(LLM_INGRESS_URI, client_uri=CENTRAL_SAMPLER_URI),
                ),
                "anthropic-api": ConnectionConfig(
                    base_url="https://api.anthropic.com",
                    auth=AuthConfig(
                        type="anthropic_api_key",
                        credential_file=Path("/var/run/secrets/central-sampler-anthropic/api-key"),
                    ),
                ),
            },
            connection_credentials={
                "anthropic-api": SecretRef(namespace=STAGING_NAMESPACE, name=_ANTHROPIC_API_KEY_SECRET).key(
                    ANTHROPIC_API_KEY.key
                )
            },
            max_live_sessions=32,
            rpc_timeout_seconds=30,
            lease_seconds=60,
        ),
        settle_deltas=True,
        compact_deltas=True,
        compact_execution_evidence=True,
        notifications_debounce=NoticeDebounceSettings(quiet_seconds=60, max_wait_seconds=120),
        notifications_github=GitHubAppProps(app_id=5188971, secret_name="agentplane-github-app"),
        kubernetes_binding_cleanup_namespaces=frozenset(agent_access_profiles.cleanup_namespaces()),
        llm_ingress=LlmIngressProps(
            litellm_key_secret_name=_LITELLM_KEY_SECRET,
            models=model_configs(STAGING_APP_MODELS),
            codex_routes=STAGING_APP_MODELS.codex,
            attribution=AttributionConfig(
                json_headers=[
                    AttributionJsonHeader(
                        name="x-litellm-spend-logs-metadata",
                        fields=[AttributionField(key=key, source=source) for key, source in _SPEND_LOGS_FIELDS.items()],
                    )
                ],
                baggage=[
                    AttributionField(key=f"litellm.metadata.spend_logs_metadata.{key}", source=source)
                    for key, source in _SPEND_LOGS_FIELDS.items()
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
            external_workload_namespaces=(public_coder_egress.NAMESPACE, haku_namespace.NAMESPACE, runner.NAMESPACE),
            standing_workload_namespaces=(haku_namespace.NAMESPACE, runner.NAMESPACE),
        ),
        app=AppProps(
            config=config,
            origin=app_origin,
            oidc_issuer=oidc_issuer,
            reach_incluster_authentik=True,
            oidc_session_secret_name=_OIDC_SESSION_SECRET,
        ),
        actions=ActionsProps(
            central_sampler_session_delegation=True,
            hostname=_ACTIONS_HOSTNAME,
            operator_oidc=_FEDERATION_TARGET,
            caller_service_account_namespaces=frozenset({STAGING_NAMESPACE, haku_namespace.NAMESPACE}),
            mcp_servers=_MCP_SERVERS,
            action_groups=_ACTION_GROUPS,
            mcp_client_metadata=McpClientMetadataSettings(
                url=AnyHttpUrl(_MCP_CLIENT_METADATA_URL), client_name="Agentplane staging"
            ),
            web_push=WebPushProps(
                subject="mailto:agentydragon@gmail.com",
                public_base_url=app_origin,
                allowed_push_hosts=_WEB_PUSH_ALLOWED_HOSTS,
                secret_name=_WEB_PUSH_SECRET,
            ),
            oauth_secret_items=(),
            github_mcp_client_secret_name=_GITHUB_MCP_CLIENT_SECRET,
            bearer_mcp_mounts=[_SSH_MCP_MOUNT, _HA_MCP_MOUNT, _TANA_MCP_MOUNT, _GOOGLE_MCP_MOUNT],
            extra_egress=[
                EgressRule.to_fqdns(*_WEB_PUSH_ALLOWED_HOSTS),
                ssh_mcp.SERVICE.egress(),
                ha_mcp.FACADE.egress(),
                tana_mcp.MCP_PROXY.egress(),
                google_mcp.SERVICE.egress(),
                # Same public-origin Gateway path as the BFF: only Authentik SNI on node:443. The
                # resolver fetches /application/o/agentplane-staging-actions/jwks/ over HTTPS.
                cilium.egress_via_gateway("auth.allegedly.works"),
                # GitHub MCP discovery advertises github.com as its OAuth authorization server.
                EgressRule.to_fqdns("api.githubcopilot.com", "github.com"),
                # Claude and Codex publish their public CIMD documents at these origins.
                EgressRule.to_fqdns("claude.ai", "chatgpt.com"),
                # `github_public_repository` policies confirm a repository is public with an
                # unauthenticated Go GitHub REST lookup (agentplane/action_service/internal/admission/policies/visibility.go); no credential
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
                presets=app_settings.orchestration_presets(
                    sandbox_presets=config.sandbox_presets, thread_presets=config.thread_presets
                ),
                direct_tools=frozenset(
                    {
                        "list_launch_presets",
                        "create_session",
                        "submit_input",
                        "interrupt_turn",
                        "change_model",
                        "change_reasoning_effort",
                        "get_session",
                        "list_sessions",
                        "read_turns",
                        "wait_for_turn",
                    }
                ),
            ),
            session_delegation=SessionDelegationProps(
                direct_tools_by_group={
                    "environments": frozenset({"list", "get", "resume"}),
                    "sessions": frozenset(
                        {
                            "create",
                            "submit_input",
                            "observe_input",
                            "get",
                            "read_turns",
                            "cancel_input",
                            "interrupt_turn",
                        }
                    ),
                }
            ),
        ),
        mcp_edge=McpEdgeProps(
            operator_oidc=_FEDERATION_TARGET,
            oauth_secret_name="agentplane-mcp-oauth",
            hostname=_ACTIONS_HOSTNAME,
            extra_egress=(cilium.egress_via_gateway("auth.allegedly.works"), EgressRule.to_fqdns("claude.ai")),
        ),
        vm_sandboxes=True,
        firecracker_sandboxes=True,
        vm_storage_class=piraeus_storage.SSD_REPLICATED,
        vm_resources=STAGING_VM_RESOURCES,
        vm_executor=Executor(url="ws://127.0.0.1:4501", owner=SystemdExecutorOwner(), placement="systemd"),
    )


# CLEANUP(added 2026-11-09): switch staging to the identity catalog at cutover, then remove
# the ServiceAccount-subject rendering and this switch.
ENV = environment(identity_catalog=False)


_ASSIGNABLE_POLICY_SETS = {
    **agent_access_profiles.policy_set_grants(),
    TOOL_CONFIG_GRANT: (tool_config_grant(STAGING_NAMESPACE),),
}

# Both the controller's own bind permissions and its delegation in other namespaces
# cover the preset role sets and the standing Claude account's additional static roles.
ASSIGNABLE_GRANTS: tuple[KubernetesGrant, ...] = (
    *(grant for grants in _ASSIGNABLE_POLICY_SETS.values() for grant in grants),
    *agent_access_profiles.CLAUDE_AI_STATIC_GRANTS,
)


def chart(app: App, env: Environment = ENV) -> Chart:
    chart = environment_chart(
        app,
        env,
        db=DbProps(
            instances=2, wal_archive=True, profile=cluster.OVH_PIRAEUS_SSD, size="96Gi", resize_in_use_volumes=True
        ),
        environment_template_name=POD_TEMPLATE,
        environment_tools_image=_ENVIRONMENT_TOOLS_IMAGE,
        environment_runtime_key_files=(_BUILDBUDDY_KEY,),
    )
    EnvironmentSandboxTemplate(
        chart,
        "runner-template-ssd",
        env,
        name=SSD_POD_TEMPLATE,
        image=_ENVIRONMENT_TOOLS_IMAGE,
        key_files=(_BUILDBUDDY_KEY,),
        storage_class=piraeus_storage.SSD_LOCAL,
    )
    # Cluster-scoped and covering every Agentplane namespace; staging is the only environment.
    workload_certificate_admission.add_workload_certificate_admission(
        chart,
        workload_credential_issuers={ServiceAccountRef(namespace=env.namespace, name=policy_controller.NAME)},
        service_requester="system:serviceaccount:flux-system:kustomize-controller",
        cert_manager=ServiceAccountRef(namespace="cert-manager", name="cert-manager"),
    )
    NetworkPolicy(
        chart,
        "networkpolicy-app-to-mcp-edge",
        metadata=ApiObjectMetadata(name="app-to-mcp-edge", namespace=env.namespace),
        endpoint_selector=app_component.service(env.namespace).pods.selector,
        egress=[_CONNECTION_SERVICE.egress()],
    )
    haku_ui_egress.add_gateway_resources(chart, namespace=STAGING_NAMESPACE)
    haku_ui = haku_ui_egress.add_workload_credentials(chart, policy_namespace=env.namespace)
    haku_ci = egress_gateway.add_gateway(chart, env, clients=runner.RUNNER_PODS)
    # Reuse the canonical key via ESO; do not grant the sandbox ServiceAccount get-secrets RBAC. The
    # environment-tools template projects `_BUILDBUDDY_KEY` into its workload containers only.
    credential_external_secret(
        chart,
        namespace=STAGING_NAMESPACE,
        target=_BUILDBUDDY_KEY.secret_name,
        source="buildbuddy-api-key",
        key=_BUILDBUDDY_KEY.file_name,
        store=EXTERNAL_CREDS_STORE,
    )
    notification_service = notifications.webhook_service(STAGING_NAMESPACE)
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
        chart,
        "command-sandbox",
        env,
        runner_zone=node_scheduling.HIL_OVH_ZONE,
        image_pull_secret_name=SANDBOX_SECRET_NAME,
    ).add_platform_resources()
    # Compose staging's complete PolicySets from the shared grant definitions and its own egress
    # rules before materializing them. The controller workload and its RBAC are separate constructs.
    role_sets: dict[str, KubernetesRoleSet] = {}
    for name, grants in sorted(_ASSIGNABLE_POLICY_SETS.items()):
        # Sandboxes and standing callers act as Identities and reach the API server only as accounts,
        # which the role sets grant. Mixed-mode PolicySets keep their Kubernetes rules for any
        # remaining ServiceAccount-subject bindings; Identity bindings use pure-egress siblings.
        role_sets[name] = KubernetesRoleSet(
            chart,
            f"kubernetes-role-set-{name}",
            metadata=ApiObjectMetadata(name=name, namespace=env.namespace),
            spec=KubernetesRoleSetSpec(rules=agent_access_profiles.role_set_rules_for_grants(grants)),
        )
        if not env.identity_catalog:
            PolicySet(
                chart,
                f"policy-set-{name}",
                metadata=ApiObjectMetadata(name=name, namespace=env.namespace),
                spec=PolicySetSpec(
                    rules=[*agent_access_profiles.rules_for_grants(grants), *ASSIGNABLE_EGRESS_RULES.get(name, ())]
                ),
            )
        if name in ASSIGNABLE_EGRESS_RULES:
            policy_name = identity_egress_set_name(name, identity_catalog=env.identity_catalog)
            PolicySet(
                chart,
                f"policy-set-{policy_name}",
                metadata=ApiObjectMetadata(name=policy_name, namespace=env.namespace),
                spec=PolicySetSpec(rules=ASSIGNABLE_EGRESS_RULES[name]),
            )
    role_sets[agent_access_profiles.CLAUDE_AI_STATIC_ROLE_SET] = KubernetesRoleSet(
        chart,
        "kubernetes-role-set-claude-ai-static",
        metadata=ApiObjectMetadata(name=agent_access_profiles.CLAUDE_AI_STATIC_ROLE_SET, namespace=env.namespace),
        spec=KubernetesRoleSetSpec(
            rules=agent_access_profiles.role_set_rules_for_grants(agent_access_profiles.CLAUDE_AI_STATIC_GRANTS)
        ),
    )
    _add_launch_accounts(chart, env, role_sets=role_sets)
    _add_standing_identity_access(chart, env, role_sets=role_sets)
    for group in staging_config.INBOX_GROUPS:
        PolicySet(
            chart,
            f"policy-set-{staging_config.send_to(group)}",
            metadata=ApiObjectMetadata(name=staging_config.send_to(group), namespace=env.namespace),
            spec=PolicySetSpec(rules=[Rule.send_message(inbox_tags=[group])]),
        )
    # The issuer serves Sandboxes' own namespace and every namespace holding standing records.
    workload_namespaces = {env.namespace, *env.egress.standing_workload_namespaces}
    controller = policy_controller.PolicyController(
        chart,
        "policy-controller",
        env,
        accounts_namespace=kubernetes_accounts_namespace(env.namespace),
        workload_namespaces=workload_namespaces,
        placement=node_scheduling.HIL_OVH,
        registry_secret_name=REGISTRY_SECRET_NAME,
    )
    controller.add_platform_resources()
    policy_controller_rbac.PolicyControllerRbac(
        chart,
        "policy-controller-rbac",
        env,
        account=controller.account,
        assignable_grants=ASSIGNABLE_GRANTS,
        accounts_namespace=kubernetes_accounts_namespace(env.namespace),
        workload_namespaces=workload_namespaces,
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
            )
        ),
        data=[remote_data(_LITELLM_KEY_SECRET, "api-key")],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
    )
    ExternalSecret(
        chart,
        "anthropic-api-key-external-secret",
        metadata=ApiObjectMetadata(
            name=_ANTHROPIC_API_KEY_SECRET,
            namespace=STAGING_NAMESPACE,
            annotations={
                "description": "ESO copy of the canonical Anthropic API key from litellm for the central sampler."
            },
        ),
        refresh_interval="1h",
        secret_store_ref=SecretStoreRef.cluster(
            single_secret_store(
                chart,
                "agentplane-staging-anthropic-api-key",
                reader=reader,
                source_namespace=ANTHROPIC_API_KEY.secret.namespace,
                source_secret=ANTHROPIC_API_KEY.secret.name,
            )
        ),
        data=[remote_data(ANTHROPIC_API_KEY.secret.name, ANTHROPIC_API_KEY.key, secret_key=ANTHROPIC_API_KEY.key)],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
    )
    ExternalSecret(
        chart,
        "tana-pat-external-secret",
        metadata=ApiObjectMetadata(
            name=_TANA_MCP_MOUNT.secret_name,
            namespace=STAGING_NAMESPACE,
            annotations={"description": "ESO copy of the canonical Tana PAT from external-creds."},
        ),
        refresh_interval="1h",
        secret_store_ref=external_creds.STORE,
        data=[remote_data(external_creds.TANA_PAT.secret.name, external_creds.TANA_PAT.key)],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
    )
    # Each store grants access to just the backend's minted bearer in its source namespace.
    for mount, source in (
        (_SSH_MCP_MOUNT, ssh_mcp.BEARER),
        (_GOOGLE_MCP_MOUNT, google_mcp.BEARER),
        (_HA_MCP_MOUNT, ha_mcp.BEARER),
    ):
        credential_external_secret(
            chart,
            namespace=STAGING_NAMESPACE,
            target=mount.secret_name,
            source=source.secret.name,
            key=source.key,
            store=single_secret_store(
                chart,
                f"agentplane-staging-{mount.name}-bearer",
                reader=reader,
                source_namespace=source.secret.namespace,
                source_secret=source.secret.name,
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
            )
        ),
        data=[remote_data(_GITHUB_MCP_CLIENT_SECRET, key) for key in ("client_id", "client_secret")],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
    )
    _add_session_secret(chart)
    add_staging_action_policies(chart, identity_catalog=env.identity_catalog, haku_ui=haku_ui, haku_ci=haku_ci)
    EgressCredentials(
        chart, "egress-credentials", namespace=env.egress.credentials_namespace, proxy_namespace=env.namespace
    )
    KubernetesAccounts(
        chart,
        "kubernetes-accounts",
        namespace=kubernetes_accounts_namespace(env.namespace),
        proxy_namespace=env.namespace,
    )
    add_staging_egress_credentials(
        chart, namespace=env.namespace, credentials_namespace=env.egress.credentials_namespace
    )
    return chart


def _add_launch_accounts(chart: Chart, env: Environment, *, role_sets: Mapping[str, KubernetesRoleSet]) -> None:
    """The accounts a launched Sandbox's Identity acts as at the API server: the shared diagnostics
    account every launch holds, and one per preset that grants Kubernetes roles (operator,
    2026-11-09), so each agent has one kubectl identity."""
    KubernetesAccount(
        chart,
        "kubernetes-account-shared-diagnostics",
        account=shared_diagnostics(env.namespace),
        policy_namespace=env.namespace,
        description="Read-only namespace diagnostics every agent may use.",
        role_sets=[role_sets[agent_access_profiles.SHARED_DIAGNOSTICS_POLICY]],
    )
    for preset, selected_sets in agent_access_profiles.PRESET_ROLE_SETS.items():
        KubernetesAccount(
            chart,
            f"kubernetes-account-{preset}",
            account=kubernetes_account_ref.account(env.namespace, preset),
            policy_namespace=env.namespace,
            description=f"The '{preset}' preset's Kubernetes identity.",
            role_sets=[role_sets[role_set] for role_set in selected_sets],
        )


def _add_standing_identity_access(
    chart: Chart, env: Environment, *, role_sets: Mapping[str, KubernetesRoleSet]
) -> None:
    """The standing Action callers' Identities and their exact Kubernetes accounts and grants."""
    for caller, description in (
        (CLAUDE_AI, "The agent behind Connections enrolled from the Claude.ai MCP connector."),
        (HAKU_AGENT, "Agents of the 'haku' preset, which clone haku-state and work from it the way Haku itself does."),
        (
            public_coder_egress.OPENCLAW,
            "The backend principal for the paused public-coder OpenClaw workload; rendering it does not start that workload.",
        ),
    ):
        Identity(
            chart,
            f"identity-{caller.identity}",
            metadata=ApiObjectMetadata(name=caller.identity, namespace=env.namespace),
            description=description,
        )
    # Each standing caller with roles of its own has its own account.
    accounts = [
        (
            caller.identity,
            f"The {caller.identity} agent's own Kubernetes identity.",
            [*caller_role_sets(), *([agent_access_profiles.CLAUDE_AI_STATIC_ROLE_SET] if caller == CLAUDE_AI else [])],
        )
        for caller in (CLAUDE_AI, HAKU_AGENT)
    ]
    for name, description, names in accounts:
        KubernetesAccount(
            chart,
            f"kubernetes-account-{name}",
            account=kubernetes_account_ref.account(env.namespace, name),
            policy_namespace=env.namespace,
            description=description,
            role_sets=[role_sets[role_set] for role_set in names],
        )


def delivery_chart(app: App, env: Environment = ENV) -> Chart:
    return runner_gateway_workload.serving_chart(app, env=env, wake_service_callers=DISPATCHER_WAKE_SERVICE_CALLERS)


def _add_session_secret(scope: Chart) -> None:
    """Generate the staging app's local session-signing key with ESO.

    Rotating this value invalidates existing browser sessions, but does not touch the
    Authentik OAuth client credentials.
    """
    mint_bearer_secret(
        scope,
        "session-external-secret",
        target=SecretRef(namespace=STAGING_NAMESPACE, name=_OIDC_SESSION_SECRET).key("session-secret"),
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
    cert_manager_environment: Kustomization,
    cnpg: Kustomization,
    external_secrets_operator: Kustomization,
    kyverno: Kustomization,
    monitoring_crds: Kustomization,
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
            *(
                KustomizationSpecHealthChecks(
                    api_version="external-secrets.io/v1", kind="ExternalSecret", name=name, namespace=namespace
                )
                for namespace, name in (
                    (ENV.egress.credentials_namespace, public_coder_egress.CLICKHOUSE_CREDENTIAL),
                    (ENV.egress.credentials_namespace, public_coder_egress.MATRIX_CREDENTIAL),
                    (ENV.egress.credentials_namespace, public_coder_egress.BRAVE_CREDENTIAL),
                    (STAGING_NAMESPACE, _LITELLM_KEY_SECRET),
                    (STAGING_NAMESPACE, _ANTHROPIC_API_KEY_SECRET),
                    (STAGING_NAMESPACE, _OIDC_SESSION_SECRET),
                )
            ),
        ],
        health_check_exprs=[database_health()],
        decryption=sops_decryption(LAYOUT.extra_resources),
        depends_on=flux_kustomization_depends_on_many(
            agentplane_crds,
            cert_manager_trust,
            cert_manager_environment,
            cnpg,
            external_secrets_operator,
            kyverno,
            monitoring_crds,
            crossplane_keys,
        ),
    )
