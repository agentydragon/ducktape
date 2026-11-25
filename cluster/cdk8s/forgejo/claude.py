"""Inline Crossplane ownership of tf/gitops/forgejo-claude, preserving its existing state."""

from cdk8s import App, Chart

from cluster.cdk8s import opentofu
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "crossplane-forgejo-claude"
TF_MODULE_NAME = "forgejo-claude"
OUTPUT_DIR = f"{GENERATED_ROOT}/forgejo/gitops/{NAME}"
KUBERNETES_IDENTITY = "opentofu-forgejo-claude"


def chart(app: App) -> Chart:
    return opentofu.gitops_workspace_chart(
        app,
        module_name=TF_MODULE_NAME,
        identity_name=KUBERNETES_IDENTITY,
        secret_accesses=(
            (
                "forgejo-secrets",
                "forgejo",
                ("forgejo-admin-password",),
                ("claude-forgejo-credentials", "forgejo-token-mint-claude"),
            ),
        ),
    )
