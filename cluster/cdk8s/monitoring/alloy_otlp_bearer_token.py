"""Inline Crossplane ownership of tf/gitops/alloy-otlp-bearer-token, preserving its existing state."""

from cdk8s import App, Chart

from cluster.cdk8s import opentofu
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "crossplane-alloy-otlp-bearer-token"
TF_MODULE_NAME = "alloy-otlp-bearer-token"
OUTPUT_DIR = f"{GENERATED_ROOT}/authentik/tf/{NAME}"
KUBERNETES_IDENTITY = "opentofu-alloy-otlp-bearer-token"


def chart(app: App) -> Chart:
    return opentofu.gitops_workspace_chart(
        app,
        module_name=TF_MODULE_NAME,
        identity_name=KUBERNETES_IDENTITY,
        secret_accesses=(
            ("authentik-secrets", "authentik", ("authentik-bootstrap",), ()),
            ("agents-infra-secrets", "agents-infra", (), ("alloy-otlp-client-credentials",)),
        ),
    )
