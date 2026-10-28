"""Inline Crossplane ownership of sso-providers using its existing PostgreSQL state."""

from cdk8s import App, Chart

from cluster.cdk8s import opentofu, terraform
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "sso-providers"
WORKSPACE_NAME = "crossplane-sso-providers"
OUTPUT_DIR = f"{GENERATED_ROOT}/authentik/tf/{WORKSPACE_NAME}"
KUBERNETES_IDENTITY = "opentofu-sso-providers"


def chart(app: App) -> Chart:
    chart = Chart(app, WORKSPACE_NAME, disable_resource_name_hashes=True)
    identity = opentofu.kubernetes_service_account(chart, KUBERNETES_IDENTITY)
    opentofu.secret_access(
        chart,
        "authentik-secrets",
        identity=identity,
        namespace="authentik",
        read=[
            # keep-sorted start
            "authentik-auragon-google-email",
            "authentik-bootstrap",
            "authentik-google-oauth",
            "authentik-user-password",
            # keep-sorted end
        ],
        write=[
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
            "paperless-sso-providers",
            "plaid-link-oidc-config",
            "plaid-spend-web-oidc-config",
            "props-oidc-config",
            # keep-sorted end
        ],
    )
    opentofu.secret_access(
        chart, "study-casino-secrets", identity=identity, namespace="study-casino", read=[], write=["study-casino-oidc"]
    )
    opentofu.gitops_workspace(
        chart,
        "workspace",
        name=NAME,
        provider_config_name=WORKSPACE_NAME,
        schema=terraform.gitops_state_schema(NAME),
        directory=f"tf/gitops/{NAME}",
        management_policies=["Observe", "Create", "Update"],
        kubernetes_identity=KUBERNETES_IDENTITY,
    )
    return chart
