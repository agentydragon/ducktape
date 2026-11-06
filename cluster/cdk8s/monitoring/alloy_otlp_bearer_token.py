"""Inline Crossplane ownership of tf/gitops/alloy-otlp-bearer-token, preserving its existing state."""

from cdk8s import App, Chart

from cluster.cdk8s import opentofu, terraform
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "crossplane-alloy-otlp-bearer-token"
TF_MODULE_NAME = "alloy-otlp-bearer-token"
OUTPUT_DIR = f"{GENERATED_ROOT}/authentik/tf/{NAME}"
KUBERNETES_IDENTITY = "opentofu-alloy-otlp-bearer-token"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    identity = opentofu.kubernetes_service_account(chart, KUBERNETES_IDENTITY)
    opentofu.secret_access(
        chart, "authentik-secrets", identity=identity, namespace="authentik", read=["authentik-bootstrap"], write=[]
    )
    opentofu.secret_access(
        chart,
        "agents-infra-secrets",
        identity=identity,
        namespace="agents-infra",
        read=[],
        write=["alloy-otlp-client-credentials"],
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
