"""Flux Kustomizations for the cluster/k8s/flux-image-automation-forgejo slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def flux_image_automation_forgejo(
    chart: Chart, forgejo_images: Kustomization, flux_image_automation_ghcr: Kustomization
) -> Kustomization:
    name = "flux-image-automation-forgejo"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            depends_on=flux_kustomization_depends_on_many(forgejo_images, flux_image_automation_ghcr),
            interval="10m",
            path="./",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
        ),
        description=(
            "Image automation for images hosted in our Forgejo registry "
            "(authenticated scans via the reflected ducktape-ci credential)."
        ),
    )
