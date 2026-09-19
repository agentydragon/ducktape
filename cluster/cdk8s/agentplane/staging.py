"""agentplane-staging: two replicas of everything, operator login federated through the
shared Authentik, and the reviewed GitHub/Kubernetes/SSH MCP action groups.
"""

from __future__ import annotations

from cdk8s import App, Chart, Duration
from cdk8s_plus_34 import DeploymentStrategy, PercentOrAbsolute

from cluster.cdk8s import cilium
from cluster.cdk8s.agentplane import actions, staging_config
from cluster.cdk8s.agentplane.actions_staging_policies import add_staging_action_policies
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
from cluster.cdk8s.ssh_mcp.config import BEARER_SECRET_NAME, MCP_URL

_NAMESPACE = "agentplane-staging"
_HOSTNAME = "agentplane-staging.allegedly.works"
_AUTHENTIK = "https://auth.allegedly.works"
_ACTIONS_OIDC_APP = f"{_AUTHENTIK}/application/o/agentplane-actions"
# The push services web-push subscriptions may target: both the Action Service's own
# allowlist and its egress rule, so the policy cannot drift from what the app accepts.
_WEB_PUSH_ALLOWED_HOSTS = ("fcm.googleapis.com", "updates.push.services.mozilla.com")
_GITHUB_MCP_URL = "https://api.githubcopilot.com/mcp/"
_KUBERNETES_MCP_URL = "https://kubectl-passthrough-mcp.allegedly.works/mcp"
_WEB_PUSH_SECRET = "agentplane-staging-web-push-vapid"
_WEB_PUSH_SECRET_FILE = "web-push-vapid.sops.yaml"
_GITHUB_MCP_CLIENT_SECRET = "haku-console-github-mcp-client-credentials"
_LITELLM_KEY_SECRET = "litellm-key-agentplane-staging"

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
    depends_on=(*DEPENDS_ON, "sso-providers-tf", "ssh-mcp", "haku-console"),
    extra_resources=(_WEB_PUSH_SECRET_FILE,),
    provided_secrets={
        "agentplane-oidc": "sso-providers-tf",
        "agentplane-mcp-oauth": "sso-providers-tf",
        _LITELLM_KEY_SECRET: "litellm-keys-tf",
        BEARER_SECRET_NAME: "ssh-mcp",
        # Reflected from the haku namespace by reflector.
        _GITHUB_MCP_CLIENT_SECRET: "haku-console",
        _WEB_PUSH_SECRET: _WEB_PUSH_SECRET_FILE,
    },
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
    egress=EgressProps(ca_secret_name="agentplane-egress-ca"),
    app=AppProps(
        hostname=_HOSTNAME,
        oidc_issuer=f"{_AUTHENTIK}/application/o/agentplane/",
        reach_incluster_authentik=True,
        runner_zone="hil-ovh",
    ),
    actions=ActionsProps(
        hostname="agentplane-actions-staging.allegedly.works",
        settings=_ACTIONS_SETTINGS,
        extra_reload_secrets=(_GITHUB_MCP_CLIENT_SECRET, _WEB_PUSH_SECRET, BEARER_SECRET_NAME),
        # The full OAuth linkage triad; testing mounts only the one MCP client's secret.
        oauth_secret_items=("client-secret", "jwt-signing-key", "encryption-key"),
        web_push_secret_name=_WEB_PUSH_SECRET,
        github_mcp_client_secret_name=_GITHUB_MCP_CLIENT_SECRET,
        ssh_mcp_bearer=True,
        extra_egress=[
            cilium.egress_to_fqdns(*_WEB_PUSH_ALLOWED_HOSTS),
            cilium.egress_to(cilium.endpoint_labels("ssh-mcp", "ssh-mcp"), 8080),
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
    add_staging_action_policies(chart)
    return chart
