"""Flux Kustomizations for the cluster/k8s/dcgm-exporter slice."""

from __future__ import annotations

from pathlib import Path

from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDependsOn,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import flux_kustomization
from cluster.cdk8s.generation import write_yaml


def dcgm_exporter() -> dict[str, object]:
    name = "dcgm-exporter"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path="./cluster/k8s/dcgm-exporter",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="2m",
            depends_on=[
                # RuntimeClass "nvidia" + the containerd nvidia runtime.
                KustomizationSpecDependsOn(name="nvidia-device-plugin", namespace="ducktape-flux"),
                # PodMonitor CRD ships with kube-prometheus-stack in monitoring-stack.
                KustomizationSpecDependsOn(
                    name="monitoring-crds",  # PodMonitor
                    namespace="ducktape-flux",
                ),
            ],
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="DaemonSet", name="dcgm-exporter", namespace="dcgm-exporter"
                )
            ],
        ),
    )


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/dcgm-exporter/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, dcgm_exporter())
