"""Inline Crossplane ownership of tf/gitops/gatus-sso, preserving its existing state."""

from cdk8s import App, Chart

from cluster.cdk8s import opentofu
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "crossplane-gatus-sso"
TF_MODULE_NAME = "gatus-sso"
OUTPUT_DIR = f"{GENERATED_ROOT}/authentik/tf/{NAME}"
KUBERNETES_IDENTITY = "opentofu-gatus-sso"


def chart(app: App) -> Chart:
    return opentofu.gitops_workspace_chart(
        app,
        module_name=TF_MODULE_NAME,
        identity_name=KUBERNETES_IDENTITY,
        secret_accesses=(
            ("authentik-secrets", "authentik", ("authentik-bootstrap",), ()),
            ("gatus-secrets", "gatus", (), ("gatus-oidc-secret",)),
        ),
    )
