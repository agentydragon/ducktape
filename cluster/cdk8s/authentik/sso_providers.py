"""Inline Crossplane ownership of sso-providers using its existing PostgreSQL state."""

from cdk8s import App, Chart

from cluster.cdk8s import opentofu, terraform
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "sso-providers"
WORKSPACE_NAME = "crossplane-sso-providers"
OUTPUT_DIR = f"{GENERATED_ROOT}/authentik/tf/{WORKSPACE_NAME}"
KUBERNETES_IDENTITY = "opentofu-sso-providers"


def chart(app: App) -> Chart:
    return opentofu.gitops_workspace_chart(
        app,
        module_name=NAME,
        identity_name=KUBERNETES_IDENTITY,
        schema=terraform.gitops_state_schema(NAME),
        secret_accesses=(
            (
                "authentik-secrets",
                "authentik",
                (
                    # keep-sorted start
                    "authentik-auragon-google-email",
                    "authentik-bootstrap",
                    "authentik-google-oauth",
                    "authentik-user-password",
                    # keep-sorted end
                ),
                (
                    # keep-sorted start
                    "agentplane-mcp-oauth",
                    "agentplane-oidc",
                    "ai-session-archive-oidc",
                    "airlock-oidc-config",
                    "claude-session-sync-oidc",
                    "cli-proxy-api-admin-oidc",
                    "forgejo-oauth-client-secret",
                    "gitlab-spike-oidc",
                    "grafana-oidc-config",
                    "headlamp-oidc-secret",
                    "langfuse-oidc-config",
                    "matrix-oidc-config",
                    "plaid-link-oidc-config",
                    "plaid-spend-web-oidc-config",
                    "props-oidc-config",
                    # keep-sorted end
                ),
            ),
            ("study-casino-secrets", "study-casino", (), ("study-casino-oidc",)),
            ("quest-diagnostics-secrets", "quest-diagnostics", (), ("quest-diagnostics-oidc",)),
            ("platform-sessions-secrets", "platform-sessions", (), ("platform-sessions-oidc",)),
        ),
    )
