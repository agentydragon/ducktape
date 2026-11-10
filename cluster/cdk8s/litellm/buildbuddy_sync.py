"""Retire Terraform's ownership of the BuildBuddy live-test secret."""

from __future__ import annotations

from cdk8s import App, Chart, JsonPatch
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts
from tofu_controller.io.fluxcd.contrib.infra import TerraformV1Alpha2SpecStoreReadablePlan

from cluster.cdk8s import terraform
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/litellm/buildbuddy-sync"
TF_MODULE = "litellm-buildbuddy-sync"
NAME = "litellm-buildbuddy-sync"


def chart(app: App, module: ArtifactGeneratorSpecArtifacts) -> Chart:
    chart = Chart(app, TF_MODULE, disable_resource_name_hashes=True)
    writer = terraform.gitops_terraform(
        chart,
        "terraform",
        module=module,
        variables=None,
        auto_apply=False,
        store_readable_plan=TerraformV1Alpha2SpecStoreReadablePlan.HUMAN,
    )
    # Approve only the reviewed module revision; the controller replans on approval.
    writer.add_json_patch(JsonPatch.add("/spec/approvePlan", "plan-latest-62a5ac5453"))
    return chart


def resources(
    chart: Chart, directory: RenderedDirectory, tofu_controller: Kustomization, crossplane_keys: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        timeout="20m",
        wait=False,
        depends_on=flux_kustomization_depends_on_many(tofu_controller, crossplane_keys),
        description="Forget Terraform's ownership of the BuildBuddy live-test secret without destroying it.",
    )
