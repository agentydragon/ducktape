"""Inline Crossplane ownership of tf/gitops/forgejo-claude, preserving its existing state."""

from cdk8s import App, Chart

from cluster.cdk8s import opentofu, terraform
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "crossplane-forgejo-claude"
TF_MODULE_NAME = "forgejo-claude"
OUTPUT_DIR = f"{GENERATED_ROOT}/forgejo/gitops/{NAME}"
KUBERNETES_IDENTITY = "opentofu-forgejo-claude"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    identity = opentofu.kubernetes_service_account(chart, KUBERNETES_IDENTITY)
    opentofu.secret_access(
        chart,
        "forgejo-secrets",
        identity=identity,
        namespace="forgejo",
        read=["forgejo-admin-password"],
        write=["claude-forgejo-credentials", "forgejo-token-mint-claude"],
    )
    opentofu.gitops_workspace(
        chart,
        "workspace",
        name=TF_MODULE_NAME,
        provider_config_name=NAME,
        schema=terraform.gitops_state_schema(TF_MODULE_NAME),
        directory=f"tf/gitops/{TF_MODULE_NAME}",
        management_policies=["Observe", "Create", "Update"],
        kubernetes_identity=KUBERNETES_IDENTITY,
    )
    return chart
