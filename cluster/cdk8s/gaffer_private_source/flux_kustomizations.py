"""Flux Kustomizations for the cluster/k8s/gaffer-private-source slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecSourceRef, KustomizationSpecSourceRefKind

from cluster.cdk8s.flux import SOPS_DECRYPTION, Kustomization, flux_kustomization, flux_kustomization_depends_on


def gaffer_private_source(chart: Chart, flux_image_automation_ghcr: Kustomization) -> Kustomization:
    name = "gaffer-private-source"
    return flux_kustomization(
        chart,
        name,
        KustomizationSpecSourceRef(kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY, name="flux-system"),
        namespace="flux-system",
        wait=None,
        timeout="10m",
        path="./cluster/k8s/gaffer-private-source",
        decryption=SOPS_DECRYPTION,
        depends_on=[flux_kustomization_depends_on(flux_image_automation_ghcr)],
    )
