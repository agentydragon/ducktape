"""agentplane-staging: two replicas of everything, operator login federated through the
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
from agentplane.action_service.sandbox.binding import SandboxExecutorBinding
from agentplane.action_service.settings import ActionServiceDeploymentSettings, WebPushDeploymentSettings
from agentplane.app.action_federation_settings import ExchangeFederationSettings
from cluster.cdk8s import cilium, external_creds, ha_mcp, node_scheduling
from cluster.cdk8s.agentplane import actions, command_sandbox, notifications, staging_config
from cluster.cdk8s.agentplane.actions_staging_policies import add_staging_action_policies
from cluster.cdk8s.agentplane.app import RunnerTemplate
from cluster.cdk8s.agentplane.chart import environment_chart
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
    DbProps,
    EgressProps,
    Environment,
    GitHubAppProps,
    HistoryWriter,
    LlmIngressProps,
    ReplicaProfile,
)
from cluster.cdk8s.agentplane.grpc_channel_config import LARGE_EVENT_GRPC_CHANNEL_OPTIONS
from cluster.cdk8s.agentplane.llm_ingress import model_configs
from cluster.cdk8s.agentplane.namespaces import STAGING_NAMESPACE
from cluster.cdk8s.external_secrets.minted_secret import mint_bearer_secret
from cluster.cdk8s.external_secrets.single_secret_store import single_secret_store
from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.generation import CNPG_DATABASE_READY, sops_decryption
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.model_selections import STAGING_APP_MODELS
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
_GROCY_SF_MCP_URL = "https://grocy-mcp-sf.allegedly.works/mcp"
_MCP_CLIENT_METADATA_URL = f"https://{_ACTIONS_HOSTNAME}/oauth/client-metadata.json"
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
_ACTIONS_SETTINGS = ActionServiceDeploymentSettings(
    operator_oidc=_FEDERATION_TARGET,
    policy_namespace=STAGING_NAMESPACE,
    caller_service_account_namespaces=frozenset({STAGING_NAMESPACE, public_coder_egress.NAMESPACE}),
    direct_wait_seconds=30,
    max_wait_seconds=180,
    web_push=WebPushDeploymentSettings(
        subject="mailto:agentydragon@gmail.com",
        public_base_url=f"https://{_HOSTNAME}",
        allowed_push_hosts=list(_WEB_PUSH_ALLOWED_HOSTS),
    ),
    mcp_client_metadata=McpClientMetadataSettings(
        url=AnyHttpUrl(_MCP_CLIENT_METADATA_URL), client_name="Agentplane staging"
    ),
    mcp_servers={
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
    },
    action_groups={
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
                    # header the server never advertises them. Ported from haku-console's
                    # now-removed GitHub MCP wiring (cluster/cdk8s/haku/console_config.py,
                    # dropped in #7773), which configured this the same way.
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
                    "url": _KUBERNETES_MCP_URL,
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
                config={
                    "transport": "streamable-http",
                    "url": _GROCY_SF_MCP_URL,
                    "server_id": "grocy_sf",
                    "auth": "oauth",
                },
            ),
        ),
        "sandbox": ActionGroup(
            title="Sandbox",
            description=(
                "Sandboxes that run as the calling ServiceAccount, and bounded commands in them. A "
                "sandbox reaches what its caller's EgressBindings allow and is admitted back to this "
                "service as that same caller, so it confers no authority the caller did not hold."
            ),
            executor=SandboxExecutorBinding(
                kind="sandbox",
                description="Stamped and exec'd by this service, as the caller, in its own namespace.",
                namespace=STAGING_NAMESPACE,
                # Each describes itself in the annotation the sandbox Actions read. The integration app's
                # runner template is offered for a caller that wants the harnesses or a state volume
                # that survives its Pod.
                templates={command_sandbox.NAME, command_sandbox.BUILD_NAME, "runner", "runner-ducktape"},
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
    },
)

ENV = Environment(
    namespace=STAGING_NAMESPACE,
    description=(
        "Agentplane staging - sandboxed runner Pods (one per Sandbox) and the integration app that drives them."
    ),
    flux_description=(
        "Complete Agentplane staging environment, including namespace, database, egress, LLM ingress, "
        "Actions, app, runner template, and operator RBAC."
    ),
    output_dir=f"{HAND_WRITTEN_ROOT}/{STAGING_NAMESPACE}",
    image_pins=f"{HAND_WRITTEN_ROOT}/{STAGING_NAMESPACE}/image-pins",
    extra_resources=(_WEB_PUSH_SECRET_FILE, "github-app.sops.yaml"),
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
        models=STAGING_APP_MODELS,
        action_federation=_ACTION_FEDERATION,
        sandbox_service_grpc_channel_options=LARGE_EVENT_GRPC_CHANNEL_OPTIONS,
        history_service_grpc_channel_options=LARGE_EVENT_GRPC_CHANNEL_OPTIONS,
    ),
    runner_grpc_channel_options=LARGE_EVENT_GRPC_CHANNEL_OPTIONS,
    history_writer=HistoryWriter.SANDBOX_SERVICE,
    notifications_github=GitHubAppProps(app_id=5188971, secret_name="agentplane-github-app"),
    db=DbProps(instances=2),
    llm_ingress=LlmIngressProps(
        litellm_key_secret_name=_LITELLM_KEY_SECRET, models=model_configs(STAGING_APP_MODELS), log_llm_requests=True
    ),
    egress=EgressProps(
        ca_secret_name=public_coder_egress.CA_BUNDLE_NAME,
        credentials_namespace=STAGING_CREDENTIALS_NAMESPACE,
        external_workload_namespaces=(public_coder_egress.NAMESPACE,),
    ),
    app=AppProps(
        hostname=_HOSTNAME,
        oidc_issuer=f"{_AUTHENTIK}/application/o/agentplane-staging/",
        reach_incluster_authentik=True,
        runner_zone=node_scheduling.HIL_OVH_ZONE,
        oidc_session_secret_name=_OIDC_SESSION_SECRET,
    ),
    actions=ActionsProps(
        hostname=_ACTIONS_HOSTNAME,
        settings=_ACTIONS_SETTINGS,
        extra_reload_secrets=(
            _GITHUB_MCP_CLIENT_SECRET,
            _WEB_PUSH_SECRET,
            _SSH_MCP_BEARER_SECRET,
            _HA_MCP_BEARER_SECRET,
            _TANA_MCP_BEARER_SECRET,
            _GOOGLE_MCP_BEARER_SECRET,
        ),
        # The full OAuth linkage triad; testing mounts only the one MCP client's secret.
        oauth_secret_items=("client-secret", "jwt-signing-key", "encryption-key"),
        web_push_secret_name=_WEB_PUSH_SECRET,
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
            # `github_public_repository` policies confirm a repository is public with an
            # unauthenticated GitHub REST call (agentplane/action_service/github_policy/visibility.py); no credential
            # rides this path.
            EgressRule.to_fqdns("api.github.com"),
            # The Kubernetes MCP server uses the public Gateway/remote-node path.
            cilium.egress_via_gateway("kubectl-passthrough-mcp.allegedly.works"),
            # Grocy SF's MCP server (OAuth discovery, DCR, and the linked /mcp calls) is the
            # same public Gateway path.
            cilium.egress_via_gateway("grocy-mcp-sf.allegedly.works"),
            EgressRule.to_endpoints(cilium.AUTHENTIK_SERVER_LABELS, 9000, server_names=["auth.allegedly.works"]),
        ],
    ),
)


def chart(app: App) -> Chart:
    chart = environment_chart(app, ENV)
    # Reuse the canonical key via ESO; do not grant the sandbox ServiceAccount get-secrets RBAC.
    credential_external_secret(
        chart,
        namespace=STAGING_NAMESPACE,
        target="buildbuddy-api-key",
        source="buildbuddy-api-key",
        key="api-key",
        store=EXTERNAL_CREDS_STORE,
    )
    RunnerTemplate(
        chart,
        "ducktape-runner-template",
        ENV,
        name="runner-ducktape",
        image="git.allegedly.works/ducktape-ci/runner-ducktape",
        buildbuddy_secret=True,
        extra_harness_env=("BBR_BUILDBUDDY_API_KEY_FILE",),
        description=(
            "Public ducktape development: the runner and harnesses plus the repository's shared "
            "bb/bbr, Bazelisk, pre-commit, formatters and Gazelle. Same container isolation and "
            "permissions as the generic runner except for a mounted BuildBuddy key; use remote execution for builds."
        ),
    )
    notification_service = notifications.service(STAGING_NAMESPACE)
    https_route(
        chart,
        "notifications-github-webhook-route",
        metadata=ApiObjectMetadata(name="agentplane-notifications-github-webhook", namespace=STAGING_NAMESPACE),
        hostnames=[_NOTIFICATIONS_HOSTNAME],
        backend=notification_service,
        paths=["/v1/webhooks/github"],
    )
    # Add to the workload-only policy without opening the testing environment to the Gateway.
    NetworkPolicy(
        chart,
        "notifications-github-webhook-policy",
        metadata=ApiObjectMetadata(name="agentplane-notifications-github-webhook", namespace=STAGING_NAMESPACE),
        endpoint_selector=notification_service.pods.selector,
        ingress=[IngressRule.from_gateway(notification_service.pod_port)],
    )
    command_sandbox.CommandSandbox(chart, "command-sandbox", ENV)
    reader = ServiceAccount(
        chart,
        "external-creds-reader",
        metadata=ApiObjectMetadata(name="external-creds-reader", namespace=STAGING_NAMESPACE),
        automount_token=False,
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
    Authentik OAuth client credentials or the Agentplane testing environment.
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
                    public_coder_egress.HAKU_CREDENTIAL,
                    public_coder_egress.CLICKHOUSE_CREDENTIAL,
                    public_coder_egress.MATRIX_CREDENTIAL,
                    public_coder_egress.BRAVE_CREDENTIAL,
                )
            ],
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
        decryption=sops_decryption(ENV.extra_resources),
        depends_on=flux_kustomization_depends_on_many(
            agentplane_crds, agent_sandbox_controller, cert_manager_trust, cnpg, external_secrets_operator
        ),
    )
