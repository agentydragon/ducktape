"""Inline Crossplane ownership of agent-machine-access using its existing PostgreSQL state."""

from cdk8s import App, Chart

from cluster.cdk8s import opentofu
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "agent-machine-access"
WORKSPACE_NAME = "crossplane-agent-machine-access"
OUTPUT_DIR = f"{GENERATED_ROOT}/authentik/tf/{WORKSPACE_NAME}"
KUBERNETES_IDENTITY = "opentofu-agent-machine-access"


def chart(app: App) -> Chart:
    return opentofu.gitops_workspace_chart(
        app,
        module_name=NAME,
        identity_name=KUBERNETES_IDENTITY,
        secret_accesses=(
            (
                "agents-infra-secrets",
                "agents-infra",
                (),
                (
                    # keep-sorted start
                    "agentplane-grocy-sf-readonly",
                    "haku-client-credentials",
                    "haku-mail-client-credentials",
                    "kubectl-sandbox-client-credentials",
                    # keep-sorted end
                ),
            ),
            (
                "authentik-secrets",
                "authentik",
                ("authentik-bootstrap",),
                (
                    # keep-sorted start
                    "aiquota-oidc",
                    "grocy-mcp-oidc-sf",
                    "grocy-mcp-oidc-vallejo",
                    "haku-console-oidc",
                    "tana-mcp-facade-oidc",
                    # keep-sorted end
                ),
            ),
        ),
    )
