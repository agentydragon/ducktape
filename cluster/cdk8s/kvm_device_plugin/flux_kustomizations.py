"""Flux Kustomizations for the cluster/k8s/kvm-device-plugin slice."""

from __future__ import annotations

from pathlib import Path

from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import flux_kustomization
from cluster.cdk8s.generation import write_yaml


def kvm_device_plugin() -> dict[str, object]:
    name = "kvm-device-plugin"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path="./cluster/k8s/kvm-device-plugin",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="2m",
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="DaemonSet", name="generic-device-plugin", namespace="kvm-device-plugin"
                )
            ],
        ),
    )


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/kvm-device-plugin/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, kvm_device_plugin())
