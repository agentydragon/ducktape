"""Flux Kustomizations for the cluster/k8s/proxmox-proxy slice."""

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


def proxmox_proxy() -> dict[str, object]:
    name = "proxmox-proxy"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            suspend=False,
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/proxmox-proxy",
            prune=True,
            wait=True,
            depends_on=[KustomizationSpecDependsOn(name="gateway", namespace="ducktape-flux")],
        ),
    )


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/proxmox-proxy/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, proxmox_proxy())
