"""agentplane-staging: two replicas of everything, operator login federated through the
shared Authentik, and the reviewed GitHub/Kubernetes/SSH/Home Assistant MCP action groups.
"""

from __future__ import annotations

from cdk8s import App, Chart, Duration
from cdk8s_plus_34 import DeploymentStrategy, PercentOrAbsolute
from eso_password_generator_crds.io.external_secrets.generators import Password, PasswordSpec
from external_secrets_crds.io.external_secrets import (
    ExternalSecret,
    ExternalSecretSpec,
    ExternalSecretSpecDataFrom,
    ExternalSecretSpecDataFromSourceRef,
    ExternalSecretSpecDataFromSourceRefGeneratorRef,
    ExternalSecretSpecDataFromSourceRefGeneratorRefKind,
    ExternalSecretSpecRefreshPolicy,
    ExternalSecretSpecTarget,
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
    ExternalSecretSpecTargetTemplate,
)
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
from cluster.cdk8s.agentplane import actions, staging_config
from cluster.cdk8s.agentplane.actions_staging_policies import add_staging_action_policies
from cluster.cdk8s.agentplane.chart import environment_chart
from cluster.cdk8s.agentplane.egress_credentials import STAGING_NAMESPACE, EgressCredentials
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
from cluster.cdk8s.flux import NAMESPACE as FLUX_NAMESPACE, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.generation import CNPG_DATABASE_READY, sops_decryption
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.ssh_mcp.config import BEARER_SECRET_KEY, BEARER_SECRET_NAME, MCP_URL

_NAMESPACE = "agentplane-staging"
_HOSTNAME = "agentplane-staging.allegedly.works"
_AUTHENTIK = "https://auth.allegedly.works"
_ACTIONS_OIDC_APP = f"{_AUTHENTIK}/application/o/agentplane-actions"
# The push services web-push subscriptions may target: both the Action Service's own
# allowlist and its egress rule, so the policy cannot drift from what the app accepts.
_WEB_PUSH_ALLOWED_HOSTS = ("fcm.googleapis.com", "updates.push.services.mozilla.com")
_GITHUB_MCP_URL = "https://api.githubcopilot.com/mcp/"
_KUBERNETES_MCP_URL = "https://kubectl-passthrough-mcp.allegedly.works/mcp"
_HOME_ASSISTANT_MCP_URL = "http://ha-mcp.ha-mcp.svc.cluster.local:8765/mcp"
# The same reflected Secret haku-console's own home_assistant server reads
# (cluster/cdk8s/haku/console_config.py), widened to reflect into this namespace too.
_HA_MCP_BEARER_SECRET = "ha-mcp-bearer"
_WEB_PUSH_SECRET = "agentplane-staging-web-push-vapid"
_WEB_PUSH_SECRET_FILE = "web-push-vapid.sops.yaml"
_GITHUB_MCP_CLIENT_SECRET = "haku-console-github-mcp-client-credentials"
_LITELLM_KEY_SECRET = "litellm-key-agentplane-staging"
_OIDC_SESSION_SECRET = "agentplane-staging-session-secret"

# The token the app exchanges its login for, and the one the Action Service accepts
# from operators: the same Authentik application.
_FEDERATION_TARGET = {
    "issuer": f"{_ACTIONS_OIDC_APP}/",
    "audience": "agentplane-actions",
    "jwks_uri": f"{_ACTIONS_OIDC_APP}/jwks/",
}
_ACTION_FEDERATION = {
    "mode": "exchange",
    "service_url": f"http://agentplane-actions.{_NAMESPACE}.svc.cluster.local:{actions.CONTAINER_PORT}",
    "token_endpoint": f"{_AUTHENTIK}/application/o/token/",
    "login_jwks_uri": f"{_AUTHENTIK}/application/o/agentplane/jwks/",
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
            "provider": "github",
            "server_url": _GITHUB_MCP_URL,
            "client_id": "configured-by-secret",
            "client_secret_file": "/etc/agentplane-github/client_secret",
            "redirect_uri": f"https://{_HOSTNAME}/mcp-linkage/callback",
        },
        "kubernetes": {
            "server_id": "kubernetes",
            "provider": "kubernetes",
            "server_url": _KUBERNETES_MCP_URL,
            "client_id": "kubectl-passthrough-mcp",
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
        "kubernetes": {
            "title": "Kubernetes MCP",
            "description": "Kubernetes passthrough MCP tools; every Action remains subject to operator approval.",
            "executor": {
                "kind": "mcp",
                "description": "Kubernetes MCP executed with the linked operator Kubernetes identity.",
                "config": {
                    "transport": "streamable-http",
                    "url": _KUBERNETES_MCP_URL,
                    "server_id": "kubernetes",
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
                    # The integration app's runner template, for now: it already carries the egress
                    # sidecar, the interception CA and the proxy environment, so the path is real
                    # end to end. Its workload container is the runner image, which is the wrong
                    # destination -- a box to run commands in wants neither the harnesses nor the
                    # state volume (agentplane/docs/sandbox_actions.md).
                    "runner": {
                        "template": "agentplane-runner",
                        "container": "runner",
                        "default_cwd": "/state",
                        "description": "The shared runner image: python, git and the agent harnesses.",
                    }
                },
                "default_environment": "runner",
            },
        },
        "ssh": {
            "title": "SSH",
            "description": "SSH commands on configured targets; every Action remains subject to operator approval.",
            "executor": {
                "kind": "mcp",
                "description": "Standalone SSH MCP backend; Agentplane retains approval and execution authority.",
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
                "description": "Standalone Home Assistant MCP backend (ha-mcp), the same one haku-console uses.",
                "config": {
                    "transport": "streamable-http",
                    "url": _HOME_ASSISTANT_MCP_URL,
                    "auth": "static_bearer",
                    "bearer_file": "/run/secrets/ha-mcp/bearer-token",
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
    include_action_policy_rule=False,
    replicas=ReplicaProfile(
        count=2,
        strategy=DeploymentStrategy.rolling_update(
            max_surge=PercentOrAbsolute.absolute(1), max_unavailable=PercentOrAbsolute.absolute(0)
        ),
        min_ready=Duration.seconds(5),
        pdb_min_available=1,
    ),
    app_config={**staging_config.config(), "action_federation": _ACTION_FEDERATION},
    db=DbProps(instances=2, pod_anti_affinity=True),
    llm_ingress=LlmIngressProps(litellm_key_secret_name=_LITELLM_KEY_SECRET),
    egress=EgressProps(
        ca_secret_name="agentplane-egress-ca", credentials_namespace=STAGING_NAMESPACE, include_forgejo_credential=True
    ),
    app=AppProps(
        hostname=_HOSTNAME,
        oidc_issuer=f"{_AUTHENTIK}/application/o/agentplane/",
        reach_incluster_authentik=True,
        runner_zone="hil-ovh",
        oidc_session_secret_name=_OIDC_SESSION_SECRET,
    ),
    actions=ActionsProps(
        hostname="agentplane-actions-staging.allegedly.works",
        settings=_ACTIONS_SETTINGS,
        extra_reload_secrets=(_GITHUB_MCP_CLIENT_SECRET, _WEB_PUSH_SECRET, BEARER_SECRET_NAME, _HA_MCP_BEARER_SECRET),
        # The full OAuth linkage triad; testing mounts only the one MCP client's secret.
        oauth_secret_items=("client-secret", "jwt-signing-key", "encryption-key"),
        web_push_secret_name=_WEB_PUSH_SECRET,
        github_mcp_client_secret_name=_GITHUB_MCP_CLIENT_SECRET,
        bearer_mcp_mounts=[
            BearerMcpMount(name="ssh-mcp", secret_name=BEARER_SECRET_NAME, secret_key=BEARER_SECRET_KEY),
            BearerMcpMount(name="ha-mcp", secret_name=_HA_MCP_BEARER_SECRET, secret_key="bearer-token"),
        ],
        extra_egress=[
            cilium.egress_to_fqdns(*_WEB_PUSH_ALLOWED_HOSTS),
            cilium.egress_to(cilium.endpoint_labels("ssh-mcp", "ssh-mcp"), 8080),
            cilium.egress_to(cilium.endpoint_labels("ha-mcp", "ha-mcp"), 8765),
            # Same public-origin Gateway path as the BFF: only Authentik SNI on node:443. The
            # resolver fetches /application/o/agentplane-actions/jwks/ over HTTPS.
            cilium.egress_via_gateway("auth.allegedly.works"),
            # GitHub MCP discovery advertises github.com as its OAuth authorization server.
            cilium.egress_to_fqdns("api.githubcopilot.com", "github.com"),
            # `github_public_repository` policies confirm a repository is public with an
            # unauthenticated GitHub REST call (github_policy/visibility.py); no credential
            # rides this path.
            cilium.egress_to_fqdns("api.github.com"),
            # The Kubernetes MCP server uses the public Gateway/remote-node path.
            cilium.egress_via_gateway("kubectl-passthrough-mcp.allegedly.works"),
            cilium.egress_to(cilium.AUTHENTIK_SERVER_LABELS, 9000, server_names=["auth.allegedly.works"]),
        ],
    ),
)


def chart(app: App) -> Chart:
    chart = environment_chart(app, ENV)
    _add_session_secret(chart)
    add_staging_action_policies(chart)
    EgressCredentials(
        chart,
        "egress-credentials",
        namespace=ENV.egress.credentials_namespace,
        proxy_namespace=ENV.namespace,
        include_forgejo=ENV.egress.include_forgejo_credential,
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
    ExternalSecret(
        scope,
        "session-external-secret",
        metadata=metadata(
            _OIDC_SESSION_SECRET,
            _NAMESPACE,
            annotations={"description": "ESO-generated Agentplane staging session-signing key."},
        ),
        spec=ExternalSecretSpec(
            refresh_policy=ExternalSecretSpecRefreshPolicy.CREATED_ONCE,
            target=ExternalSecretSpecTarget(
                name=_OIDC_SESSION_SECRET,
                creation_policy=ExternalSecretSpecTargetCreationPolicy.ORPHAN,
                deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
                immutable=True,
                template=ExternalSecretSpecTargetTemplate(type="Opaque", data={"session-secret": "{{ .password }}"}),
            ),
            data_from=[
                ExternalSecretSpecDataFrom(
                    source_ref=ExternalSecretSpecDataFromSourceRef(
                        generator_ref=ExternalSecretSpecDataFromSourceRefGeneratorRef(
                            api_version="generators.external-secrets.io/v1alpha1",
                            kind=ExternalSecretSpecDataFromSourceRefGeneratorRefKind.PASSWORD,
                            name=_OIDC_SESSION_SECRET,
                        )
                    )
                )
            ],
        ),
    )


def agentplane_staging(
    flux_chart: Chart,
    health_checks: list[KustomizationSpecHealthChecks],
    agentplane_crds: Kustomization,
    agent_sandbox_controller: Kustomization,
    cert_manager_environment: Kustomization,
    cert_manager_trust: Kustomization,
    claude_rbac: Kustomization,
    cnpg: Kustomization,
    external_secrets_config: Kustomization,
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
                external_secrets_config,
            ),
        ),
    )
