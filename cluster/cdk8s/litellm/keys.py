"""The one-shot Tofu handoff that deletes the former LiteLLM key resources."""

from __future__ import annotations

from cdk8s import App, Chart
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s import terraform
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.secret_ref import SecretRef

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/litellm/keys-tf"
TF_MODULE = "litellm-keys"


def keys_chart(app: App, module: ArtifactGeneratorSpecArtifacts) -> Chart:
    """Keep the old state/backend and providers available while Terraform destroys its resources."""
    chart = Chart(app, TF_MODULE, disable_resource_name_hashes=True)
    buildbuddy_api_key = SecretRef(namespace=terraform.NAMESPACE, name="buildbuddy-org-admin-api-key").key("api-key")
    terraform.gitops_terraform(
        chart,
        "terraform",
        module=module,
        variables=None,
        additional_vars=[terraform.secret_var("buildbuddy_api_key", buildbuddy_api_key)],
        env=[terraform.secret_env("TF_VAR_buildbuddy_api_key", buildbuddy_api_key)],
    )
    return chart


def litellm_keys_tf(
    chart: Chart, directory: RenderedDirectory, tofu_controller: Kustomization, litellm: Kustomization
) -> Kustomization:
    """Apply the state cleanup after the controller and the old provider's credentials are ready."""
    return flux_kustomization(
        chart,
        "litellm-keys-tf",
        directory,
        timeout="20m",
        depends_on=flux_kustomization_depends_on_many(tofu_controller, litellm),
        description="Retire the Terraform-owned LiteLLM resources before Crossplane recreates them.",
    )
