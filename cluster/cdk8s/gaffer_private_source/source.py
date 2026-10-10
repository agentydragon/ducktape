"""The private companion monorepo's GitRepository, and the `gaffer-images`
ImageUpdateAutomation that commits image-tag bumps back to it.

The `gaffer-private` bridge Flux Kustomization that reconciles `gaffer-private/k8s/`
from this source is a node in the central chart (`flux_kustomizations.gaffer_private_bridge`).
"""

from __future__ import annotations

from cdk8s import App, Chart

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.image_automation import ImageUpdatePush
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "gaffer-private"
OUTPUT_DIR = f"{GENERATED_ROOT}/gaffer-private-source"


def chart(app: App) -> Chart:
    chart = Chart(app, "gaffer-private-source", disable_resource_name_hashes=True)
    ImageUpdatePush(
        chart,
        "image-update-push",
        name="gaffer-images",
        description="Sibling of `all-images` for the gaffer-private GitRepository. Watches ImagePolicies whose "
        "marker comments target gaffer-private's manifests and commits image-tag bumps back to gaffer-private/main.",
        source_name=NAME,
        source_description="Private companion monorepo. Reconciled from main branch via the ducktape-automation "
        "GitHub App (also used to push image-pin commits back here).",
        url="https://github.com/agentydragon/gaffer-private.git",
        branch="main",
        path="./k8s",
        # The gaffer-private Kustomizations reconcile from this source's artifact.
        source_interval="1m",
    )
    return chart


def gaffer_private_source(
    flux_chart: Chart, directory: RenderedDirectory, flux_image_automation_ghcr: Kustomization
) -> Kustomization:
    return flux_kustomization(
        flux_chart,
        "gaffer-private-source",
        directory,
        namespace="flux-system",
        wait=None,
        timeout="10m",
        depends_on=[flux_kustomization_depends_on(flux_image_automation_ghcr)],
    )
