"""agentplane-staging: two replicas of everything, operator login federated through the
shared Authentik, and the reviewed GitHub/Kubernetes/Grocy SF/SSH/Home Assistant/Tana/Gmail/
Google Calendar MCP action groups.
"""

from __future__ import annotations

from cdk8s import App, Chart, Duration
from cdk8s_plus_34 import DeploymentStrategy, PercentOrAbsolute, ServiceAccount
from eso_password_generator_crds.io.external_secrets.generators import Password, PasswordSpec
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecRefreshPolicy,
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
    ExternalSecretSpecTargetTemplate,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    Kustomization,
    KustomizationSpecDeletionPolicy,
    KustomizationSpecHealthCheckExprs,
    KustomizationSpecHealthChecks,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s import cilium, external_creds
from cluster.cdk8s.agentplane import actions, command_sandbox, staging_config
from cluster.cdk8s.agentplane.actions_staging_policies import add_staging_action_policies
from cluster.cdk8s.agentplane.chart import environment_chart
from cluster.cdk8s.agentplane.egress_credentials import STAGING_NAMESPACE, EgressCredentials, credential_external_secret
from cluster.cdk8s.agentplane.egress_staging_credentials import add_staging_egress_credentials
from cluster.cdk8s.agentplane.environment import (
    ActionsProps,
    AppProps,
    BearerMcpMount,
    DbProps,
    EgressProps,
    Environment,
    LlmIngressProps,
    ReplicaProfile,
)
from cluster.cdk8s.external_secrets.single_secret_store import single_secret_store
from cluster.cdk8s.flux import flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.generation import CNPG_DATABASE_READY, sops_decryption
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.providers.external_secrets.external_secret import (
    add_external_secret,
    cluster_secret_store,
    password_generator,
    remote_data,
)
from cluster.cdk8s.ssh_mcp.config import BEARER_SECRET_KEY, BEARER_SECRET_NAME, MCP_URL

_NAMESPACE = "agentplane-staging"
_HOSTNAME = "agentplane-staging.allegedly.works"
_AUTHENTIK = "https://auth.allegedly.works"
_ACTIONS_OIDC_APP = f"{_AUTHENTIK}/application/o/agentplane-staging-actions"
# The push services web-push subscriptions may target: both the Action Service's own
# allowlist and its egress rule, so the policy cannot drift from what the app accepts.
_WEB_PUSH_ALLOWED_HOSTS = ("fcm.googleapis.com", "updates.push.services.mozilla.com")
_GITHUB_MCP_URL = "https://api.githubcopilot.com/mcp/"
_KUBERNETES_MCP_URL = "https://kubectl-passthrough-mcp.allegedly.works/mcp"
_GROCY_SF_MCP_URL = "https://grocy-mcp-sf.allegedly.works/mcp"
# grocy-mcp-sf's OIDCProxy authorization server (mcp_infra/authentik_auth) only advertises
# `none`/`private_key_jwt` in `token_endpoint_auth_methods_supported` -- no client_secret_post
# or client_secret_basic -- so this is a public, PKCE-only client (RFC 7591 dynamic client
# registration against https://grocy-mcp-sf.allegedly.works/register, redirect_uri
# https://agentplane-staging.allegedly.works/mcp-linkage/callback), the same shape as
# `kubernetes_admin` below. No client secret exists to rotate or leak. If the registration is ever
# lost (e.g. the server's Valkey-backed client store is wiped), re-run the DCR POST and update
# this literal; nothing else changes.
_GROCY_SF_MCP_CLIENT_ID = "cb57e244-c13c-4eac-a299-e052698b774e"
_HOME_ASSISTANT_MCP_URL = "http://ha-mcp.ha-mcp.svc.cluster.local:8765/mcp"
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
_FEDERATION_TARGET = {
    "issuer": f"{_ACTIONS_OIDC_APP}/",
    "audience": "agentplane-staging-actions",
    "jwks_uri": f"{_ACTIONS_OIDC_APP}/jwks/",
}
_ACTION_FEDERATION = {
    "mode": "exchange",
    "service_url": f"http://agentplane-actions.{_NAMESPACE}.svc.cluster.local:{actions.CONTAINER_PORT}",
    "token_endpoint": f"{_AUTHENTIK}/application/o/token/",
    "login_jwks_uri": f"{_AUTHENTIK}/application/o/agentplane-staging/jwks/",
    "target": _FEDERATION_TARGET,
    "scope": "openid",
}
_ACTIONS_SETTINGS = {
    "operator_oidc": _FEDERATION_TARGET,
    "allowed_service_account_namespaces": [_NAMESPACE],
    "web_push": {
        "subject": "mailto:agentydragon@gmail.com",
        "public_base_url": f"https://{_HOSTNAME}",
        "allowed_push_hosts": list(_WEB_PUSH_ALLOWED_HOSTS),
    },
    "mcp_servers": {
        "github": {
            "server_id": "github",
            "server_url": _GITHUB_MCP_URL,
            "client_id": "configured-by-secret",
            "client_secret_file": "/etc/agentplane-github/client_secret",
            "redirect_uri": f"https://{_HOSTNAME}/mcp-linkage/callback",
        },
        "kubernetes_admin": {
            "server_id": "kubernetes_admin",
            "server_url": _KUBERNETES_MCP_URL,
            "client_id": "kubectl-passthrough-mcp",
            "redirect_uri": f"https://{_HOSTNAME}/mcp-linkage/callback",
        },
        "grocy_sf": {
            "server_id": "grocy_sf",
            "server_url": _GROCY_SF_MCP_URL,
            "client_id": _GROCY_SF_MCP_CLIENT_ID,
            "redirect_uri": f"https://{_HOSTNAME}/mcp-linkage/callback",
        },
    },
    "action_groups": {
        "github": {
            "title": "GitHub MCP",
            "description": "GitHub's operator-linked MCP tools; every Action remains subject to operator approval.",
            "executor": {
                "kind": "mcp",
                "description": "GitHub MCP executed with the linked operator GitHub account.",
                "config": {
                    "transport": "streamable-http",
                    "url": _GITHUB_MCP_URL,
                    "server_id": "github",
                    "auth": "oauth",
                },
            },
        },
        "kubernetes_admin": {
            "title": "Kubernetes admin",
            "description": (
                "The Kubernetes API with the linked operator's own permissions. Use it only for what your own "
                "Kubernetes identity cannot do; each call waits for the operator's approval."
            ),
            "executor": {
                "kind": "mcp",
                "description": "kubectl-passthrough-mcp, run as the linked operator's Kubernetes identity.",
                "config": {
                    "transport": "streamable-http",
                    "url": _KUBERNETES_MCP_URL,
                    "server_id": "kubernetes_admin",
                    "auth": "oauth",
                },
            },
        },
        "grocy_sf": {
            "title": "Grocy SF MCP",
            "description": "Grocy SF household MCP tools; every Action remains subject to operator approval.",
            "executor": {
                "kind": "mcp",
                "description": "Grocy SF MCP executed with the linked operator Grocy account.",
                "config": {
                    "transport": "streamable-http",
                    "url": _GROCY_SF_MCP_URL,
                    "server_id": "grocy_sf",
                    "auth": "oauth",
                },
            },
        },
        "sandbox": {
            "title": "Sandbox",
            "description": (
                "Sandboxes that run as the calling ServiceAccount, and bounded commands in them. A "
                "sandbox reaches what its caller's EgressBindings allow and is admitted back to this "
                "service as that same caller, so it confers no authority the caller did not hold."
            ),
            "executor": {
                "kind": "sandbox",
                "description": "Stamped and exec'd by this service, as the caller, in its own namespace.",
                "namespace": _NAMESPACE,
                "environments": {
                    "sandbox": {
                        "template": command_sandbox.NAME,
                        "container": command_sandbox.CONTAINER,
                        "default_cwd": command_sandbox.HOME,
                        "description": (
                            "A box to run commands in: bash and coreutils, git, curl, ripgrep, jq, openssl, "
                            "kubectl (configured as the caller's ServiceAccount) and python3 (install packages "
                            "into a `python3 -m venv`). 1 core and 2Gi, and no volume: files last as long as the "
                            "box's Pod."
                        ),
                    },
                    "build": {
                        "template": command_sandbox.BUILD_NAME,
                        "container": command_sandbox.CONTAINER,
                        "default_cwd": command_sandbox.HOME,
                        "description": (
                            "The sandbox box sized for a build: the same tools, 2 cores and 4Gi, and a home "
                            "directory that survives the container being killed for running out of memory, "
                            "though not the box's Pod."
                        ),
                    },
                    # The integration app's runner template, for a caller that wants the harnesses
                    # or a state volume that survives its Pod.
                    "runner": {
                        "template": "agentplane-runner",
                        "container": "runner",
                        "default_cwd": "/state",
                        "description": (
                            "The shared runner image, built to host an agent harness: the sandbox tools (git, curl, "
                            "ripgrep, jq, openssl, kubectl, python3) plus the runner, Claude Code and Codex."
                        ),
                    },
                },
                "default_environment": "sandbox",
            },
        },
        "ssh": {
            "title": "SSH",
            "description": "SSH commands on configured targets; every Action remains subject to operator approval.",
            "executor": {
                "kind": "mcp",
                "description": "SSH MCP backend (ssh-mcp); Agentplane retains approval and execution authority.",
                "config": {
                    "transport": "streamable-http",
                    "url": MCP_URL,
                    "auth": "static_bearer",
                    "bearer_file": "/run/secrets/ssh-mcp/bearer-token",
                },
            },
        },
        "home_assistant": {
            "title": "Home Assistant MCP",
            "description": "Home Assistant tools; every Action remains subject to operator approval.",
            "executor": {
                "kind": "mcp",
                "description": "Home Assistant MCP backend (ha-mcp).",
                "config": {
                    "transport": "streamable-http",
                    "url": _HOME_ASSISTANT_MCP_URL,
                    "auth": "static_bearer",
                    "bearer_file": "/run/secrets/ha-mcp/bearer-token",
                },
            },
        },
        "tana": {
            "title": "Tana MCP",
            "description": "Tana read/write tools; every Action remains subject to operator approval.",
            "executor": {
                "kind": "mcp",
                "description": "Tana MCP backend (tana-mcp).",
                "config": {
                    "transport": "streamable-http",
                    "url": _TANA_MCP_URL,
                    "auth": "static_bearer",
                    "bearer_file": "/run/secrets/tana-mcp/bearer-token",
                },
            },
        },
        "gmail": {
            "title": "Gmail",
            "description": "Gmail read/write tools; every Action remains subject to operator approval.",
            "executor": {
                "kind": "mcp",
                "description": "Gmail MCP backend (google-mcp), on a write-scoped Google credential.",
                "config": {
                    "transport": "streamable-http",
                    "url": _GMAIL_MCP_URL,
                    "auth": "static_bearer",
                    "bearer_file": "/run/secrets/google-mcp/bearer-token",
                },
            },
        },
        "google_calendar": {
            "title": "Google Calendar",
            "description": "Google Calendar read/write tools; every Action remains subject to operator approval.",
            "executor": {
                "kind": "mcp",
                "description": "Google Calendar MCP backend (google-mcp), on a write-scoped Google credential.",
                "config": {
                    "transport": "streamable-http",
                    "url": _CALENDAR_MCP_URL,
                    "auth": "static_bearer",
                    "bearer_file": "/run/secrets/google-mcp/bearer-token",
                },
            },
        },
    },
}

ENV = Environment(
    namespace=_NAMESPACE,
    description=(
        "Agentplane staging - sandboxed runner Pods (one per Sandbox) and the integration app that drives them."
    ),
    flux_description=(
        "Complete Agentplane staging environment, including namespace, database, egress, LLM ingress, "
        "Actions, app, runner template, and operator RBAC."
    ),
    extra_resources=(_WEB_PUSH_SECRET_FILE,),
    replicas=ReplicaProfile(
        count=2,
        strategy=DeploymentStrategy.rolling_update(
            max_surge=PercentOrAbsolute.absolute(1), max_unavailable=PercentOrAbsolute.absolute(0)
        ),
        min_ready=Duration.seconds(5),
        pdb_min_available=1,
    ),
    app_config={**staging_config.config(), "action_federation": _ACTION_FEDERATION},
    db=DbProps(instances=2),
    llm_ingress=LlmIngressProps(litellm_key_secret_name=_LITELLM_KEY_SECRET),
    egress=EgressProps(ca_secret_name="agentplane-egress-ca", credentials_namespace=STAGING_NAMESPACE),
    app=AppProps(
        hostname=_HOSTNAME,
        oidc_issuer=f"{_AUTHENTIK}/application/o/agentplane-staging/",
        reach_incluster_authentik=True,
        runner_zone="hil-ovh",
        oidc_session_secret_name=_OIDC_SESSION_SECRET,
    ),
    actions=ActionsProps(
        hostname="agentplane-actions-staging.allegedly.works",
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
            cilium.egress_to_fqdns(*_WEB_PUSH_ALLOWED_HOSTS),
            cilium.egress_to(cilium.endpoint_labels("ssh-mcp", "ssh-mcp"), 8080),
            cilium.egress_to(cilium.endpoint_labels("ha-mcp", "ha-mcp"), 8765),
            cilium.egress_to(cilium.endpoint_labels("tana-mcp", "tana-mcp"), 8263),
            cilium.egress_to(cilium.endpoint_labels("google-mcp", "google-mcp"), 8080),
            # Same public-origin Gateway path as the BFF: only Authentik SNI on node:443. The
            # resolver fetches /application/o/agentplane-staging-actions/jwks/ over HTTPS.
            cilium.egress_via_gateway("auth.allegedly.works"),
            # GitHub MCP discovery advertises github.com as its OAuth authorization server.
            cilium.egress_to_fqdns("api.githubcopilot.com", "github.com"),
            # `github_public_repository` policies confirm a repository is public with an
            # unauthenticated GitHub REST call (agentplane/action_service/github_policy/visibility.py); no credential
            # rides this path.
            cilium.egress_to_fqdns("api.github.com"),
            # The Kubernetes MCP server uses the public Gateway/remote-node path.
            cilium.egress_via_gateway("kubectl-passthrough-mcp.allegedly.works"),
            # Grocy SF's MCP server (OAuth discovery, DCR, and the linked /mcp calls) is the
            # same public Gateway path.
            cilium.egress_via_gateway("grocy-mcp-sf.allegedly.works"),
            cilium.egress_to(cilium.AUTHENTIK_SERVER_LABELS, 9000, server_names=["auth.allegedly.works"]),
        ],
    ),
)


def chart(app: App) -> Chart:
    chart = environment_chart(app, ENV)
    command_sandbox.CommandSandbox(chart, "command-sandbox", ENV)
    reader = ServiceAccount(
        chart, "external-creds-reader", metadata=metadata("external-creds-reader", _NAMESPACE), automount_token=False
    )
    add_external_secret(
        chart,
        "tana-pat-external-secret",
        name=_TANA_MCP_BEARER_SECRET,
        namespace=_NAMESPACE,
        refresh="1h",
        store=external_creds.STORE,
        data=[remote_data(_TANA_MCP_BEARER_SECRET, "token")],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
        annotations={"description": "ESO copy of the canonical Tana PAT from external-creds."},
    )
    for backend, target, source in (
        ("ssh-mcp", _SSH_MCP_BEARER_SECRET, BEARER_SECRET_NAME),
        ("google-mcp", _GOOGLE_MCP_BEARER_SECRET, _GOOGLE_MCP_BEARER_SECRET),
        ("ha-mcp", _HA_MCP_BEARER_SECRET, "ha-mcp-bearer"),
    ):
        credential_external_secret(
            chart,
            namespace=_NAMESPACE,
            target=target,
            source=source,
            key="bearer-token",
            store=single_secret_store(
                chart,
                f"agentplane-staging-{backend}-bearer",
                reader=reader,
                source_namespace=backend,
                source_secret=source,
                consumer_namespace=_NAMESPACE,
            ),
        )
    # The GitHub App's pre-registered OAuth client, whose SOPS source stays in haku-console
    # (cluster/k8s/haku/console/README.md): the id rides an env var, the secret a mounted file.
    add_external_secret(
        chart,
        "github-mcp-client-external-secret",
        name=_GITHUB_MCP_CLIENT_SECRET,
        namespace=_NAMESPACE,
        refresh="1h",
        store=cluster_secret_store(
            single_secret_store(
                chart,
                "agentplane-staging-github-mcp-client",
                reader=reader,
                source_namespace="haku-console",
                source_secret=_GITHUB_MCP_CLIENT_SECRET,
                consumer_namespace=_NAMESPACE,
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
    Password(
        scope,
        "session-password-generator",
        metadata=metadata(_OIDC_SESSION_SECRET, _NAMESPACE),
        spec=PasswordSpec(length=64, digits=16, symbols=0, no_upper=False, allow_repeat=True),
    )
    add_external_secret(
        scope,
        "session-external-secret",
        name=_OIDC_SESSION_SECRET,
        namespace=_NAMESPACE,
        refresh=ExternalSecretSpecRefreshPolicy.CREATED_ONCE,
        data_from=[password_generator(_OIDC_SESSION_SECRET)],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.ORPHAN,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
        template=ExternalSecretSpecTargetTemplate(type="Opaque", data={"session-secret": "{{ .password }}"}),
        immutable=True,
        annotations={"description": "ESO-generated Agentplane staging session-signing key."},
    )


def agentplane_staging(
    flux_chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    health_checks: list[KustomizationSpecHealthChecks],
    agentplane_crds: Kustomization,
    agent_sandbox_controller: Kustomization,
    cert_manager_environment: Kustomization,
    cert_manager_trust: Kustomization,
    claude_rbac: Kustomization,
    cnpg: Kustomization,
    external_creds: Kustomization,
    external_secrets_config: Kustomization,
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
            KustomizationSpecHealthChecks(
                api_version="external-secrets.io/v1",
                kind="ExternalSecret",
                name=_OIDC_SESSION_SECRET,
                namespace=_NAMESPACE,
            ),
        ],
        health_check_exprs=[
            KustomizationSpecHealthCheckExprs(
                api_version="postgresql.cnpg.io/v1", kind="Database", current=CNPG_DATABASE_READY
            )
        ],
        decryption=sops_decryption(ENV.extra_resources),
        depends_on=flux_kustomization_depends_on_many(
            agentplane_crds,
            agent_sandbox_controller,
            cert_manager_environment,
            cert_manager_trust,
            claude_rbac,
            cnpg,
            external_creds,
            external_secrets_config,
        ),
    )
