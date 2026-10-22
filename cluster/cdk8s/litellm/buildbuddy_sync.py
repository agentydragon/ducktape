"""Keep the BuildBuddy live-test credential synced from Crossplane's LiteLLM key."""

from __future__ import annotations

from cdk8s import App, Chart
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s import terraform
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.secret_ref import SecretRef

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/litellm/buildbuddy-sync"
TF_MODULE = "litellm-buildbuddy-sync"
NAME = "litellm-buildbuddy-sync"


def chart(app: App, module: ArtifactGeneratorSpecArtifacts) -> Chart:
    chart = Chart(app, TF_MODULE, disable_resource_name_hashes=True)
    terraform.gitops_terraform(
        chart,
        "terraform",
        module=module,
        variables=None,
        env=[
            terraform.secret_env(
                "TF_VAR_buildbuddy_api_key",
                SecretRef(namespace=terraform.NAMESPACE, name="buildbuddy-org-admin-api-key").key("api-key"),
            )
        ],
    )
    return chart


def resources(
    chart: Chart, directory: RenderedDirectory, tofu_controller: Kustomization, crossplane_keys: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        timeout="20m",
        depends_on=flux_kustomization_depends_on_many(tofu_controller, crossplane_keys),
        description="Publish Crossplane's LiteLLM live-test key as the sealed BuildBuddy OPENAI_API_KEY secret.",
    )
