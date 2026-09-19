"""Flux Kustomizations for the cluster/k8s/flux-image-automation-forgejo slice."""

from __future__ import annotations

from pathlib import Path

from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDependsOn,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import flux_kustomization
from cluster.cdk8s.generation import write_yaml


def flux_image_automation_forgejo() -> dict[str, object]:
    name = "flux-image-automation-forgejo"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            depends_on=[
                KustomizationSpecDependsOn(name="forgejo-images", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="flux-image-automation-ghcr", namespace="ducktape-flux"),
            ],
            interval="10m",
            path="./cluster/k8s/flux-image-automation-forgejo",
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


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/flux-image-automation-forgejo/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, flux_image_automation_forgejo())
